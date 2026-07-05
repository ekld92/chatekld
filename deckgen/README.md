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
1. preflight   GET /api/obsidian/status   (confirm an index is ready)
2. outline     POST /api/obsidian/chat (agent mode) -> JSON outline -> [sections]
3. per section POST /api/obsidian/chat (agent mode) -> Beamer frames for that section
4. assemble    sanitize each section + wrap in a known-good preamble -> <topic>.tex
5. validate    structural sanity check (balanced frames, one document env)
6. review*     opt-in LLM .tex integrity pass + screened auto-repair (*in-app only)
```

> **Step 6 is opt-in and in-app only.** When the Deck Generator's *integrity review* toggle
> is on (`deck_review_enabled` / body `review_enabled`, default **off**), the in-app route
> makes one RAG-free LLM call over the whole assembled deck to flag compile-blocking problems
> and, when it can, return a repaired copy. The repair is screened by
> [`review.py`](review.py)`::screen_repair` (it **refuses** a repair that introduces a
> dangerous compile-time macro the original lacked, or that loses the single `document`
> environment / all frames). Dangerous-macro detection is the shared
> [`assemble.py`](assemble.py)`::find_dangerous_macros`, which also catches the
> `\csname write18\endcsname` / `\@@input` obfuscations the bare `\macro` regex missed (residual
> catcode / char-by-char limits documented at the function). The repair is only ever *offered* —
> the original deck is written to disk **first** (before the review runs), so a slow/stalled/
> timed-out review never costs you the generated deck; the user applies a repair explicitly via
> `POST /api/deck/apply-repair`. This is still emit-only: no compiler is invoked, so the pass is a
> smarter heuristic, not a guarantee. `review.py` itself is pure (prompt build + parse + screen);
> the model call lives in the app route, so the deckgen **core** stays app-independent.
>
> **What `apply-repair` guards.** It mirrors the Note Refactor write model: it requires the
> generate frame's `tex_sha256` as `base_sha256` (a **stale-diff** guard — 409 if the on-disk deck
> changed since the review), **re-screens** the submitted `.tex` through `screen_repair` against the
> current deck (so a smuggled macro can't ride in via the client body), and rewrites **only**
> `<slug>.tex` (`scaffold.write_deck_tex`) — never the `Makefile`. **One limit to know:** the repair
> half is bounded by `REVIEW_MAX_CHARS` (input) and `deck_review_max_tokens` (output); a deck whose
> corrected form exceeds the output budget streams an unterminated block that is discarded, so for
> large decks the review degrades to **issues-only** (you apply the fixes by hand) — the deck frame
> flags this via `repair_truncated` and says to raise `deck_review_max_tokens`.

Each section is its own bounded agent turn, so the agent can `search` / `read_note`
across the vault without hitting the per-turn iteration / wall-clock caps that a single
whole-deck turn would.

> **Per-section resilience (in-app).** A local backend fails a turn transiently — a memory
> hiccup, a JIT model reload, a momentary timeout. Each outline/section turn is retried up to
> `deck_section_max_attempts` (1–5, default 3) times with linear `deck_retry_backoff_s` (0–30,
> default 3) backoff ([`retry.py`](retry.py)`::chat_with_retry`, cancel-aware, one `info` line per
> retry); a section that still fails degrades to a placeholder frame rather than aborting the deck.
> SDK-level retries are disabled on the LM Studio path (`max_retries=0`) so each call is bounded by
> one timeout and this in-process retry is the single recovery layer. A per-section provider error
> is **never** fatal to the whole deck (the route relabels it to a non-fatal info); only an
> unrecoverable *outline* failure ends the stream.
>
> **Resume (in-app).** Generation persists the outline and each completed section to
> `BASE_DIR/deckgen/checkpoints/<job_key>.json` ([`checkpoint.py`](checkpoint.py), atomic) right as
> they finish. A re-submitted identical request (`deck_resume_enabled`, default on) reuses the saved
> outline + already-generated sections and resumes from the first missing one; a fully successful
> generation deletes the checkpoint, and the deck panel surfaces a "Resumed: N reused" banner. Tick
> **Start fresh** (body `force_fresh`) to discard saved progress and regenerate. The `job_key` hashes
> only the content-determining inputs, so tweaking a sampling/retry knob still resumes the same deck.

> **Why a sanitize/assemble step?** ChatEKLD's `system_prompt` is a *prefix* over its own
> answer template (it keeps the grounding + safety preamble), so the model's output cannot
> be guaranteed to be pure Beamer. `deckgen` therefore steers the model *and* strips/wraps
> the result itself in [`assemble.py`](assemble.py).

## Install

```bash
# from the repo root, in the project venv (~/venvs/chatekld2026)
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
- scaffolds `<out_dir>/<slug>/<slug>.tex` + a `Makefile` (`scaffold_deck`) that locates the
  suite's `common/latex-build.mk` by walking up the directory tree (depth-independent, so the
  deck builds from any nesting level, e.g. `<suite>/cours/<slug>/`), so the deck drops straight
  into your suite.

