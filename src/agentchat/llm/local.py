"""Local backend: real weights, loaded in-process with ``transformers``.

Satisfies the same ``LLMProvider`` protocol as the mock — nothing above
``llm/`` changes to accommodate it. Two properties matter enough to call out:
weights are read from disk by *path* with ``local_files_only=True`` (never
downloaded), and both loading and generation run on threads so the event loop
never blocks — generation is interruptible mid-token via a stopping criterion.
"""

from __future__ import annotations

import asyncio
import gc
import threading
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from agentchat.core.errors import ProviderError
from agentchat.core.models import Message
from agentchat.llm.base import GenerationOptions, ModelInfo
from agentchat.llm._hf_compat import (
    DONE,
    dtype_kwarg,
    import_backend,
    next_chunk,
    quiet_backend,
    stop_when_set,
)

#: Shared, read-only checkpoint store on the cluster. Overridable via
#: AGENTCHAT_MODEL_ROOT.
DEFAULT_MODEL_ROOT = Path(
    "/sc/projects/sci-lippert/intelligent-agents/model_checkpoints"
)

#: How long to wait for the next token before declaring the backend wedged.
#: Generous: the first token of a cold 14B model can lag behind a long prompt.
_TOKEN_TIMEOUT = 300.0

#: How long a stopped generation gets to unwind before we give up on joining
#: its thread.
_JOIN_TIMEOUT = 60.0

#: Fallback for models without a native reasoning mode: the thinking toggle
#: becomes an instruction instead of silently doing nothing.
_THINKING_INSTRUCTION = (
    "Work through the problem step by step before committing to an answer, "
    "then state the answer clearly."
)


