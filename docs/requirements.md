# Non-Functional Requirements

**Project:** Intelligent Agents — course project
**Source documents:** `Intelligent Agents Project Prompt.pdf` (version 2026-07-06), `system_vision.md`
**Mode:** Solo (2 electives required)
**Chosen electives:** Adaptive RAG · Sub-agent deployment · Intelligent context management (3 of 2 — one is buffer)
**UI:** Textual interface (TUI)
**Date:** 2026-08-06

---

## Reading guide

- **Section A** — NFRs traceable to the project prompt. These are graded. Every row cites the passage it comes from.
- **Section B** — NFRs derived from `system_vision.md`. Not graded, not required by the course. They exist so the deliverable is a usable foundation for the vision rather than a throwaway.
- **Priority:** `MUST` = failure to meet it fails the requirement · `SHOULD` = expected, degrades grade/quality if missing · `MAY` = optional, extra credit or nice-to-have.
- **Functional requirements are deliberately out of scope here.** Where a prompt passage is purely functional (e.g. "the user needs to be able to create new conversations"), only its non-functional shadow is recorded (e.g. the latency/persistence constraint that makes it usable).

---

# Section A — Prompt-derived NFRs (graded)

## A.1 Delivery & packaging

| ID | Requirement | Priority | Source passage |
|---|---|---|---|
| NFR-D-01 | The project MUST be runnable via `uv`, either as a self-contained script or via `uvx`. No manual dependency installation step may be required of the grader. | MUST | Required Features — "runnable with `uv`" |
| NFR-D-02 | Dependency and entry-point declaration MUST follow `uv` conventions (PEP 723 inline metadata for a self-contained script, or a `uvx`-installable package) so a clean machine can run the project from the archive alone. | MUST | Required Features — "Refer to the documentation of `uv`" |
| NFR-D-05 | The archive SHOULD stay under the Moodle limit where feasible — base model weights, caches, virtual environments, and ingested corpora excluded from the archive and fetched or regenerated on first run. LoRA adapters are the exception and ship with it (NFR-FT-04). | SHOULD | Overview — ZIP archive / Moodle size limit (avoids the external-hosting obligation) |
| NFR-D-07 | The presentation MUST be able to show a real-world use case that benefits from the elective features, not only isolated feature demos. | MUST | Required Features — video demo bullet ("at least one real-world use-case that benefitted from your elective features") |

## A.2 Portability & environment

| ID | Requirement | Priority | Source passage |
|---|---|---|---|
| NFR-P-01 | The UI framework MUST run on Windows, macOS, and Linux. Platform-specific APIs, path handling, and shell invocations are prohibited in the UI layer. | MUST | Required Features — "any UI framework that runs on Windows, MacOS, and Linux" |
| NFR-P-02 | The system MUST be operable over a terminal session (SSH into the compute cluster) with no local display server. This follows from the TUI choice and is a hard constraint on the demo path. | MUST | Required Features — TUI Pro note ("will make using the cluster easier") |
| NFR-P-03 | Choosing a TUI MUST NOT reduce the functional surface: every feature requirement holds identically as it would for a GUI. Any feature that is awkward in a terminal still has to be implemented, not dropped. | MUST | Required Features — "the requirements for the features *stay the same*" |
| NFR-P-04 | The system SHOULD degrade gracefully across terminal sizes and colour capabilities rather than assume a single terminal geometry. | SHOULD | Derived from NFR-P-01/02 (portability across three OS terminals) |
| NFR-P-05 | Model loading MUST tolerate the resource profile of both the cluster and a developer laptop — at minimum, the two required models must not have to be resident simultaneously if memory is insufficient. | SHOULD | Required Features — "loading of at least *two* different models" combined with the cluster context |

## A.3 Usability & interaction