### How references resolve (bare names, the kpathsea way)

The macro/bib scan resolves `\usepackage{...}` / `\addbibresource{...}` against a **suite
search path** (`_search_dirs`) — the template's own folder, then the **suite root** (the
ancestor holding `common/`), then `<suite_root>/common/` — mirroring the `TEXINPUTS` /
`BIBINPUTS` your `.latexmkrc` / `common/latex-build.mk` set at compile time. So a **bare** house
reference like `\usepackage{cress-style}` (found at `common/cress-style.sty`) or
`\addbibresource{_master.bib}` (found at the suite root, even when the template lives in a
sibling `template/` folder) is discovered — not mistaken for a CTAN package and skipped. This is
a *search path*, not a per-package list: **a new `.sty` you drop into `common/`, or a new
`.bib`, is picked up automatically with no code change**, and because the path is derived
relatively it works unchanged on any machine that keeps the suite layout. A real CTAN name
(`amsmath`, `helvet`) resolves to nothing and is silently skipped. Absolute paths are refused.

### Documenting a custom macro to the model (`% @deckgen`)

Macro *discovery* is already generic — any `\newcommand` / `\def` / `\NewDocumentCommand` is
found whether or not deckgen recognises it. To also give the model a **friendly description** of
a house macro (so it uses it correctly) **without editing app code**, annotate the definition in
your `.sty` / preamble:

```latex
% @deckgen: cite a bibliography key — numeric [n] + author/year footnote
\newcommand{\citefoot}[1]{...}

% @deckgen \commonlogo: insert a shared figure from common/fig
\newcommand{\commonlogo}[2][]{\includegraphics[#1]{#2}}
```

The positional form (`% @deckgen: …` on the line above a definition) describes the next macro;
the explicit form (`% @deckgen \name: …`) binds by name. An annotation **overrides** deckgen's
built-in description and travels with your vault, so future macros need only a vault edit — no
app change, portable across machines. Un-annotated macros still appear with a generic label.

Commented-out `\usepackage` / `\addbibresource` lines are ignored. The in-app window lets you
**edit the template/preamble before generating** — the macro/bib scan runs on your edited text.

## Augment mode (deepen / extend an existing deck) — in-app only

The Deck Generator window also **augments a deck that already exists** (one deckgen produced,
or hand-written) from a free-text instruction: *deepen* a section, *add a table*, or *add a
new section*. Like generation it is vault-grounded and **emit-only with a preview**, mirroring
Note Refactor's preview-then-confirm write model. The deckgen **core stays app-independent**
([`augment.py`](augment.py) is pure; the model call + the disk write live in
`api/routes/deck.py`, alongside the integrity-review call).

```
1. parse      split the deck into preamble / opening / sections / closing  (augment.split_deck)
2. guard      reject non-UTF-8 / >1 MB decks; an over-cap whole-deck REPLACE is refused
3. target     whole section-region, one \section, or "insert a new \section"
4. revise     one vault-grounded agent turn over the targeted excerpt
5. splice     sanitize + splice back, BYTE-IDENTICAL outside the edited span
6. screen     validate() + review.screen_repair() against the original deck
7. preview    return the proposed .tex + a frame/section count delta + a raw-byte
              sha256 token; STAGE the proposal server-side. Writes NOTHING to the deck.
8. apply      POST /api/deck/apply-augment (confirm) — read the staged proposal,
              stale-diff guard, re-screen, BACK UP <deck>.tex.bak, then write back
```

The load-bearing invariant in [`augment.py`](augment.py) is `replace_section(tex, sec, sec.body)
== tex` (byte-identical) — so a section-scoped edit changes only the bytes inside that span and
can ride the **whole-document** sha256 guard. The free-text instruction is the *trusted* task;
the existing deck content (and any retrieved vault note) is *untrusted source* wrapped in
`<existing>` — a deck frame can't redirect the instruction.

