"""deckgen CLI entry point.

    python -m deckgen --topic "Schizophrenia" --instructions @notes.txt \
        --provider ollama --model qwen2.5 --audience "medical students" \
        --port 5050 --out schizophrenia.tex

Drives the running ChatEKLD app over its local HTTP API to produce a Beamer .tex
deck grounded in the indexed Obsidian vault. Emits the .tex only — compile it
yourself (e.g. ``latexmk -pdf schizophrenia.tex``).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Optional

from .assemble import DeckMeta, assemble, assemble_with_template, validate
from .client import ChatEKLDClient, DeckgenClientError, resolve_base_url
from .outline import OutlineError, request_outline
from .scaffold import ScaffoldError, scaffold_deck, slugify
from .sections import generate_section
from .template import (
    TemplateError,
    bib_candidates_block,
    find_suite_root,
    load_template_parts,
    macro_cheatsheet,
    relevant_bib_keys,
)

# Index states that mean a usable index exists on disk.
_USABLE_STATES = {"done", "paused_partial"}
_IN_PROGRESS_STATES = {"running", "scanning", "embedding", "paused", "paused_scan"}

# Info-event substrings worth surfacing even when not --verbose.
_NOTABLE_INFO = (
    "fall", "capability", "could not", "content filter", "cut off",
    "limit", "timed out", "Indexing is still",
)


def _eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip()).strip("_").lower()
    return slug or "deck"


def _read_instructions(value: Optional[str]) -> str:
    """Resolve --instructions: literal text, or @path to read a file."""
    if not value:
        return ""
    if value.startswith("@"):
        path = value[1:]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise SystemExit(f"deckgen: could not read instructions file {path!r}: {exc}")
    return value


def _make_event_printer(verbose: bool):
    """Return an on_event callback that streams progress to stderr."""
    def _on_event(evt: dict) -> None:
        if "iteration" in evt:
            if verbose:
                _eprint(f"    · iteration {evt['iteration']}")
        elif "thought" in evt:
            if verbose:
                _eprint(f"    · thought: {str(evt['thought']).strip()[:200]}")
        elif "tool_call" in evt:
            tc = evt["tool_call"]
            if verbose:
                _eprint(f"    · tool_call: {tc.get('name')} {tc.get('arguments')}")
        elif "tool_result" in evt:
            if verbose:
                tr = evt["tool_result"]
                flag = " [error]" if tr.get("is_error") else ""
                _eprint(f"    · tool_result{flag}: {str(tr.get('content',''))[:120]}…")
        elif "info" in evt:
            info = str(evt["info"])
            if verbose or any(s.lower() in info.lower() for s in _NOTABLE_INFO):
                _eprint(f"    · info: {info}")
        elif "error" in evt:
            _eprint(f"    · ERROR: {evt['error']}")
    return _on_event


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m deckgen",
        description="Generate a Beamer .tex lecture deck from the ChatEKLD Obsidian vault.",
    )
    p.add_argument("--topic", required=True, help="Lecture topic, e.g. 'Schizophrenia'.")
    p.add_argument("--instructions", default="",
                   help="Free-form guidance, or @path to read it from a file.")
    p.add_argument("--audience", default="the audience",
                   help="Target audience, e.g. 'medical students'.")

    p.add_argument("--provider", default="ollama",
                   choices=["ollama", "lm_studio", "openai", "anthropic", "google"],
                   help="Chat provider (online providers need the API key in the app's env).")
    p.add_argument("--model", "--llm", dest="model", default="",
                   help="Model name/tag for the provider.")
    p.add_argument("--embed", default="", help="Embedding model override (local only).")
    p.add_argument("--temperature", type=float, default=None, help="Sampling temperature (0.0-2.0).")
    p.add_argument("--agent-max-iters", type=int, default=6,
                   help="Agent iterations per turn (1-12).")
    p.add_argument("--max-sections", type=int, default=8,
                   help="Upper bound on outline sections.")

    p.add_argument("--base-url", default=None, help="Full API base URL, e.g. http://127.0.0.1:5050.")
    p.add_argument("--port", type=int, default=None, help="ChatEKLD port (loopback).")

    p.add_argument("--out", default=None,
                   help="Output .tex path ('-' for stdout). Default: <topic-slug>.tex")
    p.add_argument("--title", default=None, help="Deck title (default: the topic).")
    p.add_argument("--author", default="", help="Author for the title slide.")
    p.add_argument("--institute", default="", help="Institute for the title slide.")
    p.add_argument("--date", default="", help="Date (default: \\today).")
    p.add_argument("--theme", default="Madrid", help="Beamer theme (default: Madrid).")

    p.add_argument("--template", default=None,
                   help="Path to a Beamer .tex/.sty template; reuse its preamble, "
                        "theme and custom macros (your house style). Overrides the "
                        "built-in preamble and the --title/--author/--theme metadata.")
    p.add_argument("--out-dir", default=None,
                   help="With --template: scaffold <slug>/<slug>.tex + Makefile into "
                        "this dir (default: the template's suite root — the folder "
                        "containing common/).")
    p.add_argument("--citations", dest="citations", action="store_true", default=True,
                   help="With --template: emit \\citefoot{key} from the template's "
                        "bibliography when a claim matches (default).")
    p.add_argument("--no-citations", dest="citations", action="store_false",
                   help="Plain-prose citations only (disable \\citefoot mapping).")

    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and print the outline only; do not generate sections.")
    p.add_argument("--verbose", action="store_true", help="Stream agent trace + all info events.")
    return p


def _preflight(client: ChatEKLDClient, verbose: bool) -> None:
    try:
        status = client.status()
    except DeckgenClientError as exc:
        raise SystemExit(f"deckgen: cannot reach ChatEKLD: {exc}")
    state = str(status.get("state", "")).lower()
    vault = status.get("vault_path") or "(unset)"
    if verbose:
        _eprint(f"  status: state={state!r} vault={vault} "
                f"prewarm={status.get('prewarm_status')!r}")
    if state in _IN_PROGRESS_STATES:
        _eprint("  warning: vault indexing is in progress — answers may miss "
                "content not yet indexed.")
    elif state not in _USABLE_STATES:
        _eprint(f"  warning: vault index state is {state!r}; no fully-built index "
                "detected. Generation may return little or no content.")


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    instructions = _read_instructions(args.instructions)
    on_event = _make_event_printer(args.verbose)

    try:
        base_url = resolve_base_url(base_url=args.base_url, port=args.port)
    except DeckgenClientError as exc:
        raise SystemExit(f"deckgen: {exc}")

    _eprint(f"deckgen: using ChatEKLD at {base_url}")
    client = ChatEKLDClient(base_url)
    _preflight(client, args.verbose)

    # Optional: house-style template mode -------------------------------------
    template = None
    macros_block = ""
    cite_mode = "prose"
    if args.template:
        try:
            with open(args.template, "r", encoding="utf-8", errors="replace") as fh:
                template_tex = fh.read()
            template = load_template_parts(template_tex, args.template)
        except (OSError, TemplateError) as exc:
            raise SystemExit(f"deckgen: could not use template {args.template!r}: {exc}")
        macros_block = macro_cheatsheet(template.macros)
        use_bib = args.citations and bool(template.bib_index)
        cite_mode = "bib" if use_bib else "prose"
        _eprint(
            f"deckgen: template {args.template} — {len(template.macros)} macro(s), "
            f"{len(template.bib_index)} bib key(s); citations={'on' if use_bib else 'off'}."
        )

    # 1) Outline -----------------------------------------------------------
    _eprint(f"deckgen: requesting outline for {args.topic!r} …")
    try:
        sections, outline_result = request_outline(
            client,
            topic=args.topic,
            instructions=instructions,
            provider=args.provider,
            model=args.model,
            embed=args.embed,
            max_iters=args.agent_max_iters,
            temperature=args.temperature,
            max_sections=args.max_sections,
            on_event=on_event,
        )
    except (OutlineError, DeckgenClientError) as exc:
        raise SystemExit(f"deckgen: {exc}")

    _eprint(f"deckgen: outline has {len(sections)} section(s):")
    for i, sec in enumerate(sections, start=1):
        _eprint(f"  {i}. {sec.title}  ({len(sec.points)} point(s))")

    if args.dry_run:
        _eprint("deckgen: --dry-run set; stopping after outline.")
        return 0

    # 2) Per-section generation -------------------------------------------
    section_outputs = []
    for i, sec in enumerate(sections, start=1):
        _eprint(f"deckgen: generating section {i}/{len(sections)}: {sec.title} …")
        candidate_block = ""
        if template is not None and cite_mode == "bib":
            seed = sec.title + " " + " ".join(sec.points)
            candidate_block = bib_candidates_block(
                template.bib_index, relevant_bib_keys(template.bib_index, seed)
            )
        try:
            out = generate_section(
                client,
                index=i,
                section=sec,
                full_outline=sections,
                topic=args.topic,
                instructions=instructions,
                audience=args.audience,
                provider=args.provider,
                model=args.model,
                embed=args.embed,
                max_iters=args.agent_max_iters,
                temperature=args.temperature,
                macros_block=macros_block,
                cite_mode=cite_mode,
                candidate_bib_block=candidate_block,
                on_event=on_event,
            )
        except DeckgenClientError as exc:
            raise SystemExit(f"deckgen: section {i} failed: {exc}")
        for note in out.infos:
            if args.verbose or any(s.lower() in note.lower() for s in _NOTABLE_INFO) \
                    or "placeholder" in note or "error" in note:
                _eprint(f"    note: {note}")
        section_outputs.append(out)

    # 3) Assemble + validate ----------------------------------------------
    if template is not None:
        tex = assemble_with_template(
            section_outputs,
            preamble=template.preamble,
            opening=template.opening,
            closing=template.closing,
        )
        generated_span = "\n\n".join(
            s.body.strip() for s in section_outputs if s.body.strip()
        )
        warnings = validate(
            tex,
            generated_tex=generated_span,
            known_bib_keys=template.bib_keys if cite_mode == "bib" else None,
        )
    else:
        meta = DeckMeta(
            title=args.title if args.title is not None else args.topic,
            author=args.author,
            institute=args.institute,
            date=args.date,
            theme=args.theme,
        )
        tex = assemble(section_outputs, meta)
        warnings = validate(tex)
    for w in warnings:
        _eprint(f"deckgen: WARNING — {w}")

    n_total = len(section_outputs)
    n_placeholder = sum(1 for s in section_outputs if s.placeholder)
    if n_placeholder:
        _eprint(
            f"deckgen: {n_placeholder}/{n_total} section(s) produced no usable "
            "content (placeholder frames inserted)."
        )

    # 4) Write -------------------------------------------------------------
    # Template mode (no explicit --out): scaffold a compile-ready <slug>/ folder.
    if template is not None and args.out is None:
        out_dir = args.out_dir or find_suite_root(args.template)
        if out_dir and os.path.isdir(out_dir):
            slug = slugify(args.title if args.title is not None else args.topic)
            try:
                paths = scaffold_deck(out_dir, slug, tex, overwrite=False)
            except ScaffoldError as exc:
                raise SystemExit(f"deckgen: {exc}")
            _eprint(f"deckgen: wrote {paths['tex_path']}  "
                    f"({n_total} section(s), {len(tex)} bytes).")
            _eprint(f"deckgen: wrote {paths['makefile_path']}")
            if not paths["sibling_common"]:
                _eprint("deckgen: WARNING — output folder has no sibling common/ dir; "
                        "\\usepackage{../common/...} and ../_master.bib will not resolve.")
            _eprint(f"deckgen: compile it with:  cd {paths['project_dir']} && make view")
            return _exit_code(warnings, n_placeholder, n_total)
        _eprint("deckgen: --template set but no output dir / suite root found; "
                "writing a single .tex instead (pass --out-dir to scaffold).")

    out_path = args.out or f"{_slugify(args.topic)}.tex"
    if out_path == "-":
        sys.stdout.write(tex)
        sys.stdout.flush()
        _eprint("deckgen: wrote deck to stdout.")
    else:
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(tex)
        except OSError as exc:
            raise SystemExit(f"deckgen: could not write {out_path!r}: {exc}")
        _eprint(f"deckgen: wrote {out_path}  "
                f"({n_total} section(s), {len(tex)} bytes).")
        compile_hint = (
            "cd <folder> && make view" if template is not None
            else f"latexmk -pdf {out_path}"
        )
        _eprint(f"deckgen: compile it with:  {compile_hint}")

    return _exit_code(warnings, n_placeholder, n_total)


def _exit_code(warnings: list, n_placeholder: int, n_total: int) -> int:
    """Exit codes: 0 = clean; 1 = structural warnings and/or some placeholders;
    2 = every section was a placeholder (the deck is effectively empty)."""
    if n_total and n_placeholder == n_total:
        _eprint("deckgen: no usable content was generated for ANY section — the "
                "deck is effectively empty (check the model choice and that the "
                "vault is indexed and relevant).")
        return 2
    if warnings or n_placeholder:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