| ID | Requirement | Priority | Source passage |
|---|---|---|---|
| NFR-U-01 | The full conversation history MUST be visible and navigable — *any* prior part reachable via scrolling or paging, not just a recent window. | MUST | Required Features — "view *any* part of the prior conversation history" |
| NFR-U-02 | History navigation MUST remain responsive on long conversations; rendering cost must not grow with total conversation length (virtualised/paged rendering rather than re-rendering the whole log). | SHOULD | Derived from NFR-U-01 ("any part" implies unbounded history) |
| NFR-U-03 | Switching between conversations MUST be possible at any time without losing or corrupting the state of the conversation being left. | MUST | Required Features — "switching between conversations" |
| NFR-U-04 | The UI MUST remain interactive during generation — a running generation may not block navigation, history scrolling, or stopping the generation. | MUST | Required Features — "start, **stop**, and resume chats" (stop is unimplementable in a blocked UI) |
| NFR-U-05 | Model switching MUST be exposed as a user-facing runtime action, not a config-file edit or restart. | MUST | Required Features — "ability to switch the underlying LLM model" |
| NFR-U-06 | Destructive actions (removing a conversation) SHOULD require confirmation or be undoable. | SHOULD | Derived from Required Features — "remove old conversations" |
| NFR-U-07 | Perceived latency SHOULD be managed: token streaming and visible progress/state indicators for any operation exceeding roughly one second (generation, model load, retrieval, sub-agent dispatch). | SHOULD | Derived from A.3 as a whole; TUI has no native spinner affordance |
| NFR-U-08 | The "thinking"/expensive modes MUST be user-controllable rather than always-on, so the user can trade quality against latency. | MUST (if ToT elected) · SHOULD | Elective — Tree-of-Thought, "a *user enabled* thinking mode" |

## A.4 Persistence & state

| ID | Requirement | Priority | Source passage |
|---|---|---|---|
| NFR-S-01 | Conversations MUST survive process restart — "resume chats" implies durable storage, not in-memory session state. | MUST | Required Features — "start, stop, and **resume** chats" |
| NFR-S-02 | Memory MUST be scoped to *groups* of chats (project-folder semantics), so recall is bounded by group membership and does not leak across groups. | MUST | Required Features — "memory mechanism for recalling prior information from *groups* of chats" |
| NFR-S-03 | Stored state SHOULD be inspectable and portable (a documented, non-opaque format such as SQLite or JSON) so the grader can verify persistence and so the archive stays self-contained. | SHOULD | Derived from NFR-S-01 and NFR-D-03 |
| NFR-S-04 | Deleting a conversation MUST remove it from all derived state (memory index, caches), leaving no orphaned references. | SHOULD | Derived from Required Features — "remove old conversations" + memory mechanism |

## A.5 Fine-tuning & model artefacts *(required — not elective)*

| ID | Requirement | Priority | Source passage |
|---|---|---|---|
| NFR-FT-01 | At least **two** fine-tuned models MUST ship with the project, each serving a distinct, stated special purpose. Two adapters trained for the same purpose do not satisfy this. | MUST | Required Features — "Add at least *two* fine-tuned models for special purposes (Full / LoRA / QLoRA)" |
| NFR-FT-02 | Adapter-based tuning (LoRA/QLoRA) SHOULD be preferred over full fine-tuning. The prompt's stated rationale is non-functional: training efficiency and lightweight exchangeability. | SHOULD | Required Features — "training is more efficient, and they are easier to exchange (lightweight)" |
| NFR-FT-03 | Fine-tuned models MUST be selectable at runtime through the same user-facing switching affordance as base models. "Easier to exchange" is only realised if exchange is a UI operation, not a restart. | MUST | Required Features — fine-tuning bullet + "ability to switch the underlying LLM model" |
| NFR-FT-04 | Adapter artefacts SHOULD be small enough to ship inside the ZIP archive, with base weights fetched on first run. This is the concrete payoff of choosing LoRA over full fine-tuning. | SHOULD | Derived from NFR-FT-02 + NFR-D-05 |
| NFR-FT-05 | Training MUST be reproducible from the archive alone: training script, hyperparameters, base-model identity, and the dataset (or a documented way to obtain it) are included. A shipped adapter with no training path is not verifiable. | MUST | Derived from NFR-FT-01 + hand-in as ZIP archive (Overview) |
| NFR-FT-06 | Training MUST fit the available cluster resources; where memory-bound, quantised tuning (QLoRA) is the expected route rather than reducing the number of fine-tunes below two. | MUST | Required Features — "LoRA/QLoRA, as their training is more efficient" |
| NFR-FT-07 | Each fine-tune's benefit over its base model MUST be demonstrable on its stated purpose (a before/after comparison), since the video demo has to show the features working. | MUST | Required Features — video demo bullets + NFR-D-07 |
| NFR-FT-08 | Multiple adapters SHOULD share a single resident base model rather than loading one full model copy per fine-tune. Otherwise the memory ceiling that NFR-P-05 already strains is multiplied. | SHOULD | Derived from NFR-FT-02 ("lightweight") + NFR-P-05 + NFR-SUB-05 |
| NFR-FT-09 | Switching adapters SHOULD NOT require a full base-model reload; adapter swap latency should be materially lower than base-model switch latency. | SHOULD | Derived from NFR-FT-02/03 + NFR-U-07 |
| NFR-FT-10 | The model/adapter that produced a given message SHOULD be recorded with that message, so a conversation spanning several models remains interpretable after the fact. | SHOULD | Derived from NFR-FT-03 + NFR-S-01 |