class TransformersProvider:
    """An ``LLMProvider`` backed by a local Hugging Face checkpoint.

    ``fail`` mirrors ``MockProvider``, so a simulated backend failure can be
    exercised against the real backend too.
    """

    def __init__(
        self,
        info: ModelInfo,
        *,
        path: Path,
        fail: bool = False,
    ) -> None:
        self._info = info
        self._path = Path(path)
        self._fail = fail

        self._model: Any = None
        self._tokenizer: Any = None
        self._loaded = False
        self._supports_thinking = False

        #: Set to ask an in-flight ``generate`` to stop at the next token.
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- identity ---------------------------------------------------------

    @property
    def info(self) -> ModelInfo:
        return self._info

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def path(self) -> Path:
        return self._path

    # -- lifecycle --------------------------------------------------------

    async def load(self) -> None:
        if self._loaded:
            return
        if self._fail:
            raise ProviderError(f"{self._info.name} is unavailable (simulated failure)")
        # Reading ~29 GB off a shared filesystem takes tens of seconds.
        await asyncio.to_thread(self._load_blocking)
        self._loaded = True

    def _load_blocking(self) -> None:
        # Checked before importing torch/transformers: a missing checkpoint
        # is a common misconfiguration (wrong AGENTCHAT_MODEL_ROOT), and
        # those imports alone can take well over a minute.
        if not self._path.is_dir():
            raise ProviderError(
                f"{self._info.name}: no checkpoint at {self._path}. "
                "Set AGENTCHAT_MODEL_ROOT to where the weights live."
            )

        torch, auto_model, auto_tokenizer = import_backend(self._info.name)
        quiet_backend()

        if torch.cuda.is_available():
            dtype, device_map = torch.bfloat16, {"": "cuda:0"}
        else:
            # CPU inference on a 14B model is impractical, but refusing to load
            # is worse than being slow — the laptop case is the mock backend.
            dtype, device_map = torch.float32, {"": "cpu"}

        try:
            tokenizer = auto_tokenizer.from_pretrained(
                str(self._path),
                local_files_only=True,
                trust_remote_code=False,
            )
            model = auto_model.from_pretrained(
                str(self._path),
                local_files_only=True,
                trust_remote_code=False,
                device_map=device_map,
                **dtype_kwarg(dtype),
            )
        except Exception as error:  # noqa: BLE001 — every load failure is a UI error
            raise ProviderError(
                f"{self._info.name} failed to load from {self._path}: {error}"
            ) from error

        model.eval()
        self._tokenizer = tokenizer
        self._model = model
        # Ask the template rather than hardcoding per-model behaviour, so a
        # third model slots in without touching this class.
        self._supports_thinking = "enable_thinking" in (tokenizer.chat_template or "")

    async def unload(self) -> None:
        """Release the GPU so another model can become resident."""
        if not self._loaded:
            return
        await self._settle()
        await asyncio.to_thread(self._unload_blocking)
        self._loaded = False

    def _unload_blocking(self) -> None:
        self._model = None
        self._tokenizer = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:  # pragma: no cover — unload after a failed import
            pass

    # -- generation -------------------------------------------------------

    async def generate(
        self,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        if not self._loaded or self._model is None:
            raise ProviderError(f"{self._info.name} is not loaded")
        options = options or GenerationOptions()

        # A previous stopped generation may still be unwinding on the GPU.
        await self._settle()

        streamer, state = self._start_generation(messages, options)
        try:
            while True:
                chunk = await asyncio.to_thread(
                    next_chunk, streamer, self._info.name, _TOKEN_TIMEOUT
                )
                if chunk is DONE:
                    break
                if chunk:
                    yield chunk
            error = state.get("error")
            if error is not None:
                raise ProviderError(
                    f"{self._info.name} failed during generation: {error}"
                ) from error
        finally:
            # Covers both normal completion and cancellation, so pressing
            # Escape frees the GPU instead of letting the run finish invisibly.
            self._stop.set()

    def _start_generation(
        self, messages: Sequence[Message], options: GenerationOptions
    ) -> tuple[Any, dict[str, Any]]:
        """Build the prompt and hand generation to a worker thread."""
        import torch
        from transformers import StoppingCriteriaList, TextIteratorStreamer

        prompt = self._render_prompt(messages, options)
        inputs = self._tokenizer(
            prompt,
            return_tensors="pt",
            add_special_tokens=False,  # the chat template already emitted them
        ).to(self._model.device)

        streamer = TextIteratorStreamer(
            self._tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
            timeout=_TOKEN_TIMEOUT,
        )

        kwargs: dict[str, Any] = {
            **inputs,
            "streamer": streamer,
            "max_new_tokens": options.max_tokens,
            "do_sample": options.temperature > 0,
            "stopping_criteria": StoppingCriteriaList([stop_when_set(self._stop)]),
            "pad_token_id": self._pad_token_id(),
        }
        if options.temperature > 0:
            kwargs["temperature"] = options.temperature
        else:
            # Greedy decoding — clear the sampling defaults Qwen3 ships,
            # otherwise they're ignored with a warning on stderr.
            kwargs["top_p"] = kwargs["top_k"] = None
        if options.stop:
            kwargs["stop_strings"] = list(options.stop)
            kwargs["tokenizer"] = self._tokenizer

        state: dict[str, Any] = {}
        thread = threading.Thread(
            target=self._run_generation,
            args=(torch, kwargs, streamer, state),
            name=f"generate:{self._info.id}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return streamer, state

    def _run_generation(
        self, torch: Any, kwargs: dict[str, Any], streamer: Any, state: dict[str, Any]
    ) -> None:
        try:
            with torch.inference_mode():
                self._model.generate(**kwargs)
        except BaseException as error:  # noqa: BLE001 — reported to the consumer
            state["error"] = error
            # generate() ends the stream itself on success; on failure nothing
            # would, and the consumer would block until the token timeout.
            streamer.end()

    def _pad_token_id(self) -> int | None:
        tokenizer = self._tokenizer
        if tokenizer.pad_token_id is not None:
            return int(tokenizer.pad_token_id)
        if tokenizer.eos_token_id is not None:
            return int(tokenizer.eos_token_id)
        return None

    def _render_prompt(
        self, messages: Sequence[Message], options: GenerationOptions
    ) -> str:
        payload = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.content.strip()
        ]

        template_kwargs: dict[str, Any] = {}
        if self._supports_thinking:
            template_kwargs["enable_thinking"] = options.thinking
        elif options.thinking:
            payload = _prepend_instruction(payload, _THINKING_INSTRUCTION)

        try:
            return self._tokenizer.apply_chat_template(
                payload,
                tokenize=False,
                add_generation_prompt=True,
                **template_kwargs,
            )
        except Exception as error:  # noqa: BLE001 — a bad template is a UI error
            raise ProviderError(
                f"{self._info.name}: could not format the prompt: {error}"
            ) from error

    async def _settle(self) -> None:
        """Wait for any previous generation thread to finish, then re-arm."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._stop.set()
            await asyncio.to_thread(thread.join, _JOIN_TIMEOUT)
            if thread.is_alive():
                raise ProviderError(
                    f"{self._info.name}: the previous generation did not stop"
                )
        self._thread = None
        self._stop.clear()


def _prepend_instruction(
    payload: list[dict[str, str]], instruction: str
) -> list[dict[str, str]]:
    """Fold an instruction into the system turn, adding one if absent."""
    if payload and payload[0]["role"] == "system":
        head = dict(payload[0])
        head["content"] = f"{head['content']}\n\n{instruction}"
        return [head, *payload[1:]]
    return [{"role": "system", "content": instruction}, *payload]


def default_models(root: Path | None = None) -> list[tuple[ModelInfo, dict]]:
    """The cluster model catalogue.

    Context windows are set below what the configs advertise (Qwen3: 40960,
    Phi-4-mini: 131072): they're bounded by KV-cache memory, not architecture
    — on a 40GB A100, Qwen3-14B's weights alone take ~29GB. These numbers keep
    both models resident-able one at a time.
    """
    root = Path(root) if root is not None else DEFAULT_MODEL_ROOT
    return [
        (
            ModelInfo(
                id="phi-4-mini",
                name="Phi-4-mini-instruct",
                context_window=32768,
                description=(
                    "Microsoft, 3.8B. Fast to load and to answer; no native "
                    "reasoning mode, so thinking mode is prompted."
                ),
            ),
            {"path": root / "microsoft" / "Phi-4-mini-instruct"},
        ),
        (
            ModelInfo(
                id="qwen3-14b",
                name="Qwen3-14B",
                context_window=16384,
                description=(
                    "Alibaba, 14B. Stronger and slower; native thinking mode "
                    "driven by the Ctrl+T toggle."
                ),
            ),
            {"path": root / "Qwen" / "Qwen3-14B"},
        ),
    ]
