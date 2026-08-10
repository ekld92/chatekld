# ChatEKLD — User Guide

A condensed, workflow-by-workflow guide. The full reference (config keys, API,
limits) is the project README; per-control help lives on the "?" badges next to
many settings; **⌘K** opens the command palette from anywhere.

## Getting started

1. **Pick a provider** in the sidebar — a local runner (Ollama / LM Studio)
   or an online one (OpenAI, Anthropic, Google, DeepSeek, Z.AI, OpenRouter).
   Online API keys go in `.env` (never in the app); embeddings always run
   locally.
2. **Index your vault**: set the Obsidian vault path, exclude folders you
   don't want read, then Index. Indexing is incremental — re-runs only touch
   changed files — and a long first index can be Paused or Cancelled at any
   time; both are resumable, completed work is preserved.
3. Everything that writes to your vault is **opt-in, previewed, confirmed and
   reversible** (journalled with Restore). Generators (deck / LaTeX article)
   are emit-only: they write new project folders, never your notes; the
   article generator's **Markdown-note** mode is the one exception — it
   writes a NEW note into the vault (never an overwrite), reversibly.

## Obsidian Agent (vault chat)

Ask questions against the indexed vault. Retrieval is hybrid (BM25 + dense)
with a cross-encoder reranker by default; every knob under the chat applies to
the *next* Send with no reindex.

- **Agent mode** lets the model search and read notes across several steps
  (`vault_search`, `vault_read_note`, `vault_list_materials`) instead of one
  retrieval pass — better for multi-part questions, slower per answer.
- **Web search** (agent mode, online providers only): adds a `web_search`
  tool backed by Tavily. Requires `TAVILY_API_KEY` in the environment; results
  are treated as untrusted source text, exactly like vault content. This is
  the app's one network surface beyond LLM providers and Zotero, and it is
  off by default.
- **Retrieval context**: each answer's sources (with scores) are shown in a
  collapsible panel and included in exports.
- **Export MD / TXT** writes the transcript to Downloads, stamped with the
  provider/model, per-answer token/cost figures, retrieval sources, and the
  agent's tool-call list.

### Upgrading the reranker