> **Counting note.** "Loading of at least *two* different models for generation" (NFR-P-05) and "at least *two* fine-tuned models" (NFR-FT-01) are separate obligations in the prompt. The safe reading is ≥2 selectable generation models **and** ≥2 fine-tuned artefacts. Whether the two fine-tunes may themselves count as the two switchable models is unresolved — see Open decision #5.

## A.6 Elective: Adaptive RAG

| ID | Requirement | Priority | Source passage |
|---|---|---|---|
| NFR-RAG-01 | The retrieval pipeline MUST be at least as structurally complex as the lecture diagram: judge, rewriter, multiturn retrieval, and summarisation — or a demonstrably equivalent architecture. Single-shot vector lookup does not satisfy this. | MUST | Elective — "same diagram as shown in the slides ... or an equivalently complex approach" |
| NFR-RAG-02 | Ingestion MUST accept user-provided documents, at minimum plain text and PDF. | MUST | Elective — "at least as text files and pdf's" |
| NFR-RAG-03 | Retrieval MUST be adaptive: the number of retrieval rounds is decided at runtime by the judge, not fixed, and MUST terminate — a bounded maximum iteration count is required to prevent unbounded loops. | MUST | Elective — "adaptive", "multiturn retrieval" |
| NFR-RAG-04 | Ingestion of a user corpus SHOULD be incremental and restartable; re-ingesting an unchanged corpus must not be required on every start. | SHOULD | Derived from NFR-RAG-02 + NFR-D-05 |
| NFR-RAG-05 | Retrieved evidence SHOULD be attributable to its source document, so the grader (and the vision's fidelity check) can see what grounded a generation. | SHOULD | Derived from NFR-RAG-01 (a judge is unevaluable without visible evidence) |

## A.7 Elective: Sub-agent deployment

| ID | Requirement | Priority | Source passage |
|---|---|---|---|
| NFR-SUB-01 | A sub-agent MUST be invocable as a tool call, receiving an LLM-defined task rather than a hardcoded one. | MUST | Elective — "realized as a tool call, where the model calls upon another LLM to *do* a certain task" |
| NFR-SUB-02 | The controlling LLM SHOULD be able to continue without blocking on the sub-agent's completion (asynchronous dispatch with later result collection). | MAY (extra credit) → treat as SHOULD | Elective — "Extra credit if the controlling LLM can continue onwards without having to wait" |
| NFR-SUB-03 | Sub-agent execution MUST be bounded — depth, count, and time limits — so recursive delegation cannot exhaust cluster resources. | MUST | Derived from NFR-SUB-01/02 (unbounded delegation is a stability failure) |
| NFR-SUB-04 | Sub-agent failure or timeout MUST NOT crash or hang the parent conversation; it degrades to a reported error. | MUST | Derived from NFR-U-04 (UI must stay responsive) |
| NFR-SUB-05 | Concurrent sub-agents MUST share model resources safely — no two concurrent generations may corrupt each other's state or exceed available memory. | MUST | Derived from NFR-SUB-02 + NFR-P-05 |

## A.8 Elective: Intelligent context management

| ID | Requirement | Priority | Source passage |
|---|---|---|---|
| NFR-CTX-01 | Context **compression** MUST be implemented: long histories reduced to a bounded representation without losing task-relevant information. | MUST | Elective — "at least *context compression*" |
| NFR-CTX-02 | Context **isolation** MUST be implemented: separate contexts (chats, projects, sub-agents) MUST NOT leak into one another. | MUST | Elective — "*context isolation*" |
| NFR-CTX-03 | Context **selection** MUST be implemented: what enters the prompt is chosen by relevance, not by recency alone. | MUST | Elective — "as well as *context selection*" |
| NFR-CTX-04 | Generation MUST NOT fail due to context-window overflow; the context manager is responsible for staying within the active model's window, including after a model switch to a smaller window. | MUST | Derived from NFR-CTX-01 + NFR-U-05 |
| NFR-CTX-05 | Context-management decisions SHOULD be observable (what was dropped, compressed, or selected) — required to demonstrate the feature during the presentation. | SHOULD | Derived from NFR-D-07 |

## A.9 Cross-cutting quality

| ID | Requirement | Priority | Source passage |
|---|---|---|---|
| NFR-Q-01 | Every implemented feature MUST be demonstrable through the UI without developer intervention (no scripts run on the side, no code edits mid-demo). | MUST | Required Features (grading is by demonstration) + NFR-D-07 |
| NFR-Q-02 | The system SHOULD be resilient to model backend failure — an unavailable model surfaces as a UI error, not a crash. | SHOULD | Derived from NFR-U-05 (two swappable backends imply partial availability) |
| NFR-Q-03 | Configuration (model paths, endpoints, corpus location) SHOULD be externalised, so the grader can point the system at their own environment. | SHOULD | Derived from NFR-D-01 |
| NFR-Q-04 | The archive SHOULD contain a README stating how to run it with `uv`, which electives were implemented, and where each is demonstrated. | SHOULD | Derived from NFR-D-01/03 and the grading model |

---

# Section B — Vision-derived NFRs (not graded)

Derived from `system_vision.md`. These shape *how* the required features are built so the result is a foundation for the multi-agent profile system rather than a generic chat client. None of them are course requirements; where one conflicts with Section A, Section A wins.

## B.1 Profile fidelity

| ID | Requirement | Priority | Rationale (vision passage) |
|---|---|---|---|
| VNFR-01 | An agent's output SHOULD be attributable to evidence from that person's own communication signals, not to generic model priors. Every profile-grounded claim traceable to a source. | SHOULD | "Profile fidelity as the moral core"; "structural facts alone do not make a profile that is recognizably a given person" |
| VNFR-02 | Profiles SHOULD represent both the psychological layer (values, goals, fears, drivers) and the sociological layer (who talks to whom, departmental proximity, unspoken rules) — the schema must not collapse to org-chart facts. | SHOULD | Layer 1 — "two levels" |
| VNFR-03 | Fidelity SHOULD be measurable: the system provides a way to compare an agent's predicted response to the real person's actual response. Use-case 1 is explicitly the first fidelity test. | SHOULD | Use-case ladder 1 — "the first fidelity test: how well does the network mimic the real participants?" |
| VNFR-04 | Agents SHOULD be built *in favour of* the person they represent — the profile carries their perspective rather than evaluating or scoring them. No ranking, rating, or performance-assessment surface. | SHOULD | "in *favor of* the human — carrying their flavor" |

## B.2 Isolation & multiplicity

| ID | Requirement | Priority | Rationale (vision passage) |
|---|---|---|---|
| VNFR-05 | Each agent MUST see only its own person's profile and permitted shared context. Cross-profile leakage is a correctness failure, not a privacy nicety — an agent that knows what it shouldn't cannot forecast perception accurately. | MUST (vision) | Layer 1 — one agent per employee; reinforced by NFR-CTX-02 |
| VNFR-06 | The system SHOULD support fan-out to N agents and collection of N independent responses to a single input — the mechanical core of *perception preview*. | SHOULD | Use-case ladder 1 |
| VNFR-07 | Network composition SHOULD be parameterisable along *scope* (individual → team → department → company → company-plus-context) without code changes. | SHOULD | Layer 1 — "composition varies along two axes: *scope* and *task*" |
| VNFR-08 | The agent count SHOULD scale to at least a team-sized network (order of 10 agents) within cluster resources; the architecture must not assume a single agent. | SHOULD | Layer 1 — scope axis |

## B.3 Data handling

| ID | Requirement | Priority | Rationale (vision passage) |
|---|---|---|---|
| VNFR-09 | Ingestion SHOULD handle communication exhaust in its native messy forms (email threads, chat logs, meeting notes) — not only clean prose documents. | SHOULD | Layer 1 — "emails, Slack messages, meeting notes" |
| VNFR-10 | Communication data SHOULD stay local to the machine or cluster; no profile-bearing content sent to third-party APIs by default. Real correspondence about real people is the input. | SHOULD | Implied by the data type; also derisks the thesis |
| VNFR-11 | Profiles SHOULD be inspectable and editable by a human — the represented person can see and correct their own profile. | SHOULD | "in favor of the human"; fidelity is unverifiable if the profile is opaque |
| VNFR-12 | Profile derivation SHOULD be re-runnable as signals accumulate; profiles are living artefacts, not one-time builds. | SHOULD | Use-case ladder 2–3 (a stand-in must stay current) |

## B.4 Extensibility toward the ladder

| ID | Requirement | Priority | Rationale (vision passage) |
|---|---|---|---|
| VNFR-13 | The agent interface SHOULD be uniform whether the responder is a human or an agent, so use-case 3 (agent as the human's face toward other agents) does not require re-architecture. | SHOULD | Use-case ladder 3 |
| VNFR-14 | Agent responses SHOULD be labelled as agent-generated wherever they could be mistaken for the real person. Non-negotiable for use-case 2 (stand-in when out of office). | SHOULD | Use-case ladder 2 |
| VNFR-15 | The *task* axis SHOULD be separable from the profile layer: the same agent network can be pointed at different qualitative tasks without rebuilding profiles. | SHOULD | Layer 1 — "task (what qualitative work the network does)" |
| VNFR-16 | If a fine-tune is used to carry voice or perspective, the adapter MUST be scoped to exactly one person and swappable per agent — a single adapter trained on the whole corpus averages several people into one voice, which is the precise failure mode the vision names. | SHOULD | "Profile fidelity as the moral core"; "recognizably a given person" |
| VNFR-17 | Where a profile is split across a fine-tuned adapter (style, voice, disposition) and retrieved evidence (facts, positions, history), the split SHOULD be explicit, so it stays clear which layer a given behaviour comes from and which one to fix when fidelity is poor. | SHOULD | Layer 1 — psychological vs. sociological layers; supports VNFR-03 |

---

## Mapping: vision → required & elective features

| Vision need | Feature carrying it | NFRs |
|---|---|---|
| Profiles grounded in real communication signals | Adaptive RAG *(elective)* | NFR-RAG-01..05, VNFR-01, VNFR-09 |
| One agent per employee; perception preview | Sub-agent deployment *(elective)* | NFR-SUB-01..05, VNFR-05, VNFR-06 |
| Profile compression; no cross-agent leakage | Intelligent context management *(elective)* | NFR-CTX-01..05, VNFR-05 |
| Voice and disposition of a specific person | Fine-tuning *(required)* | NFR-FT-01..10, VNFR-16, VNFR-17 |

> The two required fine-tunes are an opportunity rather than an unrelated chore: two person-specific adapters over one shared base satisfy NFR-FT-01 ("two special purposes") while directly serving VNFR-16. It also makes the demo concrete — the same prompt answered by two agents that sound like two different people is a legible *perception preview*.

---

## Open decisions

| # | Question | Blocks |
|---|---|---|
| 1 | Third elective (context management) is buffer beyond the required two — confirm it stays in scope or gets cut if time runs short. | NFR-CTX-* priority |
| 2 | Which two models satisfy NFR-P-05, and do both fit in cluster memory concurrently (needed for NFR-SUB-05)? | NFR-P-05, NFR-SUB-05 |
| 3 | Is the demo corpus real communication data or synthetic? Affects VNFR-10 and what can be shown on 2026-09-07. | VNFR-10, NFR-D-07 |
| 4 | Does "memory mechanism" (NFR-S-02) get implemented as the profile store, or as a separate mechanism alongside it? | NFR-S-02, VNFR-11 |
| 5 | May the two fine-tuned models double as the two switchable generation models, or are ≥2 base models required *in addition*? Worth asking the lecturer — it changes the memory budget materially. | NFR-P-05, NFR-FT-01 |
| 6 | What are the two "special purposes" for the fine-tunes? Two person-adapters (serves VNFR-16) vs. one person-adapter plus one functional adapter (e.g. profile extraction / summarisation)? | NFR-FT-01, NFR-FT-07 |
| 7 | What training data do the adapters use, given the demo corpus decision in #3? Person-adapters need per-person text volume that synthetic data may not supply. | NFR-FT-05, VNFR-10 |

---

## Changelog

- **2026-08-06** — Added §A.5 fine-tuning (NFR-FT-01..10) after noticing the "at least two fine-tuned models" bullet on p.2 is a **required**, not elective, feature. Added VNFR-16/17 (per-person adapters, style/evidence split), open decisions #5–7. Fixed NFR-D-05's dangling reference.
- **2026-08-06** — Initial extraction from `Intelligent Agents Project Prompt.pdf` (v2026-07-06) and `system_vision.md` (v4). Solo mode, TUI, three electives (RAG, sub-agents, context management).
