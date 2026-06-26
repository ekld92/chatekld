# deckgen — vault → Beamer `.tex` orchestrator

`deckgen` is a **standalone CLI** that turns a topic + free-form instructions into a
LaTeX **Beamer** lecture deck, grounded in your indexed Obsidian vault. It does this
entirely by driving the **running ChatEKLD app** over its local HTTP API — it adds no
retrieval, embedding, or LLM code of its own and **never imports the app**.

It **emits `.tex` only** (you compile it yourself), cites sources as **plain prose**
(e.g. `(source: note.md)`), and works with **offline** (Ollama / LM Studio) and
**online** (OpenAI / Anthropic / Google) providers.

## How it works

```
1. preflight   GET /api/obsidian/status + /materials   (confirm an index is ready)
2. outline     POST /api/obsidian/chat (agent mode) -> JSON outline -> [sections]
3. per section POST /api/obsidian/chat (agent mode) -> Beamer frames for that section
4. assemble    sanitize each section + wrap in a known-good preamble -> <topic>.tex
5. validate    structural sanity check (balanced frames, one document env)
```

Each section is its own bounded agent turn, so the agent can `search` / `read_note`
across the vault without hitting the per-turn iteration / wall-clock caps that a single
whole-deck turn would.

> **Why a sanitize/assemble step?** ChatEKLD's `system_prompt` is a *prefix* over its own
> answer template (it keeps the grounding + safety preamble), so the model's output cannot
> be guaranteed to be pure Beamer. `deckgen` therefore steers the model *and* strips/wraps
> the result itself in [`assemble.py`](assemble.py).

## Install

```bash
# from the repo root, in the project venv (~/venvs/papermind2026)
pip install -r deckgen/requirements.txt   # just `requests`
```

## Usage

The ChatEKLD app binds to a **dynamic port** (logged as `Starting Flask on port N` in
`chatekld.log`). Pass it with `--port`, or `--base-url`; with neither, `deckgen` tries to
auto-discover the port from the log.

```bash
# Offline (local, tool-capable model recommended: qwen2.5, llama3.1+, mistral-nemo)
python -m deckgen \
  --topic "Schizophrenia" \
  --instructions @lecture_notes.txt \
  --audience "medical students" \
  --provider ollama --model qwen2.5 \
  --port 5050 \
  --out schizophrenia.tex

# Online (the app must have been launched with ANTHROPIC_API_KEY in its env)
python -m deckgen --topic "Schizophrenia" --provider anthropic --model <claude-id> \
  --port 5050 --out schizophrenia.tex

# Inspect the outline only
python -m deckgen --topic "Schizophrenia" --provider ollama --model qwen2.5 --port 5050 --dry-run

# House-style template mode: reuse your own Beamer template (preamble, theme,
# custom macros like \citefoot / \commonlogo) and scaffold a compile-ready folder.
python -m deckgen --topic "Schizophrenia" --provider ollama --model qwen2.5 --port 5050 \
  --template /path/to/suite/template/presentation.tex
#   -> writes <suite>/<slug>/<slug>.tex + Makefile (out-dir defaults to the suite
#      root — the folder containing common/). Override with --out-dir, or pass
#      --out <file> to get a single .tex instead. Disable cite mapping with --no-citations.

# Then compile it yourself — WITHOUT shell-escape (the deck is built from
# untrusted vault notes; this prevents \write18 etc. from running at compile time)
latexmk -pdf schizophrenia.tex                 # latexmk does not enable shell-escape by default
pdflatex -no-shell-escape schizophrenia.tex    # explicit, if you use pdflatex directly
cd <slug> && make view                          # template mode: build with your suite's Makefile
```

## Template mode (custom Beamer templates with custom functions)

With `--template <path.tex>` (or the in-app **Deck Generator** window), deckgen:

- splits your template into `preamble` / opening scaffold (title + outline frames) /
  closing tail (`split_template`) and **reuses the preamble verbatim** — your document
  class, theme, packages and metadata. The template's *example* sections are dropped and
  the generated sections injected in their place;
- scans the preamble **and the local `.sty` files it `\usepackage`s** for custom macros
  (`scan_macros`), so house macros like `\citefoot{key}` and `\commonlogo[opts]{file}` are
  advertised to the model;
- resolves the bibliography from `\addbibresource{...}` (`resolve_bib`) so the model can emit
  real `\citefoot{key}` cites for a relevance-bounded candidate set, with plain-prose
  `(source: note.md)` as the fallback. `validate` flags any `\citefoot`/`\cite` key not found
  in the `.bib` (an invented citation) for you to review;
- scaffolds `<out_dir>/<slug>/<slug>.tex` + a 2-line `Makefile` (`scaffold_deck`) that
  `include`s `../common/latex-build.mk`, so the deck drops straight into your suite.

Commented-out `\usepackage` / `\addbibresource` lines are ignored. The in-app window lets you
**edit the template/preamble before generating** — the macro/bib scan runs on your edited text.

