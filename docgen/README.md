# docgen — vault → LaTeX article / report **or** Markdown note

`docgen` is the **prose sibling of [`deckgen`](../deckgen/README.md)**: it turns a
topic + free-form instructions into a grounded long-form document instead of a slide
deck. Like `deckgen`'s core it is **app-independent** (it never imports the Flask app);
unlike `deckgen` it has **no standalone CLI** — it is driven only through the app's
`/api/docgen/*` blueprint ([`api/routes/docgen.py`](../api/routes/docgen.py)).

It produces one of two kinds:

- **LaTeX article/report** (`\documentclass{article}`, prose `\section`s — not slides):
  **emit-only**, exactly like the deck. Scaffolds a compile-ready `<slug>/<slug>.tex` +
  `Makefile` into your suite; you compile with `make`, or use the in-app **Preview PDF**
  and opt-in **Compile & Auto-Fix**.
- **Markdown note written into your vault**: a NEW long-form `.md` note (`# Title` +
  `## sections`) under a sub-folder you choose. It **never overwrites** an existing note
  and is **journalled + reversible** from Note Refactor → Restore — the app's third
  vault-write path (via [`refactor/create_note.py`](../refactor/create_note.py)).

Both grounding rules from the deck carry over: the vault is the **sole** source, claims
cite their source note inline as `(source: note-name.md)`, and nothing is invented.

## Built on deckgen's engine

`docgen` binds `deckgen`'s extracted, document-class-agnostic substrate rather than
duplicating it:

- [`deckgen/latex.py`](../deckgen/latex.py) — the shared LaTeX substrate
  (metadata/title/dangerous-macro/cite/graphics/comment/template-assembly +
  `validate_common` + the `DocClassStrategy` seam) that `deckgen/assemble.py` binds to
  `BEAMER` and `docgen/article.py` binds to `ARTICLE`.
- [`deckgen/outline.py`](../deckgen/outline.py) / [`deckgen/sections.py`](../deckgen/sections.py)
  — parametrized with document-class hooks (sanitize / prompts / placeholder, defaulting
  to Beamer so the deck path stays byte-identical). `docgen/generate.py` (LaTeX article)
  and `docgen/markdown.py` (Markdown) bind their own hooks over the SAME
  retry/salvage/checkpoint engine.
- [`deckgen/augment.py`](../deckgen/augment.py) / [`deckgen/compile.py`](../deckgen/compile.py)
  — the augment splice + the Compile & Auto-Fix loop, reused as-is (the LaTeX path only).

## Module map

- `article.py` — the `ARTICLE` `DocClassStrategy`: `sanitize_article_section` (a `\section`
  gate, not a frame gate), `assemble_article`, `validate_article`
  (= article structural check + `validate_common`); re-exports the substrate.
- `prompts.py` — prose **article** outline / section / augment prompts + message builders
  (see [PROMPTS.md §8a-8c](../PROMPTS.md)).
- `markdown.py` — the **Markdown** kind: heading-gated `sanitize_markdown_section`,
  `assemble_markdown`, Markdown prompts (PROMPTS.md §8d-8e), and the outline/section bindings.
- `generate.py` — binds `deckgen.outline`/`sections` to the article hooks.
- `review.py` — `screen_article_repair`, the augment/compile safety gate (pure; the
  structural floor is "≥1 `\section`" instead of the deck's "≥1 frame").
- `tests/` — the app-independent core tests (article + markdown sanitize/assemble/prompts
  + the engine-hook bindings). Route-level coverage lives in `test_docgen_route.py`
  (LaTeX validators + a mocked-engine generate) and `test_docgen_md.py` (the `create_note`
  journal op + the `generate-md` route).

`docgen` imports only `deckgen.{latex,outline,sections}` (the route layer `api/routes/docgen.py` binds `deckgen.{augment,compile,review}`) — nothing
app-side.

## Config

`docgen_*` config keys mirror `deck_*` (temperature, max sections 1-30, agent iterations,
per-section attempts/backoff/max-tokens, resume) and are set in the LLM Settings window's
**"Report / Article · generation"** section. Augment and Compile & Auto-Fix reuse the
`deck_agent_*` / `deck_compile_*` / `deck_review_*` knobs. See the root
[README](../README.md#config-keys) and [CLAUDE.md](../CLAUDE.md) for the full contract.