**Augment can overwrite an existing user file**, so its write path is stricter than apply-repair's
(it matches Note Refactor's vault writers rather than apply-repair):

- **server-side staging** — the preview stages the screened proposal under the app data dir
  (`BASE_DIR/deckgen/staging/`); `apply-augment`'s body is `{deck_path, base_sha256, confirm}` and
  the proposal is read **from staging, never from the client** (a page can't apply arbitrary `.tex`);
- **stale-diff** — the on-disk raw-byte sha must equal both the client's `base_sha256` and the
  staged base, else **409**;
- **re-screen** — the staged proposal is run back through `screen_repair` against the current deck
  (defence in depth against a tampered staging file);
- **backup** — the deck is copied to `<deck>.tex.bak` **before** the overwrite (and the apply aborts
  if the backup can't be written), so the change is always recoverable;
- **truncation guard** — a whole-deck *deepen/table* whose source region exceeds
  `AUGMENT_MAX_SOURCE_CHARS` (40k) is **refused** (the model would only have seen a prefix and its
  shorter output would silently drop the rest); narrow the scope to one section instead;
- **UTF-8 only** — a non-UTF-8 deck is rejected up front rather than corrupted on write;
- **count delta** — the preview reports `frames`/`sections` before→after so a silent content loss
  is visible; the UI flags any decrease.

No diagrams / TikZ are written — text frames and `tabular` tables only. Augment has **no journal**
(unlike Note Refactor's reversible vault writes); the `.tex.bak` is the recovery path. Only **one**
deck operation (generate *or* augment) runs at a time — a concurrent request gets a **409**.

Like generate, an augment turn is **resilient to a transient provider blip**: a turn-level `{"error"}`
the agent loop emits is relabelled to a non-fatal `{"info"}`, so a momentary timeout/JIT-reload no
longer discards the whole augmentation — the run still terminates cleanly with a single error only if
it truly produced no usable content.

> **Augment is in-app only** — there is no CLI flag for it (the CLI builds a *new* deck per run).

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

## Compile & Auto-Fix (in-app only, opt-in)

The one exception to "emit-only": after generating (or on any existing validated deck), the UI's
**Compile & Auto-Fix** button drives `POST /api/deck/compile-fix` — our **own** bounded
`latexmk -interaction=nonstopmode -no-shell-escape` run (never the suite's `make`, whose mk could
enable shell escape), with `TEXINPUTS`/`BIBINPUTS` pointed at the deck dir + the suite's `common/`
so bare-name `.sty`/`.bib` resolve exactly as the suite build resolves them.

The loop is *repair-budgeted* (`deck_compile_max_iters`, 1–3): compile → parse the `.log`
([`compile.py`](compile.py)`::parse_latex_log`) → one RAG-free LLM repair turn over the whole
`.tex` → `screen_repair` gate → write → **compile again to verify**. An unverified repair is never
the final on-disk state; a `.bak` of the original is written before the first repair; missing
`.sty`/`.cls`/`.bib` errors are surfaced to the user and never fed to the model (it would "fix"
them by deleting the `\usepackage`). `latexmk` runs in its own process group with TERM→KILL
escalation on `deck_compile_timeout_s`, and is discovered PATH-independently
(`compile.py::find_latexmk`, `/Library/TeX/texbin` first) so the packaged Finder-launched app
finds it. Model/token knobs are config-only (`deck_review_model` / `deck_review_max_tokens` — the
same never-body-overrides posture as the integrity review). Everything runs under the single
deck-op lock; a concurrent deck operation gets **409**.

## Tests

```bash
python -m pytest deckgen/tests/ -v
```

These cover the pure logic (outline parsing, `.tex` sanitize/assemble/validate, template
split/scan/bib, scaffold, and augment split/splice round-trip) with no server and no
`requests` import. They are **not** part of
the app's hermetic suite. The in-process runner + deck route are covered by `test_deck.py`
at the repo root (which *is* hermetic).

## Limitations

- No in-app LaTeX **compilation** — emit-only. The opt-in integrity review (step 6, app
  only) is an *LLM* pass + screened auto-repair, not a real `latexmk`/`pdflatex` run, so it
  catches likely compile-blockers but cannot guarantee the deck compiles.
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
