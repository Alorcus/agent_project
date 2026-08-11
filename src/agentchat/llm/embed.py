"""The sentence encoder backend: real weights, in-process, CPU-only.

Satisfies `EmbeddingProvider` — no torch object crosses back out of `encode`.
Weights are read from disk by path with ``local_files_only=True``, exactly
like `llm/local.py`'s checkpoints.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: Pinned as the default of both `ENCODER_MODEL` and `RECALL_FLOOR_MODEL` —
#: see § 1 of the stage-3 plan for why.
DEFAULT_ENCODER_ID = "sentence-transformers/all-MiniLM-L6-v2"

#: This encoder's trained sequence length — a model property, not a § 9
#: constant, so it stays a documented literal here.
_MAX_LENGTH = 256


class LocalEncoder:
    """`AutoModel` + mean pooling + L2 normalisation — what
    `sentence-transformers` wraps, spelled out so no new dependency is
    needed."""

    def __init__(self, model_id: str, *, path: Path) -> None:
        self._model_id = model_id
        self._path = Path(path)
        self._model: Any = None
        self._tokenizer: Any = None
        self._loaded = False

    @property
    def model_id(self) -> str:
        return self._model_id

    def load(self) -> None:
        if self._loaded:
            return
        from transformers import AutoModel, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            str(self._path), local_files_only=True, trust_remote_code=False
        )
        model = AutoModel.from_pretrained(
            str(self._path), local_files_only=True, trust_remote_code=False
        )
        model.eval()
        self._tokenizer = tokenizer
        self._model = model
        self._loaded = True

    def encode(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        import torch

        self.load()
        inputs = self._tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=_MAX_LENGTH,
            return_tensors="pt",
        )
        with torch.inference_mode():
            output = self._model(**inputs)

        mask = inputs["attention_mask"].unsqueeze(-1).expand(output.last_hidden_state.size()).float()
        summed = (output.last_hidden_state * mask).sum(dim=1)
        counts = mask.sum(dim=1).clamp(min=1e-9)
        pooled = summed / counts
        normalised = torch.nn.functional.normalize(pooled, p=2, dim=1)
        return [tuple(row.tolist()) for row in normalised]


def default_encoder_path(model_root: Path) -> Path:
    return Path(model_root) / "sentence-transformers" / "all-MiniLM-L6-v2"