> **Compile safety.** `deckgen` writes model-generated LaTeX grounded in untrusted vault
> content. `validate` warns if the output contains shell-escape / file-IO macros
> (`\write18`, `\input`, `\include`, `\openin`, `\read`, `\immediate`). Always compile
> without shell-escape and review any such warning before building.

### Key flags

| Flag | Meaning |
|---|---|
| `--topic` (required) | Lecture topic. |
| `--instructions` | Free text, or `@path` to read from a file. |
| `--audience` | e.g. `"medical students"` — steers tone/depth. |
| `--provider` / `--model` / `--embed` | Provider + model selection (online keys live in the app's env). |
| `--port` / `--base-url` | How to reach the local app (port auto-discovered from the log otherwise). |
| `--out` | Output path (`-` for stdout). Default `<topic-slug>.tex`. |
| `--title` / `--author` / `--institute` / `--date` / `--theme` | Title-slide + preamble metadata. |
| `--agent-max-iters` | Agent iterations per turn (1–12, default 6). |
| `--max-sections` | Cap on outline sections (default 8). |
| `--temperature` | Sampling temperature. |
| `--dry-run` | Stop after the outline. |
| `--verbose` | Stream the agent trace + all info events. |

**Exit codes** (the `.tex` is written in every non-fatal case):

| Code | Meaning |
|---|---|
| `0` | Clean deck — no structural warnings, no placeholder sections. |
| `1` | Written, but with `validate` warnings and/or *some* sections that produced no usable content (placeholder frames inserted). |
| `2` | Written, but *every* section was a placeholder — the deck is effectively empty (bad model/vault). |

## Tests

```bash
python -m pytest deckgen/tests/ -v
```

These cover the pure logic (outline parsing, `.tex` sanitize/assemble/validate, template
split/scan/bib, scaffold) with no server and no `requests` import. They are **not** part of
the app's hermetic suite. The in-process runner + deck route are covered by `test_deck.py`
at the repo root (which *is* hermetic).

## Limitations

- No in-app LaTeX compilation / error-repair loop — emit-only.
- No automated figures (`\includegraphics` / `\commonlogo`); text frames only. (The model
  *may* emit a `\commonlogo{...}` it sees advertised, but deckgen does not place real images.)
- `\citefoot{key}` cite mapping (template mode) is best-effort over a relevance-bounded
  candidate set; `validate` flags invented keys but cannot guarantee the *right* key was
  chosen. Plain-prose `(source: note.md)` otherwise.
- Output quality depends on the model honoring the steering prompt; small local models may
  fall back to plain RAG (ChatEKLD surfaces a capability warning — `deckgen` echoes it).
- Cross-section coherence is best-effort; the whole outline is passed into each section
  call to reduce drift/overlap, but it is not eliminated.

## Future improvements / To consider

Captured from a 2026-06-19 design review on **offline batch generation** (e.g. "produce
25 lecture decks, one per teaching topic, from a single prompt"):

- **Batch / multi-deck generation is not built in.** `deckgen` is one deck per
  invocation. Two ways to get a batch, neither requiring changes to the deckgen core:
  - *(A) Caller supplies the topics* — loop a topic list and call `python -m deckgen`
    once per topic (each scaffolds its own `<slug>/` folder). Run **sequentially**: the
    vault holds a per-acquisition operation lock and there is a single local model in
    memory, so parallel runs would contend on both.
  - *(B) One prompt → N topics* — add a thin pre-step (one local LLM call: "propose N
    teaching topics on X as a JSON list") that feeds each topic into path (A). This is
    the only piece not already present; it belongs **outside** the app-independent core
    (wrap `ChatEKLDClient` / `InProcessChatRunner`), e.g. a `deckgen` batch subcommand or
    a separate script.
- **Two hard prerequisites for an offline run:**
  1. *A running, indexed Obsidian vault that actually covers every topic.* Outlines and
     sections are grounded in the vault; a topic with no supporting notes comes back as
     placeholder frames (exit code `2`). `/api/obsidian/status` must be `done` (or
     `paused_partial`).
  2. *A tool-capable local model.* Agent mode needs reliable function-calling. **Prefer
     `qwen2.5` (14B+)**; `llama3.1`/`qwen3` also work. ⚠️ The app's `DEFAULT_LLM`
     (`llama3.2`) is too small for dependable agent tool-calling — small models fall back
     to plain RAG. The local embed model (`nomic-embed-text` by default) must also be
     pulled.
- **Runtime is the main practical caveat, not a blocker.** Each deck is ~8 sections and
  **each section is its own bounded agent turn** (up to the 300 s per-turn wall-clock cap).
  25 decks × ~8 sections locally can run to **several hours** — plan it as an overnight,
  sequential batch and use the per-deck exit codes (`0` clean / `1` warnings or some
  placeholders / `2` empty) to flag decks for re-run.
- **Pre-flight sanity check (run on the Mac, all read-only):** confirm `ollama list`
  shows the server up with a tool-capable model + the embed model; confirm
  `~/Library/Application Support/ChatEKLD/obsidian_storage/obsidian_meta.json` exists; and,
  with the app running, that `/api/obsidian/status` reports `done` and
  `/api/obsidian/materials` lists notes covering the intended topics.