The default cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`) is trained on
English. For a multilingual FR/DE/EN vault, `BAAI/bge-reranker-v2-m3` usually
ranks noticeably better. No reindex is needed — reranking happens at query time.

1. **LLM Settings → Vault Chat · fine-tuning → Reranker model**: enter
   `BAAI/bge-reranker-v2-m3`.
2. **Quit and relaunch the app.** HuggingFace offline mode is decided once, at
   launch, from whichever model is configured at that moment — so a model
   changed mid-session can never be fetched, and reranking simply switches
   itself off for the rest of that session.
3. **Ask one question.** The new model (~2.3 GB) downloads on that first query
   and is cached from then on. You need to be online for it, and that first
   answer is slow; every later one is not.

To go back, enter the old id and relaunch again — the previous model stays
cached, so the switch back is offline and instant.

> **Measuring the difference — repo checkout only.** `scripts/eval_retrieval.py`
> scores your own vault three ways (current reranker · reranking off ·
> `bge-reranker-v2-m3`) and prints the recall/MRR/nDCG delta. It ships with the
> source and is **not** included in the packaged app; run it from a repo
> checkout with the project virtualenv's interpreter, after the one-time
> golden-set setup (`tests/eval/README.md`):
>
> ```bash
> python scripts/eval_retrieval.py --preset reranker-ab
> ```
>
> Running it also pre-populates the HuggingFace cache, so step 3 above becomes
> instant — but it is an optional measurement, not a prerequisite: steps 1–3
> work on their own.

## Single Paper

Upload one PDF, tune the prompt (document type, audience, focus question,
language), and stream a summary. A paper too large for the model's window is
summarised map-reduce (notes per part, then your template over the combined
notes) — slower, never silent. Exports carry a provenance footer (model,
params, tokens/cost).

## Plain Chat

A direct, multi-turn conversation with the configured model — no vault, no
tools. The transcript lives in the browser window only (lost on reload); use
Export MD/TXT to keep it, provenance included.

## Library Audit & Biblio Tagging

**Run Scan** (manual only) reconciles the vault against Zotero, local PDFs and
Finder tags — a set of mostly read-only report tabs (missing notes,
un-Zotero'd and duplicate PDFs, tag drift, Tag Hygiene…; the tag-drift and
Tag Hygiene tabs carry the explicit confirmed write verbs described below)
plus:

- **Tagging**: propose tags with the LLM (constrained to your `_tags.md`
  vocabulary), edit them as chips, and apply to the note frontmatter, the
  PDF's Finder tags and the Zotero item — every write confirmed, journalled
  and reversible (Restore). The **Triage deck** is the keyboard-first view
  (`P` propose · `A` accept · `S` skip · `M` defer · `V` PDF · `⌫` undo).
- **Deleting tags**: click the × on any current tag (table cells or deck
  chips) to remove it from that one store — confirmed and undoable. Removal
  is deliberately *not* vocabulary-gated: the tag most worth deleting is
  often one that already left the vocabulary.
- **Tag Hygiene → Retire**: each spelling variant has a `[retire]` button
  that removes the tag from *every* note, PDF and Zotero item that carries
  it (and, for in-vocabulary tags, its `_tags.md` row) — one confirm that
  states that exact scope (Zotero child-note tags and `.bib` keywords are
  not touched), journalled as a batch. Each run keeps its own one-click
  Undo button — starting another retire doesn't remove it — and the other
  `[retire]` buttons are disabled while a run is in flight.
- The **ambiguous-PDF triage deck** resolves unmatched PDFs against the
  bibliography, one card at a time, with undo.
- **Tag Drift** compares tags by a spelling-insensitive fingerprint: a tag the
  note already carries as a case/accent/separator variant — or as an FR/EN
  synonym listed in your `_tags.md` — is shown in the *Variants* column
  (informational), and only genuinely-missing tags get the Apply button. The
  *Note-only* column is the read-only reverse direction, and an Audit-Settings
  checkbox can include the Zotero item's own tags (badged "(item)").
- **Finder Tag Drift** compares, per paper, the PDF's Finder tags against the
  note's tags (papers with at least one Finder tag). Fix a gap in place:
  **[→ note]** writes the Finder-only tags into the note's frontmatter,
  **[→ Finder]** writes the note-only tags onto the PDF's Finder tags — both
  confirmed, journalled, and undoable per row. A tag marked *not in vocab* is
  absent from `_tags.md` (writes are vocabulary-gated); **[+ vocab]** adds it
  to the vocabulary and applies it in one step. Spelling near-misses show in
  the *Variants* column (informational).
- **Possible Duplicates** finds byte-*different* copies of the same paper the
  exact Duplicate PDFs scan cannot see — re-downloads with different
  watermarks (same first-page text), preprint vs published (same DOI/arXiv
  id), one citation key with several PDFs, and title+year+author record
  matches. Every group states its evidence, and content-similar pairs carry a
  `%` badge (likely / very likely / near-certain — cutoff tunable via
  `audit_near_dup_ratio`); **[not a duplicate]** hides a group from future
  scans and is undoable from the same row.

### Desktop widgets (paper of the day)

Each scan (and the **Refresh widget** button next to Run Scan) writes a small
snapshot — `widget.json` in the app-data folder — with a *paper of the day*
(random, avoiding recent repeats; same pick all day), one completable
suggestion like "Tag 10 papers", drift/unread counts, and a *notion of the
day* from your `_tags.md` glosses. Two optional companions read it, even with
the app closed: a **SwiftBar** menu-bar plugin and an **Übersicht** desktop
card (install files + steps in the project's `packaging/widgets/`, also
included in the DMG). Nothing scans in the background — the widgets show the
snapshot's age, and freshness is simply your last scan.

## Deck Generator & Report / Article

Turn a topic + instructions into a LaTeX Beamer deck or a prose article
grounded in the vault, in your own template's house style. Emit-only: you
compile with `make`. Optional extras, each bounded and screened:

- **Preview PDF** — one latexmk pass, renders in-app, never edits the `.tex`.
- **Compile & Auto-Fix** — a bounded compile + LLM log-repair loop; every
  repair is screened and verified by the next compile, with a `.bak` first.
- **Augment** — deepen / add a table / reformat / add a section to an
  existing deck or article. Preview first, then a confirmed Apply.
- **Too large to revise in one pass?** The refusal offers **Revise section
  by section**: one pass per `\section`, spliced back together, previewed as
  one combined proposal. Over-cap or failed sections are skipped and left
  unchanged (the offer says so).
- **Integrity review** (opt-in): flags compile-blocking problems and offers a
  screened repair. When the deck is too large for a one-pass repair, use
  **Try section-by-section repair** (same skip rule: a section it can't
  repair is left unchanged).
- The Markdown variant of the article generator writes ONE new note into your
  vault (never overwrites), stamped with provenance frontmatter and
  reversible from Note Refactor → Restore.

## Note Refactor

Point it at a sub-folder. **Run Plan** is read-only: it proposes OCR callouts
for images, deterministic formatting fixes, and hygiene notes, with preview
diffs. Writes happen only through the confirmed batch actions (Apply, Fix
formatting), each journalled and reversible via **Restore**.

- **LLM actions** (per note): improve formatting, summarize an attached PDF,
  free-prompt edits, advisory Mermaid diagrams — all previewed and staged
  server-side before the confirmed apply.
- **Note too large for one LLM pass?** The refusal offers **Edit section by
  section**: one bounded pass per heading section, recombined into a single
  previewed diff. Over-cap or failed sections are skipped and left
  unchanged.
- **Archive image** moves an unused image out of the vault (reference-checked
  vault-wide first) and swaps in a thumbnail — reversible.

## Prompt Hub & provenance

The Prompt Hub tab shows the exact system prompt last sent per workflow
(read-only, keys redacted). Every export and generated document is stamped
with the provider/model that produced it; generated LaTeX carries a comment
header (and articles an optional commented-out AI-disclosure paragraph in the
ICMJE style), generated vault notes carry provenance frontmatter.

## Writing good prompts

The app's own prompts already handle safety, grounding and citations — your
text steers *content*. The rules below distill current prompt-engineering
practice (few-shot examples, question decomposition, context budgeting,
cross-lingual retrieval research) down to what actually applies here:

- **Be specific — and ask one thing at a time.** « Quels sont les effets
  indésirables de la clozapine chez le sujet âgé ? » beats « parle-moi de la
  clozapine » — retrieval returns a handful of excerpts (top-k), so the
  narrower the question, the better the excerpts. A multi-part question is
  better decomposed: send the parts one after another, or hand the whole
  thing to agent mode, which breaks it down itself.
- **Put key terms in the source's language.** The vault mixes French, German
  and English, and the default prompts tell the model to use all three — but
  *retrieval* still matches best when the query and the note share a
  language (dense embeddings align same-language text more strongly, and the
  lexical leg needs the exact token). To surface a German source, include
  the German term: « TOC (Zwangsstörung, OCD) : quelles options après échec
  des ISRS ? ». You can also steer the answer side: « réponds en français,
  en t'appuyant aussi sur les sources en allemand et en anglais ».
- **Pick the answer mode for the task** (vault chat → Answer mode). *Strict*
  for factual lookup (refuses rather than guesses), *balanced* for everyday
  use (marks what is not in the notes), *exploratory* for synthesis and
  hypothesis-generation (allows flagged inferences), *concise* for quick
  bedside answers. Changing mode is often more effective than adding
  instructions to compensate for the wrong one.
- **Show the format, don't describe it.** One worked micro-example beats a
  paragraph of formatting rules: « Réponds sous forme de tableau, une ligne
  par molécule, ex. : | clozapine | 300–600 mg/j | agranulocytose | ». The
  same goes for deck/article instructions — sketch one slide or one section
  heading the way you want them all.
- **Keep system prompts short, and phrase them positively.** The vault-chat
  system prompt is a *prefix* over the app's own template (capped at 4000
  characters; the safety and grounding preamble is always kept) — two to
  four behavioural sentences (« réponds en français, style clinique,
  distingue faits et avis ») work better than a page of rules, and « cite
  chaque source » works better than « n'oublie pas de… ne pas omettre… ».
  Save the context window for what matters: the retrieved excerpts.
- **Use the focus question** (Single Paper) instead of rewriting the system
  prompt: it is injected with a directive that prioritises matching passages
  and asks the model to say explicitly when the paper doesn't answer it.
- **Retrieval is not a full read.** Answers are grounded in the retrieved
  excerpts only. If an answer looks thin, re-ask more precisely, raise top-k
  a little, or switch on agent mode — don't assume the note is missing.
- **Iterate instead of mega-prompting.** Start with a plain question; refine
  based on what comes back (« développe le point 2 », « uniquement les
  données chez l'enfant »). A short follow-up usually beats rewriting one
  giant prompt — and in vault chat each Send retrieves fresh excerpts for
  the refined question.

### Vault chat

Single-shot (the default) — one retrieval pass, best for factual questions:

- « Quelles sont les recommandations du vault sur la surveillance de la
  clozapine (NFS, glycémie) ? Réponds sous forme de tableau. »
- « Compare ECT et kétamine dans la dépression résistante d'après mes notes,
  en citant chaque source. »

Agent mode — the model searches and reads notes over several steps; better for
multi-part or cross-note questions, slower per answer:

- « Fais la synthèse de tout ce que le vault contient sur la catatonie :
  diagnostic, échelles, traitement. Cherche aussi avec les termes anglais et
  allemands. »

When to enable the optional retrieval knobs:

- **Thesaurus expansion / glossary primer** — when your questions use the
  vault's shorthand (`EDC`, `isrs`, `PEC`…). Honest limit: the synonym tables
  (`_abreviations.md` / `_tags.md`) are French/English, so German synonyms are
  **not** expanded — put the German term in your question yourself.
- **Wikilink expansion** — when the answer likely lives in notes *linked from*
  the obvious one (needs the reranker, which is on by default).
- **Web search** (agent mode, online provider, `TAVILY_API_KEY`) — when the
  question needs facts newer than your notes.

### Plain Chat

No vault, no retrieval — best for drafting and reformulation: « Rédige un
paragraphe de discussion… », « Traduis en anglais académique… ». Anything that
should be *grounded in your notes* belongs in vault chat instead.

### Single Paper

- Focus question: « L'effet est-il maintenu à 12 mois ? » · « Quels effets
  indésirables graves sont rapportés ? »
- Audience changes register, not content: "General public" simplifies the
  wording; "Experts" keeps the methodology.

### Deck & Article generators

Topic = the subject; instructions = everything else (audience, structure,
length, tone, languages):

- « Public : internes de psychiatrie. 10–12 diapos. Termine par une diapo
  "points clés". Appuie-toi aussi sur les sources anglaises et allemandes du
  vault, mais rédige en français. »
- « Article de synthèse pour un staff : contexte, données d'efficacité,
  tolérance, place dans la stratégie thérapeutique. Cite les sources clés
  (auteur, année). »

### Note Refactor (free-prompt edits)

Instructions operate on the *selected note or section* — say what to change
and what to preserve:

- « Reformate en titres et sous-titres clairs, sans changer le contenu
  clinique. »
- « Convertis les posologies en tableau : molécule, dose, indication, EI. »

Every LLM edit is previewed as a diff and applied only after you confirm.

## Tips & limits

- If you change the **embedding model** after indexing, re-index from
  scratch — mixing embeddings yields a useless index.
- PDFs are capped at 20 000 pages; anything over 1 000 pages is extracted in
  1 000-page ranges, so the cap bounds time, not memory. Scanned PDFs fall
  back to the OCR model when no text layer is found.
- The app's own API is local-loopback only. Outbound network is limited to
  your chosen LLM providers, Zotero, one-time model downloads (the
  HuggingFace reranker on its first use, and the optional `--seed-models`
  helper), and — only if you enable it — the agent's web-search tool.
  Uploads and the index live in an owner-only app-data directory.

## Recovering from anything

- Chat/summary errors offer Retry without losing your draft.
- Every vault write has a journal entry: Note Refactor → Restore, Tagging →
  Restore, and the audit's `.bak` files.
- **Reset App Data** (sidebar) clears uploads and the Obsidian index — never
  your vault; the reset flow can also delete the saved config and feedback
  file if you need a truly clean slate.
