"""CHERQAiN CLI.

Exposes the application workflows through a unified command line interface —
a thin client over the running server (GUI app or `cherqain server`), which
the driver discovers via ``BASE_DIR/server.json`` / ``CHATEKLD_API_URL``.
Only the read-only ``status`` / ``config`` reads have an in-process fallback;
every other command requires the server so its locking, validation, and the
selected subsystem's recovery/audit controls remain authoritative.

Notes:
- Click (shipped via Flask) provides the hierarchical subcommands with no new
  third-party dependency.
- Commands are grouped by domain; ``python -m cli --help`` is the current
  command inventory and ``CLI_GUIDE.md`` describes effects and recovery.
- Errors surface formatted message bodies; most commands exit 1 on refusal
  and 2 when the server is unavailable. Valid responses print readable output
  or JSON. Deeper input validation is the driver's/server's
  job (`_safe_subfolder`, the route validators) — the CLI layer itself only
  does argument-shape checks.
"""

import json
import os
import sys
from typing import Any
import click

# Ensure repository root is on sys.path BEFORE domain imports
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.driver import (  # noqa: E402
    _SERVER_DOWN as _SERVER_DOWN_MSG,
    audit_mapping,
    audit_pdf_decision,
    audit_scan,
    zotlink_attach,
    zotlink_attach_candidates,
    zotlink_repoint,
    zotlink_repoint_candidates,
    generate_deck,
    generate_doc,
    get_config,
    get_status,
    maintenance_run,
    maintenance_steps,
    notesync_status,
    notesync_campaign,
    library_campaign,
    set_config,
    summarize_paper,
    tagging_apply,
    tagging_pdf_links,
    tagging_inventory,
    tagging_model_info,
    tagging_propose,
    tagging_restore,
    vault_ask,
    vault_index,
    vault_search,
)


@click.group()
def cli():
    """CHERQAiN CLI - Perform PDF summarization, Vault RAG, Audit, Tagging, and Generation without GUI."""
    pass


# ---------------------------------------------------------------------------
# Status, Config & Server
# ---------------------------------------------------------------------------


@cli.command("status")
@click.option("--json-out", is_flag=True, help="Output raw JSON")
def status_cmd(json_out: bool):
    """Show system status, provider health, and vector index state."""
    res = get_status()
    if json_out:
        click.echo(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        click.echo("=== CHERQAiN Status ===")
        if res.get("server_running"):
            click.echo("Server: running")
        else:
            # The values below come from a read-only in-process snapshot, which
            # answers even with no server — so a bare "not running" reads like a
            # config problem when it is really a DISCOVERY one. Say what is
            # actually broken (writes) and how to get round it, because the GUI
            # can be up and healthy while BASE_DIR/server.json is missing
            # (see CLAUDE.md § Known Issues, 2026-08-12).
            click.echo("Server: not running (in-process snapshot)")
            click.echo("  Writes are unavailable — only status/config can be read.")
            click.echo("  If the app IS open, its discovery file is missing:")
            click.echo("    - quit and relaunch the app, or")
            click.echo("    - export CHATEKLD_API_URL=http://127.0.0.1:<port>")
            click.echo("      using the port from chatekld.log's 'Serving on' line.")
        if "config" in res:
            click.echo(f"Provider: {res['config'].get('provider')}")
            click.echo(f"Model: {res['config'].get('model')}")
            # Printed only when it DIFFERS from the chat model: an extra line
            # repeating the one above is noise, but a silent divergence is the
            # bug this fixes — `tagging_propose_model` overrides the chat model
            # for every propose call, and nothing anywhere said so.
            _tm = res["config"].get("tagging_model")
            if _tm and _tm != res["config"].get("model"):
                click.echo(f"Tagging Model: {_tm}")
            click.echo(f"Embed Model: {res['config'].get('embed_model')}")
            click.echo(f"Vault Path: {res['config'].get('vault_path')}")
        idx = res.get("vault_index")
        # The rich /api/obsidian/status payload shape on both paths (2026-07-22
        # audit C3 — the old printer read keys that existed in NO shape).
        if isinstance(idx, dict):
            click.echo(f"Vault Index State: {idx.get('state')}")
            if idx.get("is_partial"):
                click.echo("  (partial index)")
            if idx.get("warning"):
                click.echo(f"  Warning: {idx.get('warning')}")
        elif idx is not None:
            click.echo(f"Vault Index Status: {idx}")
        # Restore-journal size (2026-08-26). Best-effort and printed only when
        # the journal exists: applied Finder/Zotero undo records are never
        # evicted by design, so this number only ever grows and nothing else
        # in the app reported it. A lookup failure must not fail `status`.
        try:
            from core.driver import tagging_journal_stats
            js = tagging_journal_stats()
        except Exception:  # noqa: BLE001 — disclosure, never a gate
            js = {"error": "unavailable"}
        if "error" not in js and js.get("ops"):
            mb = js.get("bytes", 0) / (1024 * 1024)
            click.echo(
                f"Tag Journal: {js['ops']:,} operations, {mb:.1f} MB "
                f"({js.get('protected', 0):,} permanent)")
            ret = js.get("retention") or {}
            if ret.get("enabled"):
                pol = ret.get("policy")
                val = ret.get("days") if pol == "age_days" else ret.get("keep")
                click.echo(f"  Retention: {pol} = {val}")


@cli.command("config")
@click.option("--set-key", help="Key to update in config")
@click.option("--set-val", help="Value to set")
def config_cmd(set_key: str, set_val: str):
    """View or update configuration settings."""
    if set_key and set_val:
        res = set_config({set_key: set_val})
        if "error" in res:
            click.echo(f"Error: {res['error']}", err=True)
            sys.exit(1)
        click.echo(f"Updated {set_key} = {set_val}")
    else:
        cfg = get_config()
        click.echo(json.dumps(cfg, indent=2, ensure_ascii=False))


def _server_env_dirs() -> list:
    """``BASE_DIR`` first, then the checkout (``launch.py``'s order)."""
    from core.constants import BASE_DIR
    return [str(BASE_DIR), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]


@cli.command("server")
@click.option("--host", default="127.0.0.1", help="Host address to bind to (loopback only)")
@click.option("--port", default=5000, type=int, help="Port to listen on")
def server_cmd(host: str, port: int):
    """Launch the headless Flask backend server (loopback-only).

    Non-loopback binds are a HARD error, not an opt-in (2026-07-22 audit C5 —
    the old --unsafe-bind flag was provably dead functionality: the origin
    guard (api/security.py) fail-closes on any non-loopback Host header AND
    any non-loopback remote_addr, so a network bind 403'd every remote request
    anyway. "Making it work" would mean relaxing the DNS-rebinding/CSRF guard
    around an unauthenticated API — rejected by design).
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        click.echo(
            f"Refusing to bind {host}: the API is unauthenticated and its origin "
            f"guard rejects non-local clients by design, so a network bind cannot "
            f"work. Bind 127.0.0.1 and use SSH forwarding if you need remote "
            f"access.", err=True)
        sys.exit(1)

    from core.server_info import (ServerAlreadyRunning, acquire_server_lease,
                                  clear_server_info, write_server_info)
    try:
        server_lease = acquire_server_lease()
    except ServerAlreadyRunning as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        # The same .env files the GUI loads (BASE_DIR, then the checkout), before
        # the app import — without them ZOTERO_API_KEY was missing and the Zotero
        # transport silently fell back to the local API (CLI proof run 2026-09-23, C4).
        from core.env_files import load_env_files
        load_env_files(_server_env_dirs())
        click.echo(f"Starting CHERQAiN headless server on http://{host}:{port} ...")
        from app import create_app

        app = create_app()
        # Written slightly BEFORE bind (Waitress blocks): harmless, since every
        # reader re-probes /api/health before trusting the record.
        write_server_info(port, host=host)
        import atexit
        atexit.register(clear_server_info)
        try:
            # AP-06: the headless process is not a weaker server mode. It uses the
            # exact GUI Waitress pool/channel ceilings while retaining the same
            # loopback-only and single-process checks above.
            from core.wsgi import serve_loopback
            serve_loopback(app, host=host, port=port)
        finally:
            clear_server_info()

    finally:
        server_lease.close()


# ---------------------------------------------------------------------------
# Single Paper Summarizer
# ---------------------------------------------------------------------------


@cli.group("paper")
def paper_group():
    """Single PDF paper operations."""
    pass


@paper_group.command("summarize")
@click.argument("pdf_path", type=click.Path(exists=True))
@click.option("--preset", default="concise", type=click.Choice(["concise", "detailed"]))
@click.option("--lang", default="fr", help="Target language (fr, en, etc.)")
@click.option("--doc-type", default="auto", help="Document type (rct, review, etc.)")
@click.option("--audience", default="",
              type=click.Choice(["", "researcher", "student", "public"]),
              help="Target audience (empty = server default)")
def paper_summarize_cmd(pdf_path: str, preset: str, lang: str, doc_type: str,
                        audience: str):
    """Summarize a single PDF document."""
    click.echo(f"Summarizing {pdf_path} (preset={preset}, lang={lang})...")
    res = summarize_paper(pdf_path, preset=preset, lang=lang,
                          document_type=doc_type, audience=audience)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo("\n--- SUMMARY ---")
    click.echo(res.get("summary"))


# ---------------------------------------------------------------------------
# Vault (RAG & Indexing)
# ---------------------------------------------------------------------------


@cli.group("vault")
def vault_group():
    """Obsidian vault indexing, search and RAG operations."""
    pass


@vault_group.command("index")
def vault_index_cmd():
    """Build or update the Obsidian vault index (always incremental).

    (The former --force flag was removed 2026-07-22: it sent a body key the
    server never read. Use the GUI's Reset for a from-scratch rebuild.)
    """
    click.echo("Building/updating vault index...")
    res = vault_index()
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@vault_group.command("search")
@click.argument("query")
@click.option("--top-k", default=5, type=int, help="Number of results")
def vault_search_cmd(query: str, top_k: int):
    """Search notes in the indexed vault (runs one full RAG turn).

    (The former --mode flag was removed 2026-07-22: it was accepted and
    silently ignored — retrieval is always the server's configured hybrid
    pipeline.)
    """
    res = vault_search(query, top_k=top_k)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(f"Found {res.get('count', 0)} matching chunks for '{query}':\n")
    # The rows are the chat ``retrieval`` frame's ``{source, score, is_image,
    # paper?}`` — there is no text to print, and the former ``file_path``/
    # ``text`` reads printed ``None`` for every row (review 2026-09-23).
    for idx, item in enumerate(res.get("results", []), 1):
        source = item.get("source") or "?"
        paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
        label = paper.get("label")
        head = f"[{idx}] {source}" + (f" — {label}" if label else "")
        click.echo(f"{head} (score: {item.get('score')})")


@vault_group.command("ask")
@click.argument("question")
@click.option("--agent", is_flag=True, help="Use ReAct agent mode with vault tools")
def vault_ask_cmd(question: str, agent: bool):
    """Ask a question to the Obsidian vault."""
    click.echo(f"Asking vault (agent_mode={agent})...")
    res = vault_ask(question, agent_mode=agent)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo("\n--- ANSWER ---")
    click.echo(res.get("answer"))


# ---------------------------------------------------------------------------
# Library Audit
# ---------------------------------------------------------------------------


@cli.group("audit")
def audit_group():
    """Library audit operations (Zotero, Vault, PDFs, Finder)."""
    pass


@audit_group.command("scan")
def audit_scan_cmd():
    """Run a read-only audit scan of vault notes vs Zotero."""
    click.echo("Running library audit scan...")
    res = audit_scan()
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


def _read_path_selection(pdfs: str, pdfs_file) -> list:
    """``--pdfs a,b`` ∪ ``--pdfs-file`` (one vault-relative path per line,
    ``#`` comments) → ordered list; an EMPTY selection is a UsageError, never
    "everything" (the `_read_key_selection` posture)."""
    selected: list = []
    for chunk in (pdfs or "").split(","):
        p = chunk.strip()
        if p and p not in selected:
            selected.append(p)
    if pdfs_file:
        from pathlib import Path as _P

        try:
            text = _P(pdfs_file).read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise click.UsageError(
                f"--pdfs-file is not UTF-8 text ({exc.reason} at byte {exc.start}).")
        for line in text.splitlines():
            p = line.split("#", 1)[0].strip()
            if p and p not in selected:
                selected.append(p)
    if not selected:
        raise click.UsageError(
            "No PDF selected: give --pdfs and/or a non-empty --pdfs-file.")
    return selected


@audit_group.command("mapping")
@click.option("--pdfs", default="", help="Comma-separated vault-relative PDF paths.")
@click.option("--pdfs-file", type=click.Path(exists=True, dir_okay=False),
              default=None, help="File with one vault-relative PDF path per line.")
@click.option("--link", "link_key", default=None, metavar="KEY",
              help="Link the ONE given PDF to this citation key.")
@click.option("--detach", "detach_key", default=None, metavar="KEY",
              help="Detach the ONE given PDF from this key (no automatic rung re-claims it).")
@click.option("--detach-all", "do_detach_all", is_flag=True,
              help="Detach the ONE given PDF from EVERY key (no automatic rung "
                   "may ever claim it; it stays listed as unmapped).")
@click.option("--no-match", "state_no_match", is_flag=True,
              help="Mark the PDFs 'not a paper for Zotero'.")
@click.option("--pending", "state_pending", is_flag=True,
              help="Mark the PDFs 'add to Zotero later'.")
@click.option("--clear", "do_clear", is_flag=True,
              help="Undo every curated decision for the PDFs.")
def audit_mapping_cmd(pdfs, pdfs_file, link_key, detach_key, do_detach_all,
                      state_no_match, state_pending, do_clear):
    """Curate the PDF<->bib mapping (confirm-gated on the server; undoable with --clear).

    Exactly one of --link / --detach / --detach-all / --no-match / --pending
    / --clear.
    """
    chosen = [name for name, on in (("link", link_key), ("detach", detach_key),
                                    ("detach_all", do_detach_all),
                                    ("no_match", state_no_match),
                                    ("pending", state_pending),
                                    ("clear", do_clear)) if on]
    if len(chosen) != 1:
        raise click.UsageError(
            "Choose exactly one of --link KEY, --detach KEY, --detach-all, "
            "--no-match, --pending, --clear.")
    action = chosen[0]
    rels = _read_path_selection(pdfs, pdfs_file)
    key = link_key or detach_key
    if action in ("link", "detach", "detach_all") and len(rels) != 1:
        raise click.UsageError(
            f"--{action.replace('_', '-')} takes exactly one PDF.")
    click.echo(f"audit mapping: {action} x {len(rels)} PDF(s)"
               + (f" -> {key}" if key else "") + " ...")
    res = audit_mapping(action, rels, key)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@audit_group.command("pdf-decision")
@click.option("--keys", default="", help="Comma-separated citation keys.")
@click.option("--keys-file", type=click.Path(exists=True, dir_okay=False),
              default=None, help="File with one citation key per line.")
@click.option("--dismiss", "verb_dismiss", is_flag=True,
              help="Mark the entries as needing no local PDF.")
@click.option("--clear", "verb_clear", is_flag=True,
              help="Undo the decision for the entries.")
@click.option("--clear-all", "verb_clear_all", is_flag=True,
              help="Undo every 'needs no PDF' decision.")
def audit_pdf_decision_cmd(keys, keys_file, verb_dismiss, verb_clear, verb_clear_all):
    """Record / undo 'this bib entry needs no local PDF' (Bib Entries Without PDFs)."""
    chosen = [n for n, on in (("dismiss", verb_dismiss), ("clear", verb_clear),
                              ("clear_all", verb_clear_all)) if on]
    if len(chosen) != 1:
        raise click.UsageError("Choose exactly one of --dismiss, --clear, --clear-all.")
    verb = chosen[0]
    selection = None
    if verb != "clear_all":
        selection = _read_key_selection(keys, keys_file)
        if selection is None:
            raise click.UsageError(f"--{verb} needs --keys or --keys-file.")
    elif keys or keys_file:
        raise click.UsageError("--clear-all takes no keys.")
    res = audit_pdf_decision(verb, sorted(selection) if selection else None)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Vault Maintenance (private-only build feature)
# ---------------------------------------------------------------------------


@cli.group("zotlink")
def zotlink_group():
    """Zotero attachment links (linked files pointing at the vault's PDFs)."""
    pass


@zotlink_group.command("candidates")
@click.option("--json-out", is_flag=True, help="Print the full JSON payload.")
def zotlink_candidates_cmd(json_out: bool):
    """Records with a Zotero item and mapped PDFs Zotero does not yet link."""
    res = zotlink_attach_candidates()
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(2 if res["error"] == _SERVER_DOWN_MSG else 1)
    if json_out:
        click.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    click.echo(f"Records with a Zotero item and a PDF: {res.get('count')}")
    click.echo(f"PDFs not yet linked in Zotero:      {res.get('to_link')}")
    click.echo(f"Group-library records (never linked): {res.get('group_count')}")
    click.echo(f"Transport: {res.get('transport')} · can write: {res.get('can_write')}"
               f" · path form: {res.get('path_form')} · batch max: {res.get('batch_max')}")
    for r in (res.get("rows") or []):
        if r.get("to_link"):
            click.echo(f"  {r['citation_key']}  ({r['to_link']} to link)")


@zotlink_group.command("attach")
@click.option("--keys", default="", help="Comma-separated citation keys.")
@click.option("--keys-file", type=click.Path(exists=True, dir_okay=False),
              default=None, help="File with one citation key per line.")
@click.option("--dry-run", is_flag=True, help="Report the plan; write nothing.")
@click.option("--acknowledge-plugins", is_flag=True,
              help="I have disabled the auto-rename/move of any installed Zotero "
                   "attachment plugin (Attanger, ZotFile, ZotMoov).")
def zotlink_attach_cmd(keys: str, keys_file, dry_run: bool, acknowledge_plugins: bool):
    """Create linked-file attachments in Zotero for the records' mapped PDFs.

    Absolute vault paths; personal library only (group records are skipped);
    a PDF Zotero already links is skipped. Undo: Tagging → Restore, or the
    Zotero Attachments tab's remove verb.
    """
    selection = _read_key_selection(keys, keys_file)
    if selection is None:
        raise click.UsageError("--keys or --keys-file is required (never the whole library).")

    def _printer(frame):
        if isinstance(frame, dict) and "info" in frame:
            click.echo(frame["info"])

    res = zotlink_attach(sorted(selection), dry_run=dry_run,
                         acknowledge_plugins=acknowledge_plugins, on_event=_printer)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@zotlink_group.command("repoint-candidates")
@click.option("--item-key", required=True, help="The attachment's Zotero key.")
@click.option("--query", default="", help="Narrow the list by substring.")
@click.option("--json-out", is_flag=True, help="Print the full JSON payload.")
def zotlink_repoint_candidates_cmd(item_key: str, query: str, json_out: bool):
    """Biblio PDFs that could be the new home of a broken linked attachment."""
    res = zotlink_repoint_candidates(item_key, query)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(2 if res["error"] == _SERVER_DOWN_MSG else 1)
    if json_out:
        click.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    click.echo(f"Zotero points at: {res.get('stored_path')}"
               f"  ({'exists' if res.get('stored_exists') else 'MISSING'})")
    click.echo(f"Biblio PDFs considered: {res.get('total')}")
    for c in (res.get("candidates") or []):
        click.echo(f"  {c['score']:.3f}  {c['rel']}")


@zotlink_group.command("repoint")
@click.option("--item-key", required=True, help="The attachment's Zotero key.")
@click.option("--rel", required=True,
              help="Target PDF, relative to the bibliography folder.")
def zotlink_repoint_cmd(item_key: str, rel: str):
    """Point one linked attachment at a different file (reversible from Restore)."""
    res = zotlink_repoint(item_key, rel)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(2 if res["error"] == _SERVER_DOWN_MSG else 1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cli.group("maintain")
def maintain_group():
    """Vault maintenance steps (the maintain.sh replacement).

    Private-only build feature: on a public build the endpoints are absent and
    every subcommand exits 1 with "not available in this build". Use
    ``maintain list`` to review which opt-in steps launch external tools,
    reach the network/model, or write data.
    """
    pass


@maintain_group.command("list")
@click.option("--json-out", is_flag=True, help="Output raw JSON")
def maintain_list_cmd(json_out: bool):
    """List the registered maintenance steps and their availability."""
    res = maintenance_steps()
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(2 if res["error"].startswith("The CHERQAiN server") else 1)
    if json_out:
        click.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    for step in res.get("steps", []):
        mark = "on " if step.get("default_on") else "off"
        avail = "" if step.get("available", True) else \
            f"  [unavailable: {step.get('reason')}]"
        click.echo(f"  {step.get('id'):<14} [{mark}] {step.get('kind'):<8} "
                   f"{step.get('hint')}{avail}")


@maintain_group.command("run")
@click.option("--only", default="", help="Comma-separated step ids "
                                         "(default: all default-on steps)")
@click.option("--apply", "apply_mode", is_flag=True,
              help="Confirm write/external effects; dry-run options default off.")
@click.option("--preview", is_flag=True,
              help="Confirm external/model/network preview; keep supported steps dry-run.")
@click.option("--option", "raw_options", multiple=True,
              metavar="STEP.KEY=VALUE",
              help="Set a registry option; repeat for multiple values.")
@click.option("--json-out", is_flag=True, help="Output raw JSON")
def maintain_run_cmd(only: str, apply_mode: bool, preview: bool,
                     raw_options: tuple[str, ...], json_out: bool):
    """Run maintenance steps against the vault.

    Exit codes preserve the maintain.sh gate: 0 = every step passed,
    1 = findings / a step failed / cancelled, 2 = setup error (no server).
    """
    if apply_mode and preview:
        raise click.UsageError("Choose either --apply or --preview, not both.")
    wanted = [s.strip() for s in only.split(",") if s.strip()] or None
    # Always read the registry before spending: it is the source of selection,
    # effect classification, and option parsing for all CLI modes.
    listing = maintenance_steps()
    if "error" in listing:
        click.echo(f"Error: {listing['error']}", err=True)
        sys.exit(2 if listing["error"].startswith("The CHERQAiN server") else 1)
    rows = listing.get("steps", [])
    by_id = {s.get("id"): s for s in rows}
    selected_ids = wanted or [s.get("id") for s in rows if s.get("default_on")]
    unknown = [s for s in selected_ids if s not in by_id]
    if unknown:
        raise click.UsageError(
            f"Unknown step id(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(by_id))}.")
    effectful = [by_id[s] for s in selected_ids
                 if by_id[s].get("kind") != "read"]
    if effectful and not (apply_mode or preview):
        names = ", ".join(s["id"] for s in effectful)
        raise click.UsageError(
            f"Step(s) {names} write data or invoke external services. "
            "Use --preview for a no-write preview where supported, or "
            "--apply to authorize effects.")

    parsed_options: dict[str, dict[str, Any]] = {}
    for raw in raw_options:
        lhs, sep, value = raw.partition("=")
        step_id, dot, key = lhs.partition(".")
        if not sep or not dot or not step_id or not key:
            raise click.UsageError(
                f"Invalid --option {raw!r}; expected STEP.KEY=VALUE.")
        if step_id not in selected_ids:
            raise click.UsageError(
                f"Option group {step_id!r} is not a selected step.")
        specs = {s.get("key"): s for s in by_id[step_id].get("options", [])}
        spec = specs.get(key)
        if spec is None:
            raise click.UsageError(f"Unknown option {step_id}.{key}.")
        parsed: Any
        if spec.get("type") == "bool":
            folded = value.strip().lower()
            if folded not in ("true", "false"):
                raise click.UsageError(
                    f"{step_id}.{key} must be true or false.")
            parsed = folded == "true"
        elif spec.get("type") == "int":
            try:
                parsed = int(value)
            except ValueError as exc:
                raise click.UsageError(
                    f"{step_id}.{key} must be an integer.") from exc
        else:
            parsed = value
        parsed_options.setdefault(step_id, {})[key] = parsed

    # --preview/--apply decide the supported dry-run knobs consistently. The
    # server still validates every value and remains the authority; this only
    # gives the CLI a safe, legible default.
    for step_id in selected_ids:
        specs = {s.get("key"): s for s in by_id[step_id].get("options", [])}
        if "dry_run" in specs and (apply_mode or preview):
            parsed_options.setdefault(step_id, {})["dry_run"] = not apply_mode

    def _printer(ev):
        if json_out:
            return
        if "info" in ev:
            click.echo(ev["info"])
        elif "step" in ev:
            s = ev["step"]
            mark = "OK " if s.get("ok") else "FAIL"
            click.echo(f"[{mark}] {s.get('id')}: {s.get('summary')}")
            for f in (s.get("findings") or [])[:25]:
                detail = f" — {f['detail']}" if f.get("detail") else ""
                click.echo(f"    - {f.get('label')}{detail}")
            n_findings = len(s.get("findings") or [])
            if n_findings > 25:
                click.echo(f"    … and {n_findings - 25} more findings")
            for line in s.get("detail_lines") or []:
                click.echo(f"    {line}")

    res = maintenance_run(
        wanted, on_event=_printer, options=parsed_options or None,
        confirm=bool(apply_mode or preview))
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(2 if res["error"].startswith("The CHERQAiN server") else 1)
    if json_out:
        click.echo(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        if res.get("cancelled"):
            click.echo("Run cancelled before completion.")
        elif res.get("ok"):
            click.echo(f"All {len(res.get('ran', []))} step(s) passed.")
        else:
            failed = sorted(k for k, v in (res.get("results") or {}).items()
                            if not v)
            click.echo(f"Findings in: {', '.join(failed) or '(run incomplete)'}",
                       err=True)
    sys.exit(0 if res.get("ok") else 1)


# ---------------------------------------------------------------------------
# Biblio Tagging
# ---------------------------------------------------------------------------


@cli.group("tagging")
def tagging_group():
    """Biblio article tagging operations."""
    pass


# The batch loop's constants moved to tagging/batch.py with the loop itself
# (Maintenance Phase 2 extraction) — aliased here so anything addressing them
# by the historical cli.main names keeps resolving.
from tagging.batch import (  # noqa: E402,F401 — re-exported aliases
    MAX_BACKOFF_S as _MAX_BACKOFF_S,
    MAX_CONSECUTIVE_FAILURES as _MAX_CONSECUTIVE_FAILURES,
    STALE_SNAPSHOT_MIN as _STALE_SNAPSHOT_MIN,
)


def _read_key_selection(keys: str, keys_file) -> set | None:
    """Union of ``--keys`` and ``--keys-file`` into a set, or ``None`` if unset.

    ``None`` (neither given) means "no subset selection" — the whole scope.
    An EMPTY selection is a different thing and is refused: a ``--keys-file``
    that is empty, all comments, or the wrong file is a mistake, and silently
    treating it as "process everything" would run the whole library when the
    user asked for a handful of papers (the empty-vs-unset distinction that
    already bit `--targets` on the write path).

    The file format is one key per line with ``#`` comments, so the picker's
    export can be edited by hand and commented without breaking.
    """
    selected: set[str] = set()
    for chunk in (keys or "").split(","):
        k = chunk.strip()
        if k:
            selected.add(k)
    if keys_file:
        from pathlib import Path as _P

        try:
            # utf-8-sig: an exported/re-saved file often carries a BOM, which
            # plain utf-8 leaves glued (invisibly) to the first key — it then
            # matched nothing and the diagnostic printed it unreadably.
            text = _P(keys_file).read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise click.UsageError(
                f"--keys-file is not UTF-8 text ({exc.reason} at byte "
                f"{exc.start}). Re-save it as UTF-8 — the picker's "
                "'Copy keys' export is the expected source.")
        for line in text.splitlines():
            k = line.split("#", 1)[0].strip()
            if k:
                selected.add(k)
    if not keys and not keys_file:
        return None
    if not selected:
        raise click.UsageError(
            "The key selection is empty (no keys in --keys / --keys-file). "
            "Omit both to process the whole scope.")
    return selected


def _parse_targets(raw: str) -> list:
    """Split and validate a ``--targets`` value, or raise ``UsageError``.

    An unrecognised name used to survive all the way to the wire, where the
    driver maps every REAL target to false — so the command wrote nothing,
    reported success and exited 0 after paying for the LLM pass. A name is an
    argument shape, so rejecting it here matches the module's validation policy.
    """
    from core.constants import TAGGING_TARGETS

    parsed = [t.strip() for t in raw.split(",") if t.strip()]
    unknown = [t for t in parsed if t not in TAGGING_TARGETS]
    if unknown:
        raise click.UsageError(
            f"Unknown target(s): {', '.join(unknown)}. "
            f"Valid targets: {', '.join(TAGGING_TARGETS)}.")
    if not parsed:
        raise click.UsageError(
            f"--targets needs at least one of: {', '.join(TAGGING_TARGETS)}.")
    return parsed


@tagging_group.command("pdf-links")
@click.option("--keys", default="", help="Comma-separated citation keys.")
@click.option("--keys-file", type=click.Path(exists=True, dir_okay=False),
              default=None, help="File with one citation key per line.")
def tagging_pdf_links_cmd(keys: str, keys_file):
    """Write each record's mapped PDFs into its note as a `pdf:` wikilink list."""
    selection = _read_key_selection(keys, keys_file)
    if selection is None:
        # `None` means "no subset selection", which for every other command
        # reads as the whole scope. There is no whole-library meaning here:
        # the route is bounded by what the restore journal can undo, so an
        # unset selection must be a refusal, not a library-wide write.
        raise click.UsageError("--keys or --keys-file is required.")
    click.echo(f"pdf-links: {len(selection)} record(s) ...")
    res = tagging_pdf_links(sorted(selection))
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(2 if res["error"] == _SERVER_DOWN_MSG else 1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@tagging_group.command("propose")
@click.argument("citation_key")
def tagging_propose_cmd(citation_key: str):
    """Propose tags for a paper from vault vocabulary."""
    click.echo(f"Proposing tags for {citation_key}...")
    res = tagging_propose(citation_key)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@tagging_group.command("apply")
@click.argument("citation_key")
@click.option("--tags", required=True, help="Comma-separated list of tags")
@click.option("--targets", default="note,finder,zotero", help="Comma-separated targets")
def tagging_apply_cmd(citation_key: str, tags: str, targets: str):
    """Apply tags to frontmatter, Finder tags, or Zotero item."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    target_list = _parse_targets(targets)
    click.echo(f"Applying tags {tag_list} to {citation_key} on targets {target_list}...")
    res = tagging_apply(citation_key, tag_list, target_list)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@tagging_group.command("batch")
@click.option("--limit", type=int, default=0,
              help="Stop after N papers (0 = no limit).")
@click.option("--dry-run", is_flag=True,
              help="Propose only; write nothing. Genuinely write-free — run this first.")
@click.option("--targets", default="note,finder,zotero",
              help="Comma-separated targets to write.")
@click.option("--with-pdf/--no-with-pdf", "pdf_orphans", default=True,
              help="Include papers that DO have a local PDF (default: yes). "
                   "Turning it off requires --no-pdf-entries.")
@click.option("--no-pdf-entries", is_flag=True,
              help="Also process entries with no local PDF, classified from "
                   "their Zotero abstract + bibliographic metadata.")
@click.option("--min-conf", type=float, default=0.0,
              help="Skip proposed tags below this confidence.")
@click.option("--sleep", type=float, default=1.0,
              help="Seconds between papers (be kind to the provider).")
@click.option("--keys", default="",
              help="Only these citation keys (comma-separated). Combine with "
                   "--keys-file; the two are unioned.")
@click.option("--keys-file", type=click.Path(exists=True, dir_okay=False),
              default=None,
              help="File of citation keys, one per line (# comments and blank "
                   "lines ignored). Pairs with the picker's exported list.")
@click.option("--include-tagged", is_flag=True,
              help="Process rows even when every requested target already "
                   "carries tags. Requires an explicit --keys/--keys-file "
                   "selection.")
@click.option("--min-tags", type=int, default=None,
              help="Only papers carrying at least this many tags across the "
                   "requested targets they have.")
@click.option("--max-tags", type=int, default=None,
              help="Only papers carrying at most this many tags. "
                   "`--max-tags 2` is the under-tagged cohort.")
def tagging_batch_cmd(limit, dry_run, targets, pdf_orphans, no_pdf_entries,
                      min_conf, sleep, keys, keys_file, include_tagged,
                      min_tags, max_tags):
    """Propose and apply tags across the library.

    Walks the tagging inventory, skips anything already tagged on every
    REQUESTED target it has, and for the rest proposes tags and applies them
    through the same confirm-gated, journalled, vocabulary-validated endpoints
    the GUI uses.

    RESUMING NEEDS A FRESH SCAN. "Already tagged" is read from the audit scan
    SNAPSHOT, and no tagging write updates that snapshot — so re-running
    straight after an interrupted batch re-proposes every paper it just tagged
    (a full PDF-less pass is ~1150 LLM calls, and the writes are reversible but
    the spend is not). Run `cherqain audit scan`, let it finish, then re-run.
    The snapshot's age is printed at start.

    --dry-run writes nothing to the vault, the journal, Zotero or Finder. It
    does still call the model, and propose writes regenerable server-side
    caches (extracted PDF text, `.notext` markers, gloss embeddings).

    --include-tagged drops the already-tagged gate so an UNDER-tagged paper
    (one tag where it should have five) can be re-proposed. It requires an
    explicit --keys/--keys-file selection; re-proposing is safe because the
    prompt never shows the model the paper's own app-written tags and
    apply-all unions, so the existing tag survives.

    --min-tags/--max-tags select by tag DEPTH instead, counting the union
    across the requested targets a paper HAS. Either one replaces the
    already-tagged gate with the cohort it names, and needs no key list
    because the range is bounded by construction: `--max-tags 2` is the
    under-tagged cohort, `--max-tags 0` is exactly the untagged one.
    """
    # `--no-with-pdf` alone selected rows with has_pdf False out of a
    # `with_pdf` scope, i.e. always nothing — reported as a cheerful
    # "To process: 0" rather than as the contradiction it is. The flag pair was
    # also named `--pdf-orphans/--no-pdf-orphans`, which reads as a synonym of
    # `--no-pdf-entries` while meaning the opposite (one INCLUDES PDF-less
    # entries, the other restricts TO them).
    if not pdf_orphans and not no_pdf_entries:
        raise click.UsageError(
            "--no-with-pdf excludes every row in the default scope. "
            "Add --no-pdf-entries to work on PDF-less entries only.")

    target_list = _parse_targets(targets)
    # Parsed with the other ARGUMENT validation, before any network call: a
    # malformed selection is a usage error, and `_parse_targets` sets the
    # module's policy of rejecting those before paying for anything.
    wanted_keys = _read_key_selection(keys, keys_file)
    # An UNSET selection is never "everything" (the `_parse_targets` /
    # `_read_key_selection` posture): --include-tagged drops the
    # already-tagged gate, so without an explicit selection it would
    # re-propose — and re-pay for — every paper in the library.
    if include_tagged and wanted_keys is None:
        raise click.UsageError(
            "--include-tagged needs an explicit --keys/--keys-file selection: "
            "without one it would re-propose every paper in the library.")
    # Argument-shape validation before the inventory fetch — the _parse_targets
    # policy. A negative or inverted range is a typo, and a typo that silently
    # selects nothing reads as "everything was already tagged".
    for name, val in (("--min-tags", min_tags), ("--max-tags", max_tags)):
        if val is not None and val < 0:
            raise click.UsageError(f"{name} must be zero or greater.")
    if (min_tags is not None and max_tags is not None and min_tags > max_tags):
        raise click.UsageError(
            f"--min-tags ({min_tags}) is greater than --max-tags ({max_tags}), "
            "which selects nothing.")
    if include_tagged and (min_tags is not None or max_tags is not None):
        raise click.UsageError(
            "--include-tagged and --min-tags/--max-tags are two different "
            "selections; use one or the other.")

    # The loop itself lives in tagging/batch.py (Maintenance Phase 2
    # extraction) — this command binds the DRIVER transports, resolved from
    # this module's namespace at call time so the tests' `cli.main.tagging_*`
    # patches keep applying below the seam they always did.
    from tagging.batch import BatchOptions, run_batch

    opts = BatchOptions(
        limit=limit, dry_run=dry_run, targets=tuple(target_list),
        pdf_orphans=pdf_orphans, no_pdf_entries=no_pdf_entries,
        min_conf=min_conf, sleep_s=sleep,
        wanted_keys=frozenset(wanted_keys) if wanted_keys is not None else None,
        include_tagged=include_tagged,
        min_tags=min_tags, max_tags=max_tags,
    )

    def _echo(msg, err=False):
        click.echo(msg, err=err)

    outcome = run_batch(
        opts,
        inventory=lambda **kw: tagging_inventory(**kw),
        propose=lambda key: tagging_propose(key),
        apply=lambda key, tags, targets: tagging_apply(key, tags, targets),
        # tagging_model_info reads /api/config ONLY — never get_status(), whose
        # /api/obsidian/status GET destructively drains the one-shot vault
        # status feed the GUI polls (2026-08-18 review).
        model_info=lambda: tagging_model_info(),
        echo=_echo,
    )
    if outcome.error is not None or outcome.failed:
        sys.exit(1)


@tagging_group.command("restore-batch")
@click.argument("batch_id")
@click.option("--confirm", is_flag=True, help="Required.")
def tagging_restore_batch_cmd(batch_id: str, confirm: bool):
    """Reverse every applied op of one batch (e.g. a record merge), newest first."""
    _require_confirm(confirm, "tagging restore-batch")
    _emit(_drv.tagging_restore_batch(batch_id, on_event=_info_printer))


@tagging_group.command("restore")
@click.argument("op_id")
def tagging_restore_cmd(op_id: str):
    """Reverse one journalled tagging write by its op id."""
    res = tagging_restore(op_id)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Notes Sync
# ---------------------------------------------------------------------------


@cli.group("notesync")
def notesync_group():
    """Zotero ⇄ Obsidian notes: status and explicitly reviewed campaigns."""


@notesync_group.command("status")
@click.option("--json", "json_out", is_flag=True, help="Machine-readable output")
@click.option("--state", default="", help="Only rows in this state (e.g. conflict)")
def notesync_status_cmd(json_out: bool, state: str):
    """Which notes are in step, need a pull, a push, or a merge.

    Reads Zotero fresh. Prepare selected campaigns with notesync plan; inspect
    incomplete results before recovery. Campaign application remains gated.
    """
    res = notesync_status()
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(2 if res["error"].startswith("The CHERQAiN server") else 1)
    rows = res.get("rows", [])
    if state:
        rows = [r for r in rows if r.get("state") == state]
    if json_out:
        click.echo(json.dumps({**res, "rows": rows}, indent=2, ensure_ascii=False))
        return
    counts = res.get("counts", {})
    click.echo("Two-way sync: " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
               + f"  (of {res.get('total', 0)})")
    for r in rows:
        if r.get("state") == "in_sync":
            continue
        click.echo(f"  {r.get('citation_key'):<48} {r.get('state'):<16} "
                   f"pull {r.get('missing_words', 0):>4}  yours {r.get('extra_words', 0):>4}")
    sys.exit(1 if any(r.get("state") == "conflict" for r in rows) else 0)


def _campaign_output(result):
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    if 'error' in result or result.get('ok') is False:
        raise click.exceptions.Exit(1)


@notesync_group.command('plan')
@click.option('--key', 'keys', multiple=True, required=True, help='Exact citation key; repeat for a selected batch (maximum 100).')
def notesync_plan_cmd(keys):
    """Prepare a durable deterministic merge campaign; writes no notes."""
    # The server's 1–100 bound, checked before any request (the
    # _parse_targets policy; 2026-09-17 review, E1).
    if len(set(keys)) > 100:
        raise click.UsageError(f"--key accepts at most 100 distinct citation keys; {len(set(keys))} given.")
    _campaign_output(notesync_campaign('plan', data={'citation_keys': list(keys)}))


@notesync_group.command('review')
@click.argument('identifier')
def notesync_review_cmd(identifier):
    """Read exact proposed texts, evidence and recorded outcomes."""
    _campaign_output(notesync_campaign('review', identifier=identifier))


@notesync_group.command('inspect')
@click.argument('identifier')
def notesync_inspect_cmd(identifier):
    """Read current states alongside the journal; never replay writes."""
    _campaign_output(notesync_campaign('inspect', identifier=identifier))


@notesync_group.command('apply', hidden=True)
@click.option('--plan', 'identifier', required=True, help='Durable campaign identifier.')
@click.option('--decisions', type=click.File('r'), required=True, help='JSON list: citation_key, evidence, approve:true, edited result.')
@click.option('--confirm', is_flag=True, required=True, help='Apply only the exact approved selection to both notes.')
def notesync_apply_cmd(identifier, decisions, confirm):
    """Apply selected reviewed results through the guarded server writer (disabled in production)."""
    try:
        selected = json.load(decisions)
    except ValueError as exc:
        raise click.ClickException('Invalid decisions JSON.') from exc
    if not isinstance(selected, list) or not 1 <= len(selected) <= 100:
        raise click.UsageError('--decisions must be a JSON list of 1–100 decisions.')
    _campaign_output(notesync_campaign('apply', identifier=identifier,
                                      data={'decisions': selected, 'confirm': confirm}))


@notesync_group.command('settle', hidden=True)
@click.option('--plan', 'identifier', required=True, help='Previously dispatched campaign identifier.')
@click.option('--decision', type=click.File('r'), required=True,
              help='JSON object: citation_key, current evidence, outcome_evidence and original approved result from inspect.')
@click.option('--confirm', is_flag=True, required=True, help='Record only an inspected, fully evidenced completed dispatch.')
def notesync_settle_cmd(identifier, decision, confirm):
    """Record a dispatch that crashed after saving its base; writes nothing (disabled in production).

    Inspect must report the outcome as settleable: both notes, the base record
    and the journals must describe the approved result. New edits, a base from
    another operation or missing journal evidence keep the outcome retained.
    """
    try:
        selected = json.load(decision)
        if not isinstance(selected, dict):
            raise ValueError('Expected an object.')
    except ValueError as exc:
        raise click.ClickException('Invalid settlement decision JSON object.') from exc
    _campaign_output(notesync_campaign('settle', identifier=identifier,
                                      data=dict(selected, confirm=confirm)))


@notesync_group.command('undo', hidden=True)
@click.option('--plan', 'identifier', required=True, help='Campaign whose application succeeded on both notes.')
@click.option('--decision', type=click.File('r'), required=True,
              help='JSON object: citation_key and the outcome_evidence reported by inspect.')
@click.option('--confirm', is_flag=True, required=True, help='Restore both before-images and the pre-campaign base.')
def notesync_undo_cmd(identifier, decision, confirm):
    """Undo one applied campaign note on BOTH destinations; admission stays closed (disabled in production).

    Admissible only while both notes still carry the exact written image: the
    Obsidian note hashes to the journalled after-image and the Zotero note holds
    the written HTML at the written version. Any later edit on either side
    refuses the undo. An undo that completes on one destination only is
    retained as undo_incomplete and never retried.
    """
    try:
        selected = json.load(decision)
        if not isinstance(selected, dict):
            raise ValueError('Expected an object.')
    except ValueError as exc:
        raise click.ClickException('Invalid undo decision JSON object.') from exc
    _campaign_output(notesync_campaign('undo', identifier=identifier,
                                      data={**selected, 'confirm': confirm}))


@notesync_group.command('recover', hidden=True)
@click.option('--plan', 'identifier', required=True, help='Previously dispatched campaign identifier.')
@click.option('--decision', type=click.File('r'), required=True,
              help='JSON object: citation_key, current evidence, outcome_evidence and original approved result from inspect.')
@click.option('--confirm', is_flag=True, required=True, help='Explicitly complete this exact inspected result in both notes.')
def notesync_recover_cmd(identifier, decision, confirm):
    """Complete an inspected partial merge; production admission remains closed (disabled in production).

    Never recreates a note or blindly retries an unknown write. Inspection alone
    does not settle journals or advance a base. Native/human acceptance is required.
    """
    try:
        selected = json.load(decision)
        if not isinstance(selected, dict):
            raise ValueError('Expected an object.')
    except ValueError as exc:
        raise click.ClickException('Invalid recovery decision JSON object.') from exc
    _campaign_output(notesync_campaign('recover', identifier=identifier,
                                      data={**selected, 'confirm': confirm}))


@cli.group('library')
def library_group():
    """Deterministic relationship checks and source-based review proposals."""


@library_group.command('check')
def library_check_cmd():
    """Read deterministic checks over saved observations; no implicit scan.

    Saved-only files and unjoined notes remain review entries, not verified
    absences or write authorizations. Historical decisions are preserved.
    """
    _campaign_output(library_campaign('checks'))


@library_group.command('links')
def library_links_cmd():
    """Inspect current PDF links in known scan notes; no vault walk or writes."""
    _campaign_output(library_campaign('links'))


@library_group.command('storage')
def library_storage_cmd():
    """Zotero storage control: stored, misplaced, broken, relative or duplicate attachment paths.

    Read-only over the saved Zotero observation; each finding carries the manual
    correction to make in Zotero or Attanger. Nothing is converted or written.
    """
    _campaign_output(library_campaign('storage'))


@library_group.command('review')
@click.argument('identifier')
def library_review_cmd(identifier):
    """Read a durable campaign, proposals and outcomes."""
    _campaign_output(notesync_campaign('review', identifier=identifier))


@library_group.command('bibliography-decide')
@click.option('--plan', 'identifier', required=True)
@click.option('--decisions', type=click.File('r'), required=True, help='JSON list: record_id, field, evidence, approve boolean; approvals also require support_assessment:supported and reason. Optional reviewed_value records an edited value; no metadata is applied.')
@click.option('--confirm', is_flag=True, required=True)
def library_decide_cmd(identifier, decisions, confirm):
    """Record an exact field selection; never applies Zotero metadata."""
    try:
        selected = json.load(decisions)
    except ValueError as exc:
        raise click.ClickException('Invalid decisions JSON.') from exc
    _campaign_output(library_campaign('decisions', {'decisions': selected, 'confirm': confirm}, identifier))


@library_group.command('authenticate')
@click.option('--input', 'records_file', type=click.File('r'), required=True,
              help='JSON list: id, pdf (relative to the bibliography folder) and current fields (DOI, title, creators).')
def library_authenticate_cmd(records_file):
    """Locate each record's DOI, title and authors in its own PDF pages; no model, no network.

    A DOI printed only in the reference list is reported as reference_list_only:
    a cited paper, not this document. Every location carries a page, offset,
    verbatim excerpt and passage fingerprint. Nothing is written.
    """
    try:
        records = json.load(records_file)
    except ValueError as exc:
        raise click.ClickException('Invalid records JSON.') from exc
    _campaign_output(library_campaign('authenticate', {'records': records}))


@library_group.command('bibliography-apply', hidden=True)
@click.option('--plan', 'identifier', required=True)
@click.option('--decisions', type=click.File('r'), required=True,
              help='JSON list: record_id, field, evidence — each must be an approved field of the recorded review.')
@click.option('--confirm', is_flag=True, required=True, help='Write exactly these approved fields to Zotero.')
def library_apply_cmd(identifier, decisions, confirm):
    """Write approved fields to Zotero; returns 501 in production pending validation.

    Each field is written through the shared transport under the version guard,
    journalled before the PATCH and reversible from Tagging → Restore. A field
    whose current value changed since review is refused; a write that would move
    an unselected field (Zotero promotes an Extra 'Citation Key:' line into the
    native citationKey) is refused before the PATCH, and any unselected change
    observed after it is recorded on the journal op, never counted as success.
    """
    try:
        selected = json.load(decisions)
    except ValueError as exc:
        raise click.ClickException('Invalid decisions JSON.') from exc
    _campaign_output(library_campaign('apply', {'decisions': selected, 'confirm': confirm}, identifier))


@library_group.command('bibliography-plan')
@click.option('--input', 'corpus_file', type=click.File('r'), required=True, help='JSON corpus: id, current fields, sources with id/locator/text, optional claim and pages.')
@click.option('--provider', required=True, help='Explicit local or API provider for this campaign.')
@click.option('--model', required=True, help='Explicit model for this campaign; no fallback.')
@click.option('--max-tokens', type=click.IntRange(128, 8192), required=True, help='Output token budget for one request; input capped at 60,000 characters.')
@click.option('--resolve-dois', is_flag=True, help='Also send up to five explicit DOI values to Crossref before analysis; registry evidence only.')
def bibliography_plan_cmd(corpus_file, provider, model, max_tokens, resolve_dois):
    """Analyze supplied sources and optional DOI metadata; never writes metadata.

    Review the returned campaign with library review ID. Crossref acquisition
    is opt-in; field support and three separate judgments need human review.
    """
    try:
        corpus = json.load(corpus_file)
    except ValueError as exc:
        raise click.ClickException('Invalid corpus JSON.') from exc
    result = library_campaign('bibliography', {'corpus': corpus, 'provider': provider,
                                               'model': model, 'max_tokens': max_tokens, 'resolve_dois': resolve_dois})
    _campaign_output(result)
    if result.get('outcomes', {}).get('generation', {}).get('status') == 'interrupted':
        raise click.exceptions.Exit(1)


# ---------------------------------------------------------------------------
# Deck & Doc Generation
# ---------------------------------------------------------------------------


@cli.group("deck")
def deck_group():
    """LaTeX Beamer slide deck generation."""
    pass


@deck_group.command("generate")
@click.argument("topic")
@click.option("--template", "template_path", required=True,
              type=click.Path(exists=True),
              help="Beamer template .tex the deck inherits (preamble/macros/bib)")
@click.option("--out-dir", default="",
              help="Output folder (default: the template's suite root)")
@click.option("--name", "deck_name", default="", help="Deck folder/slug name")
@click.option("--instructions", default="", help="Free-form prompt instructions")
@click.option("--overwrite", is_flag=True, help="Overwrite an existing deck folder")
def deck_generate_cmd(topic: str, template_path: str, out_dir: str,
                      deck_name: str, instructions: str, overwrite: bool):
    """Generate a vault-grounded LaTeX Beamer deck (emit-only scaffold)."""
    click.echo(f"Generating Beamer deck on topic '{topic}'...")
    res = generate_deck(topic, template_path=template_path, out_dir=out_dir,
                        deck_name=deck_name, instructions=instructions,
                        overwrite=overwrite)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cli.group("docgen")
def docgen_group():
    """LaTeX Article or Vault Markdown document generation."""
    pass


@docgen_group.command("generate")
@click.argument("topic")
@click.option("--instructions", default="", help="Free-form prompt instructions")
@click.option("--mode", default="markdown", type=click.Choice(["markdown", "latex"]))
@click.option("--template", "template_path", default="",
              type=click.Path(),
              help="Article template .tex (REQUIRED for --mode latex)")
@click.option("--out-dir", default="",
              help="Output folder for latex mode (default: template suite root)")
def docgen_generate_cmd(topic: str, instructions: str, mode: str,
                        template_path: str, out_dir: str):
    """Generate a vault-grounded Markdown note or LaTeX article."""
    if mode == "latex" and not template_path:
        click.echo("Error: --template is required for --mode latex.", err=True)
        sys.exit(1)
    click.echo(f"Generating document (mode={mode}) on topic '{topic}'...")
    res = generate_doc(topic, instructions=instructions, mode=mode,
                       template_path=template_path, out_dir=out_dir)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    if mode == "markdown":
        click.echo("\n--- GENERATED MARKDOWN NOTE ---")
        click.echo(res.get("document"))
    else:
        click.echo("\n--- GENERATED LATEX DOCUMENT ---")
        click.echo(res.get("document"))


# ---------------------------------------------------------------------------
# MCP Server Command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Annual library cleanup T1 (2026-09-17): GUI-only routes, now headless
# ---------------------------------------------------------------------------
#
# Every write command below demands an explicit `--confirm` from the person
# running it; the driver forwards `confirm: true` only then. Reads print the
# server's JSON verbatim so the cleanup harness and a human see the same thing.

from core import driver as _drv  # noqa: E402


def _emit(res: dict) -> None:
    """Print a driver result as JSON; exit 2 when no server, 1 on refusal."""
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))
    err = res.get("error")
    if err:
        sys.exit(2 if err == _SERVER_DOWN_MSG else 1)
    if res.get("ok") is False:
        sys.exit(1)


def _require_confirm(confirm: bool, what: str) -> None:
    if not confirm:
        raise click.UsageError(f"{what} writes; re-run with --confirm after a preview/dry run.")


def _load_json_file(path, what: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise click.UsageError(f"{what}: cannot read JSON ({exc}).")


@audit_group.command("status")
def audit_status_cmd():
    """The audit scan's lifecycle state (idle/scanning/done/error/cancelled)."""
    _emit(_drv.audit_status())


@audit_group.command("summary")
def audit_summary_cmd():
    """Headline counters of the cached scan."""
    _emit(_drv.audit_summary())


@audit_group.command("report", help=(
    "One reconciliation report of the cached scan, as JSON. NAME is one of: "
    + ", ".join(_drv.AUDIT_REPORT_NAMES) + "."))
@click.argument("name")
def audit_report_cmd(name: str):
    if name not in _drv.AUDIT_REPORT_NAMES:
        raise click.UsageError("Unknown report. Known: " + ", ".join(_drv.AUDIT_REPORT_NAMES))
    _emit(_drv.audit_report(name))


@audit_group.command("wait")
@click.option("--timeout", default=3600, show_default=True, help="Seconds to wait.")
@click.option("--interval", default=5.0, show_default=True, help="Seconds between polls.")
def audit_wait_cmd(timeout: int, interval: float):
    """Poll until the running scan leaves `scanning`; exit 1 unless it is `done`."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while True:
        res = _drv.audit_status()
        if res.get("error"):
            _emit(res)
        state = res.get("state")
        if state != "scanning":
            click.echo(json.dumps({"state": state, "error": res.get("error"),
                                   "finished_at": res.get("finished_at"),
                                   "has_results": res.get("has_results")}, indent=2))
            sys.exit(0 if state == "done" else 1)
        if _time.monotonic() >= deadline:
            click.echo(f"Still scanning after {timeout}s.", err=True)
            sys.exit(1)
        _time.sleep(interval)


@audit_group.command("dup-decision")
@click.option("--pair-ids", default="", help="Comma-separated near-duplicate pair ids.")
@click.option("--pair-ids-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="File with one pair id per line (# comments allowed).")
@click.option("--dismiss", "verb_dismiss", is_flag=True, help="Mark the groups 'not a duplicate'.")
@click.option("--clear", "verb_clear", is_flag=True, help="Undo the dismissal.")
def audit_dup_decision_cmd(pair_ids, pair_ids_file, verb_dismiss, verb_clear):
    """Dismiss / undo near-duplicate groups in one atomic write."""
    if verb_dismiss == verb_clear:
        raise click.UsageError("Choose exactly one of --dismiss, --clear.")
    selection = _read_key_selection(pair_ids, pair_ids_file)
    if selection is None:
        raise click.UsageError("--pair-ids or --pair-ids-file is required.")
    _emit(_drv.audit_dup_decision("dismiss" if verb_dismiss else "clear", sorted(selection)))


@audit_group.command("pdf-facts")
@click.argument("pdfs", nargs=-1, required=True)
def audit_pdf_facts_cmd(pdfs):
    """Read-only evidence for up to 50 biblio PDFs (vault-relative paths)."""
    _emit(_drv.audit_pdf_facts(list(pdfs)))


@cli.group("pdfs")
def pdfs_group():
    """Manage PDFs: recoverable cleanup, link and Zotero trash per entry."""
    pass


@pdfs_group.command("entry")
@click.argument("citation_key")
def pdfs_entry_cmd(citation_key: str):
    """An entry's PDFs with annotation counts, note dependencies and operations."""
    _emit(_drv.pdfs_entry(citation_key))


@pdfs_group.command("candidates")
@click.option("--query", default="", help="Filename substring.")
def pdfs_candidates_cmd(query: str):
    """Bibliography PDFs by name (at most 100)."""
    _emit(_drv.pdfs_candidates(query))


def _pdfs_fields(action, survivor, unmap, delete, pdf, attachment,
                 acknowledge_annotations, accept_annotation_loss, acknowledge_plugins):
    if action == "cleanup":
        actions = {rel: "unmap" for rel in unmap}
        for rel in delete:
            if rel in actions:
                raise click.UsageError(f"{rel} is both --unmap and --delete.")
            actions[rel] = "delete"
        if not survivor or not actions:
            raise click.UsageError("cleanup needs --survivor and at least one --unmap/--delete.")
        return {"survivor": survivor, "actions": actions,
                "acknowledge_annotations": True if acknowledge_annotations else None}
    if action == "link":
        if not pdf:
            raise click.UsageError("link needs --pdf.")
        return {"pdf": pdf, "acknowledge_plugins": True if acknowledge_plugins else None}
    if not attachment or not survivor:
        raise click.UsageError("trash needs --attachment and --survivor.")
    return {"attachment": attachment, "survivor": survivor,
            "accept_annotation_loss": True if accept_annotation_loss else None}


def _pdfs_options(fn):
    for opt in reversed([
        click.option("--key", "citation_key", required=True, help="Citation key of the entry."),
        click.option("--action", type=click.Choice(sorted(_drv.PDFS_ACTIONS)), required=True),
        click.option("--survivor", default=None, help="Vault-relative PDF that is kept."),
        click.option("--unmap", multiple=True, help="Vault-relative PDF to unmap (repeatable)."),
        click.option("--delete", multiple=True,
                     help="Vault-relative PDF to move to recoverable storage (repeatable)."),
        click.option("--pdf", default=None, help="link: vault-relative PDF to link."),
        click.option("--attachment", default=None, help="trash: Zotero attachment key."),
        click.option("--acknowledge-annotations", is_flag=True,
                     help="cleanup: remove an annotated copy anyway."),
        click.option("--accept-annotation-loss", is_flag=True,
                     help="trash: trash an attachment carrying Zotero annotations."),
        click.option("--acknowledge-plugins", is_flag=True,
                     help="link: a file-renaming Zotero plugin is known to be disabled."),
    ]):
        fn = opt(fn)
    return fn


@pdfs_group.command("preview")
@_pdfs_options
def pdfs_preview_cmd(citation_key, action, survivor, unmap, delete, pdf, attachment,
                     acknowledge_annotations, accept_annotation_loss, acknowledge_plugins):
    """Plan an action; writes nothing. Prints the plan, blockers and token."""
    fields = _pdfs_fields(action, survivor, unmap, delete, pdf, attachment,
                          acknowledge_annotations, accept_annotation_loss, acknowledge_plugins)
    _emit(_drv.pdfs_preview(citation_key, action, **fields))


@pdfs_group.command("apply")
@_pdfs_options
@click.option("--token", required=True, help="The token printed by `pdfs preview`.")
@click.option("--decision-ref", default=None,
              help="cleanup: the approving decision-file row, recorded on the operation.")
@click.option("--confirm", is_flag=True, help="Required: apply the previewed plan.")
def pdfs_apply_cmd(citation_key, action, survivor, unmap, delete, pdf, attachment,
                   acknowledge_annotations, accept_annotation_loss, acknowledge_plugins,
                   token, decision_ref, confirm):
    """Apply a previewed plan exactly (refused if anything changed)."""
    _require_confirm(confirm, "pdfs apply")
    fields = _pdfs_fields(action, survivor, unmap, delete, pdf, attachment,
                          acknowledge_annotations, accept_annotation_loss, acknowledge_plugins)
    if decision_ref is not None:
        if action != "cleanup":
            raise click.UsageError("--decision-ref applies to cleanup only.")
        fields["decision_ref"] = decision_ref
    _emit(_drv.pdfs_apply(citation_key, action, token, **fields))


@pdfs_group.command("holding")
def pdfs_holding_cmd():
    """Files in recoverable storage (original path, sha256, decision) and unsettled operations."""
    _emit(_drv.pdfs_holding())


@pdfs_group.command("restore")
@click.option("--operation", required=True)
@click.option("--confirm", is_flag=True, help="Required.")
def pdfs_restore_cmd(operation: str, confirm: bool):
    """Restore a retained cleanup (moves files back) or a failed link's mapping."""
    _require_confirm(confirm, "pdfs restore")
    _emit(_drv.pdfs_restore(operation))


@pdfs_group.command("inspect")
@click.option("--operation", required=True)
def pdfs_inspect_cmd(operation: str):
    """Observe an unsettled operation; never retries a write."""
    _emit(_drv.pdfs_inspect(operation))


@pdfs_group.command("complete")
@click.option("--operation", required=True)
@click.option("--token", required=True, help="The token printed by `pdfs inspect`.")
@click.option("--confirm", is_flag=True, help="Required.")
def pdfs_complete_cmd(operation: str, token: str, confirm: bool):
    """Record the inspected outcome of an unsettled operation."""
    _require_confirm(confirm, "pdfs complete")
    _emit(_drv.pdfs_complete(operation, token))


@cli.group("accession")
def accession_group():
    """Online identification of orphan and pending PDFs."""
    pass


def _info_printer(frame):
    if isinstance(frame, dict) and "info" in frame:
        click.echo(frame["info"], err=True)


@accession_group.command("rows")
@click.option("--scope", type=click.Choice(_drv.ACCESSION_ROW_SCOPES), default="orphans",
              show_default=True,
              help="orphans (default), the curated pending bucket, or all.")
def accession_rows_cmd(scope: str):
    """Orphans and pending PDFs with their cached resolutions (no network)."""
    _emit(_drv.accession_rows(scope))


@accession_group.command("identify")
@click.option("--pdf", required=True, help="Vault-relative PDF.")
@click.option("--force", is_flag=True, help="Ignore the cached resolution.")
def accession_identify_cmd(pdf: str, force: bool):
    """Run the ladder for one PDF (queries the configured sources)."""
    _emit(_drv.accession_identify(pdf, force=force, on_event=_info_printer))


@accession_group.command("batch")
@click.option("--pdfs", default="", help="Comma-separated vault-relative PDFs.")
@click.option("--pdfs-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--force", is_flag=True, help="Ignore cached resolutions.")
@click.option("--confirm", is_flag=True, help="Required: this queries online sources.")
def accession_batch_cmd(pdfs, pdfs_file, force: bool, confirm: bool):
    """Identify a selection online (egress; progress on stderr)."""
    if not confirm:
        raise click.UsageError("accession batch queries online sources; re-run with --confirm.")
    _emit(_drv.accession_batch(_read_path_selection(pdfs, pdfs_file), force=force,
                               on_event=_info_printer))


@maintain_group.command("rename")
@click.argument("rel")
@click.argument("new_name")
@click.option("--confirm", is_flag=True, help="Required.")
def maintain_rename_cmd(rel: str, new_name: str, confirm: bool):
    """Rename one biblio PDF (REL relative to the bibliography folder)."""
    _require_confirm(confirm, "maintain rename")
    _emit(_drv.maintenance_rename(rel, new_name))


@maintain_group.command("rename-preview")
@click.argument("rels", nargs=-1)
@click.option("--rels-file", type=click.Path(exists=True, dir_okay=False),
              help="One biblio-relative PDF path per line (# comments).")
def maintain_rename_preview_cmd(rels, rels_file):
    """What `maintain rename` would do for each PDF (read-only, ≤200)."""
    selection = list(rels)
    if rels_file:
        with open(rels_file, encoding="utf-8") as fh:
            selection += [ln.strip() for ln in fh
                          if ln.strip() and not ln.lstrip().startswith("#")]
    if not selection:
        raise click.UsageError("Name at least one PDF (arguments or --rels-file).")
    _emit(_drv.maintenance_rename_preview(selection))


@notesync_group.command("legacy-status")
def notesync_legacy_status_cmd():
    """Missing notes and one-directional region update candidates."""
    _emit(_drv.notesync_legacy_status())


@notesync_group.command("preview")
@click.argument("citation_key")
def notesync_preview_cmd(citation_key: str):
    """The exact note `notesync create` would write."""
    _emit(_drv.notesync_preview(citation_key))


@notesync_group.command("create")
@click.option("--keys", default="")
@click.option("--keys-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--confirm", is_flag=True, help="Required.")
def notesync_create_cmd(keys, keys_file, confirm: bool):
    """Create missing Obsidian reference notes (≤100, journalled)."""
    _require_confirm(confirm, "notesync create")
    sel = _read_key_selection(keys, keys_file)
    if sel is None:
        raise click.UsageError("--keys or --keys-file is required.")
    _emit(_drv.notesync_create(sorted(sel)))


@notesync_group.command("update-preview")
@click.argument("citation_key")
def notesync_update_preview_cmd(citation_key: str):
    _emit(_drv.notesync_update_preview(citation_key))


@notesync_group.command("migrate")
@click.option("--undo", is_flag=True, help="Unmigrate instead.")
@click.option("--confirm", is_flag=True, help="Write; without it this is a dry run.")
def notesync_migrate_cmd(undo: bool, confirm: bool):
    """Marker-layout migration (dry run unless --confirm; needs a clean vault git)."""
    _emit(_drv.notesync_layout("unmigrate" if undo else "migrate", dry_run=not confirm))


@notesync_group.command("bootstrap")
@click.option("--keys", default="")
@click.option("--keys-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--all", "all_notes", is_flag=True, help="Every note whose sides are equal.")
@click.option("--confirm", is_flag=True, help="Required.")
def notesync_bootstrap_cmd(keys, keys_file, all_notes: bool, confirm: bool):
    """Write sync bases where both sides already agree."""
    _require_confirm(confirm, "notesync bootstrap")
    sel = _read_key_selection(keys, keys_file)
    if (sel is None) == (not all_notes):
        raise click.UsageError("Give --keys/--keys-file or --all (exactly one).")
    _emit(_drv.notesync_bootstrap(sorted(sel or ()), all_notes=all_notes))


def _pull_push_cmd(kind):
    @click.option("--keys", default="")
    @click.option("--keys-file", type=click.Path(exists=True, dir_okay=False), default=None)
    @click.option("--accept-conflict", is_flag=True,
                  help="Overwrite a side that changed since the sync base.")
    @click.option("--confirm", is_flag=True, help="Required.")
    def cmd(keys, keys_file, accept_conflict: bool, confirm: bool):
        _require_confirm(confirm, f"notesync {kind}")
        sel = _read_key_selection(keys, keys_file)
        if sel is None:
            raise click.UsageError("--keys or --keys-file is required.")
        fn = _drv.notesync_pull if kind == "pull" else _drv.notesync_push
        _emit(fn(sorted(sel), accept_conflict=accept_conflict))
    cmd.__doc__ = ("Zotero → Obsidian region write (≤100)." if kind == "pull"
                   else "Obsidian → Zotero note write (≤50, version-guarded).")
    return cmd


def _require_plan_digest(confirm: bool, plan_digest: str | None, command: str) -> None:
    """A confirm executes only the plan a dry run showed (2026-09-21, library
    workflow lot 1). Refused here, before any request, so a typo'd flag never
    reaches the server as an unbound confirm."""
    if confirm and not plan_digest:
        raise click.UsageError(
            f"{command} --confirm needs --plan-digest: run it without --confirm, "
            "review the plan, then pass the plan_digest it printed.")
    if plan_digest and not confirm:
        raise click.UsageError("--plan-digest only applies with --confirm.")


@notesync_group.command("refresh-all")
@click.option("--confirm", is_flag=True, help="Write; without it this is a dry run.")
@click.option("--plan-digest", default=None,
              help="The plan_digest printed by the dry run you reviewed.")
def notesync_refresh_all_cmd(confirm: bool, plan_digest: str | None):
    """Rewrite EVERY note's Zotero region from Zotero (zotero-authoritative
    mode). Not journalled: git must hold every note it replaces, which is the
    undo. Notes deleted in Zotero are listed, never removed. Two steps: a dry
    run prints the plan and its plan_digest; --confirm --plan-digest executes
    exactly that plan, or refuses if anything moved."""
    _require_plan_digest(confirm, plan_digest, "notesync refresh-all")
    _emit(_drv.notesync_refresh_all(dry_run=not confirm, plan_digest=plan_digest))


@notesync_group.command("mirror")
@click.option("--confirm", is_flag=True, help="Write; without it this is a dry run.")
@click.option("--plan-digest", default=None,
              help="The plan_digest printed by the dry run you reviewed.")
def notesync_mirror_cmd(confirm: bool, plan_digest: str | None):
    """Create, refresh and baseline the Obsidian mirror of Zotero's reading
    notes, in one sweep (zotero-authoritative mode).

    Three phases: create the notes Zotero has and the folder lacks (journalled,
    reversible from Settings → History and Restore), rewrite every region that differs
    (NOT journalled — the notes folder must be committed in the vault's git,
    which is the undo), then record a sync point for everything left without
    one, including the notes it just created. Without --confirm it plans all
    three, writes nothing and prints the plan_digest that --confirm
    --plan-digest must repeat."""
    _require_plan_digest(confirm, plan_digest, "notesync mirror")
    res = _drv.notesync_mirror(dry_run=not confirm, plan_digest=plan_digest)
    _emit(res)
    # The sweep's own verdict rides the terminal frame inside a 200, which
    # `_emit` cannot see: a refused or partial run (`plan_changed`,
    # `git_not_clean`, a failure) must not exit 0 (2026-09-21 review).
    if (res.get("mirror") or {}).get("ok") is False:
        sys.exit(1)


notesync_group.command("pull")(_pull_push_cmd("pull"))
notesync_group.command("push")(_pull_push_cmd("push"))


@notesync_group.command("merge-preview")
@click.argument("citation_key")
def notesync_merge_preview_cmd(citation_key: str):
    """Three-way merge proposal with the sha256/evidence a merge must quote."""
    _emit(_drv.notesync_merge_preview(citation_key))


@notesync_group.command("merge")
@click.option("--preview-file", type=click.Path(exists=True, dir_okay=False), required=True,
              help="JSON saved from `notesync merge-preview`.")
@click.option("--result-file", type=click.Path(exists=True, dir_okay=False), required=True,
              help="The reviewed merged text.")
@click.option("--confirm", is_flag=True, help="Required.")
def notesync_merge_cmd(preview_file, result_file, confirm: bool):
    """Write a reviewed merge to both the Obsidian region and the Zotero note."""
    _require_confirm(confirm, "notesync merge")
    preview = _load_json_file(preview_file, "--preview-file")
    try:
        with open(result_file, encoding="utf-8") as fh:
            result = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise click.UsageError(f"--result-file: {exc}")
    _emit(_drv.notesync_merge(preview.get("citation_key", ""), result,
                              preview.get("content_sha256", ""), preview.get("evidence", "")))


@notesync_group.command("key-changes")
def notesync_key_changes_cmd():
    """Reference notes whose citation key changed in Zotero (read-only)."""
    _emit(_drv.notesync_key_changes())


@notesync_group.command("rename-note")
@click.argument("citation_key")
@click.option("--dry-run", is_flag=True, help="Show the rename; write nothing.")
@click.option("--confirm", is_flag=True, help="Required unless --dry-run.")
def notesync_rename_note_cmd(citation_key: str, dry_run: bool, confirm: bool):
    """Rename the note CITATION_KEY (its old key) to its record's new key."""
    if not dry_run:
        _require_confirm(confirm, "notesync rename-note")
    _emit(_drv.notesync_rename_note(citation_key, dry_run=dry_run))


@notesync_group.command("create-attempts")
def notesync_create_attempts_cmd():
    """Retained note-creation attempts (app-owned evidence)."""
    _emit(_drv.notesync_create_attempts())


@notesync_group.command("inspect-create-attempt")
@click.argument("citation_key")
def notesync_inspect_create_attempt_cmd(citation_key: str):
    _emit(_drv.notesync_inspect_create_attempt(citation_key))


@notesync_group.command("create-zotero-note")
@click.argument("citation_key")
@click.option("--confirm", is_flag=True, help="Required.")
def notesync_create_zotero_note_cmd(citation_key: str, confirm: bool):
    """Create the missing Zotero reading note for one record."""
    _require_confirm(confirm, "notesync create-zotero-note")
    _emit(_drv.notesync_create_zotero_note(citation_key))


@cli.group("zotcreate")
def zotcreate_group():
    """Create a Zotero item for an orphan PDF and link the file under it."""
    pass


@zotcreate_group.command("preview")
@click.option("--pdf", required=True, help="Vault-relative orphan or pending PDF.")
def zotcreate_preview_cmd(pdf: str):
    """The draft: provenance, identifiers, suggested type (no network)."""
    _emit(_drv.zotcreate_preview(pdf))


@zotcreate_group.command("create")
@click.option("--draft-file", type=click.Path(exists=True, dir_okay=False), required=True,
              help="JSON object with " + ", ".join(_drv.ZOTCREATE_FIELDS) + ".")
@click.option("--dry-run", is_flag=True, help="Validate every gate; write nothing.")
@click.option("--confirm", is_flag=True, help="Required unless --dry-run.")
def zotcreate_create_cmd(draft_file, dry_run: bool, confirm: bool):
    """Create the parent item and link the PDF (one batch undo)."""
    if not dry_run:
        _require_confirm(confirm, "zotcreate create")
    _emit(_drv.zotcreate_create(_load_json_file(draft_file, "--draft-file"), dry_run=dry_run))


@zotcreate_group.command("batch")
@click.option("--rows-file", type=click.Path(exists=True, dir_okay=False), required=True,
              help="JSON list of rows {id?, pdf, item_type, fields, creators, decision_ref?}.")
@click.option("--dry-run", is_flag=True, help="One observation; admit or refuse each row; write nothing.")
@click.option("--acknowledge-plugins", is_flag=True, help="Proceed despite a file-renaming Zotero plugin.")
@click.option("--confirm", is_flag=True, help="Required for a real run.")
def zotcreate_batch_cmd(rows_file, dry_run: bool, acknowledge_plugins: bool, confirm: bool):
    """Create many Zotero items sharing ONE Web observation (SSE)."""
    if not dry_run:
        _require_confirm(confirm, "zotcreate batch")
    rows = _load_json_file(rows_file, "--rows-file")

    def _progress(ev):
        if isinstance(ev, dict) and "row" in ev:
            r = ev["row"]
            click.echo(f"{r.get('status'):16} {r.get('id')} {r.get('reason') or r.get('item_key') or ''}", err=True)
        elif isinstance(ev, dict) and "info" in ev:
            click.echo(ev["info"], err=True)

    _emit(_drv.zotcreate_batch(rows, dry_run=dry_run, acknowledge_plugins=acknowledge_plugins,
                               on_event=_progress))


@zotcreate_group.command("settle-never-created")
@click.option("--pdf", required=True, help="Vault-relative PDF whose create attempt is stuck.")
@click.option("--confirm", is_flag=True,
              help="Retire the attempt; without it this is a dry run showing the evidence.")
def zotcreate_settle_cmd(pdf: str, confirm: bool):
    """Unblock a PDF whose earlier create never reached Zotero (dry run unless --confirm)."""
    _emit(_drv.zotcreate_settle_never_created(pdf, dry_run=not confirm))


@zotcreate_group.command("reinstate")
@click.option("--seq", type=int, required=True, help="Retired attempt number (see `zotcreate attempts`).")
@click.option("--confirm", is_flag=True, help="Required.")
def zotcreate_reinstate_cmd(seq: int, confirm: bool):
    """Undo settle-never-created: the PDF is blocked again."""
    _require_confirm(confirm, "zotcreate reinstate")
    _emit(_drv.zotcreate_reinstate_attempt(seq))


@zotcreate_group.command("attempts")
def zotcreate_attempts_cmd():
    """Retained create attempts by stage."""
    _emit(_drv.zotcreate_attempts())


@library_group.group("run")
def library_run_group():
    """The "update my library" run: sessions, analyse, apply (plan lots 2-5)."""
    pass


@library_run_group.command("new")
@click.option("--provider", default=None, help="Session model provider (e.g. ollama, openai).")
@click.option("--model", default=None, help="Session model id.")
@click.option("--budget-usd", type=float, default=None,
              help="Spending limit for the session (required for a priced online model; "
                   "an unpriced one needs --no-limit).")
@click.option("--allow-unpriced", is_flag=True, hidden=True,
              help="Retired: use --no-limit (an unpriced model cannot be held to a limit).")
@click.option("--no-limit", is_flag=True,
              help="No spending limit: accept the cost of an online model outright "
                   "(spend is still measured). Cannot be combined with --budget-usd.")
@click.option("--label", default="", help="Free label shown with the session.")
def library_run_new_cmd(provider, model, budget_usd, allow_unpriced, no_limit, label):
    """Begin a session, optionally with the model it proposes tags with."""
    if allow_unpriced:
        # Retired (audit remediation 2026-09-23, L3); kept one release so a
        # script that passes it gets this sentence, not "no such option".
        raise click.UsageError("--allow-unpriced is retired: an unpriced model cannot be held to "
                               "a spending limit. Use --no-limit to accept its cost.")
    if no_limit and budget_usd is not None:
        raise click.UsageError("--no-limit and --budget-usd say opposite things; give one of them.")
    if not (provider or model) and (budget_usd is not None or no_limit):
        # These describe the session MODEL's cost; without --provider/--model
        # there is no model, and they were silently dropped (audit
        # remediation 2026-09-23, L11). Refused before any request.
        raise click.UsageError("--budget-usd, --no-limit and --allow-unpriced describe the "
                               "session model; give --provider and --model with them.")
    llm = None
    if provider or model:
        if not (provider and model):
            raise click.UsageError("--provider and --model go together.")
        llm = {"provider": provider, "model": model}
        if budget_usd is not None:
            llm["budget_usd"] = budget_usd
        if no_limit:
            llm["no_limit"] = True
    _emit(_drv.library_session_create(llm=llm, label=label))


@library_run_group.command("show")
@click.argument("session")
def library_run_show_cmd(session):
    """A session: its model, spend, last analysis and applies."""
    _emit(_drv.library_session(session))


def _library_progress(ev):
    if isinstance(ev, dict) and "info" in ev:
        click.echo(ev["info"], err=True)


@library_run_group.command("analyze")
@click.argument("session")
@click.option("--propose-tags", is_flag=True,
              help="Also propose tags with the session's model (may spend within its limit).")
def library_run_analyze_cmd(session, propose_tags):
    """Observe and propose; writes app tracking/cache/spend, not library content."""
    _emit(_drv.library_analyze(session, propose_tags=propose_tags, on_event=_library_progress))


@library_run_group.command("apply")
@click.argument("session")
@click.option("--approvals-file", required=True, type=click.Path(exists=True, dir_okay=False),
              help="JSON {mirror, pdf_links, tags, renames, leftovers} — rows chosen from "
                   "`library run analyze`, applied in that order (CLI_GUIDE.md).")
@click.option("--no-note-tags", is_flag=True,
              help="Do not write tags into Obsidian notes (they are on by default, "
                   "beside Finder and Zotero).")
@click.option("--confirm", is_flag=True, help="Required.")
def library_run_apply_cmd(session, approvals_file, no_note_tags, confirm):
    """Apply the reviewed rows: the mirror, then pdf: links, then tags, then renames."""
    _require_confirm(confirm, "library run apply")
    approvals = _load_json_file(approvals_file, "--approvals-file")
    if not isinstance(approvals, dict):
        raise click.UsageError("--approvals-file must hold a JSON object.")
    if no_note_tags:
        approvals["tags"] = [dict(r, targets={**(r.get("targets") or {}), "note": False})
                             for r in approvals.get("tags") or [] if isinstance(r, dict)]
    res = _drv.library_apply(session, approvals, confirm=True, on_event=_library_progress)
    _emit(res)
    # Exit 1 unless every row ended done/skipped (the mirror precedent: a run
    # that failed or is uncertain must not look like success to a script).
    run = res.get("library_apply") or {}
    statuses = [r.get("status") for r in (run.get("renames") or []) + (run.get("tags") or [])
                + (run.get("pdf_links") or []) + (run.get("leftovers") or [])]
    if run.get("mirror"):
        statuses.append(run["mirror"].get("status"))
    # Also non-zero when something the user asked for was NOT done even
    # though no row failed (audit remediation 2026-09-23, L5/L11): approval
    # rows ignored as duplicates/malformed, ticked note tags or `pdf:` lists
    # the mirror could not write, note units left open (e.g. "rescan
    # needed"), a run stopped by a scan, or a response with no summary.
    left_open = (run.get("ignored") or run.get("note_tags_unwritten")
                 or run.get("pdf_links_unwritten") or (run.get("mirror") or {}).get("open")
                 or run.get("stopped") or run.get("mirror_units_dropped"))
    if (not run or run.get("cancelled") or left_open
            or any(s not in ("done", "skipped") for s in statuses)):
        sys.exit(1)


@library_run_group.command("resolve")
@click.argument("session")
@click.argument("unit")
@click.argument("step", type=click.Choice(["rename", "tag", "mirror", "associate"]))
@click.option("--status", "status", required=True, type=click.Choice(["done", "failed"]),
              help="What you found after inspecting it: done (it landed) or failed (it did not; retryable).")
@click.option("--note", default="", help="Optional note kept with the resolution.")
@click.option("--confirm", is_flag=True, help="Required.")
def library_run_resolve_cmd(session, unit, step, status, note, confirm):
    """Resolve ONE uncertain step after inspecting it (audit remediation 2026-09-23).

    An uncertain outcome (the write may or may not have landed) blocks its
    unit until someone looks. Renames are also settled automatically from the
    journal when it can tell; this is the manual verb for the rest.
    """
    _require_confirm(confirm, "library run resolve")
    detail = {"note": note} if note else None
    _emit(_drv.library_record_step(session, unit, step, status, detail, confirm=True))


@cli.group("zotrecord")
def zotrecord_group():
    """Zotero record corrections: chapter titles (T9) and record merges (T8)."""
    pass


@zotrecord_group.command("retitle")
@click.argument("citation_key")
@click.option("--title", required=True, help="The corrected title.")
@click.option("--expected-title", default=None,
              help="The title reviewed (defaults to the cached scan's Zotero title).")
@click.option("--pdf", default=None, help="Biblio-relative PDF to check the title against.")
@click.option("--accept-unauthenticated", is_flag=True,
              help="Write a title the PDF's front matter does not print.")
@click.option("--dry-run", is_flag=True, help="Check the evidence; write nothing.")
@click.option("--confirm", is_flag=True, help="Required for a real write.")
def zotrecord_retitle_cmd(citation_key, title, expected_title, pdf, accept_unauthenticated, dry_run, confirm):
    """Write one item's title after checking the PDF prints it (dry run unless --confirm)."""
    if not dry_run:
        _require_confirm(confirm, "zotrecord retitle")
    _emit(_drv.zotrecord_retitle(citation_key, title, expected_title=expected_title, pdf=pdf,
                                 accept_unauthenticated=accept_unauthenticated, dry_run=dry_run))


@zotrecord_group.command("merge-preview")
@click.argument("keep")
@click.argument("retire")
def zotrecord_merge_preview_cmd(keep, retire):
    """Show what merging RETIRE into KEEP would do (reads only)."""
    _emit(_drv.zotrecord_merge_preview(keep, retire))


@zotrecord_group.command("merge")
@click.argument("keep")
@click.argument("retire")
@click.option("--dry-run", is_flag=True, help="Plan only; write nothing.")
@click.option("--confirm", is_flag=True, help="Required for a real merge.")
def zotrecord_merge_cmd(keep, retire, dry_run, confirm):
    """Merge RETIRE into KEEP (one batch id; undo with `tagging restore-batch`)."""
    if not dry_run:
        _require_confirm(confirm, "zotrecord merge")
    _emit(_drv.zotrecord_merge(keep, retire, dry_run=dry_run))


@zotrecord_group.command("trash")
@click.argument("citation_key")
@click.option("--accept-children", is_flag=True, help="Trash the item even though it has child items.")
@click.option("--dry-run", is_flag=True, help="Plan only; write nothing.")
@click.option("--confirm", is_flag=True, help="Required for a real trash.")
def zotrecord_trash_cmd(citation_key, accept_children, dry_run, confirm):
    """Move an empty record's Zotero item to the trash (undo with `tagging restore`)."""
    if not dry_run:
        _require_confirm(confirm, "zotrecord trash")
    _emit(_drv.zotrecord_trash(citation_key, accept_children=accept_children, dry_run=dry_run))


@zotlink_group.command("status")
def zotlink_status_cmd():
    """Attachment counts and whether the transport can write."""
    _emit(_drv.zotlink_status())


@zotlink_group.command("trash")
@click.option("--item-keys", default="", help="Comma-separated attachment keys.")
@click.option("--item-keys-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--accept-annotation-loss", is_flag=True,
              help="Trash attachments that carry Zotero annotations too.")
@click.option("--confirm", is_flag=True, help="Required.")
def zotlink_trash_cmd(item_keys, item_keys_file, accept_annotation_loss: bool, confirm: bool):
    """Move attachments to Zotero's trash (reversible; never deletes files)."""
    _require_confirm(confirm, "zotlink trash")
    sel = _read_key_selection(item_keys, item_keys_file)
    if sel is None:
        raise click.UsageError("--item-keys or --item-keys-file is required.")
    _emit(_drv.zotlink_trash(sorted(sel), accept_annotation_loss=accept_annotation_loss,
                             on_event=_info_printer))


@zotlink_group.command("retitle")
@click.option("--item-keys", default="", help="Comma-separated attachment keys.")
@click.option("--item-keys-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--dry-run", is_flag=True, help="Report what would change; write nothing.")
@click.option("--confirm", is_flag=True, help="Required unless --dry-run.")
def zotlink_retitle_cmd(item_keys, item_keys_file, dry_run: bool, confirm: bool):
    """Set linked attachments' Zotero titles to their filenames (reversible)."""
    if not dry_run:
        _require_confirm(confirm, "zotlink retitle")
    sel = _read_key_selection(item_keys, item_keys_file)
    if sel is None:
        raise click.UsageError("--item-keys or --item-keys-file is required.")
    _emit(_drv.zotlink_retitle(sorted(sel), dry_run=dry_run, on_event=_info_printer))


@tagging_group.command("pdf-links-status")
@click.option("--state", default="", help="Narrow rows to one state.")
def tagging_pdf_links_status_cmd(state: str):
    """Which notes carry a current, absent, stale or hand-written pdf: list."""
    _emit(_drv.tagging_pdf_links_status(state))


# ---------------------------------------------------------------------------
# Annual library cleanup harness (T0, private tooling; strippable)
# ---------------------------------------------------------------------------


def _cleanup_modules():
    """Import the private harness lazily; a stripped build says so plainly."""
    try:
        from cleanup import runner, paths as cpaths
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "cleanup":
            raise click.ClickException("The cleanup harness is not available in this build.")
        raise
    return runner, cpaths


def _cleanup_run(fn):
    runner, _ = _cleanup_modules()
    try:
        return fn(runner)
    except (runner.RunRefused, ValueError) as exc:
        raise click.ClickException(str(exc))


@cli.group("cleanup")
def cleanup_group():
    """Annual library cleanup: decision files, dry runs, applies and reports."""
    pass


@cleanup_group.command("new-pdfs")
@click.option("--run", "run", type=int, default=None,
              help="Continue this run (YYYYMMDD); default: today's.")
def cleanup_new_pdfs_cmd(run):
    """The recurring routine for PDFs added since the cleanup (T7).

    Proposes the next stage's decision file into the run's own directory and
    stops with the one command that moves the run forward. Never applies.
    """
    from core import driver

    _cleanup_modules()
    from cleanup import newpdfs

    try:
        res = _cleanup_run(lambda _r: newpdfs.step(
            driver, run=run, echo=lambda m: click.echo(m, err=True)))
    except newpdfs.RunBlocked as exc:
        raise click.ClickException(str(exc))
    click.echo(f"run {res['run']}")
    for st in res["stages"]:
        sel = st.get("selection") or {}
        extra = (f"  ({sel.get('undecided', 0)} undecided, {sel.get('accepted', 0)} to apply)"
                 if st["state"] in ("review", "apply") else "")
        click.echo(f"  {st['pid']:<4} {st['state']}{extra}")
    click.echo(res["next"] or "Nothing left to do in this run.")


@cleanup_group.command("report")
@click.argument("phase")
@click.option("--year", "--run", "year", default=2026, show_default=True,
              help="Cleanup year, or a new-pdfs run as YYYYMMDD.")
@click.option("--replace", is_flag=True, help="Phase 00 only: overwrite an existing baseline.")
def cleanup_report_cmd(phase: str, year: int, replace: bool):
    """Write the Phase 0 baseline, or a later phase's delta against it."""
    from core import driver
    if driver.resolve_base_url() is None:
        raise click.ClickException(_SERVER_DOWN_MSG)
    res = _cleanup_run(lambda r: r.report(phase, driver, year=year, replace=replace))
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cleanup_group.command("needs-pdf")
@click.option("--year", "--run", "year", default=2026, show_default=True,
              help="Cleanup year, or a new-pdfs run as YYYYMMDD.")
def cleanup_needs_pdf_cmd(year: int):
    """List records that owned a PDF at the Phase 0 baseline and own none now.

    Read-only: compares the baseline's raw pdf_links.json with the live
    inventory and writes 07-final/needs-pdf.{md,json}. Creates nothing.
    """
    from core import driver
    if driver.resolve_base_url() is None:
        raise click.ClickException(_SERVER_DOWN_MSG)
    _cleanup_modules()
    from cleanup import needspdf

    try:
        res = needspdf.write(driver, year=year)
    except needspdf.NeedsPdfError as exc:
        raise click.ClickException(str(exc))
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cleanup_group.command("propose")
@click.argument("phase")
@click.option("--year", "--run", "year", default=2026, show_default=True,
              help="Cleanup year, or a new-pdfs run as YYYYMMDD.")
@click.option("--llm", default=None, metavar="PROVIDER:MODEL",
              help="Run the phase's LLM drafting pass (Phase 02). Needs --approve-usd.")
@click.option("--approve-usd", type=float, default=None,
              help="The USD ceiling the estimate must fit under before any call.")
@click.option("--allow-unpriced", is_flag=True,
              help="Permit a model with no PRICING_TABLE entry (cost cannot be estimated).")
@click.option("--llm-limit", type=int, default=None, help="At most N model calls.")
@click.option("--estimate-only", is_flag=True,
              help="Print the cost estimate and make no call.")
@click.option("--max-output-tokens", type=int, default=None,
              help="Output-token cap per call. Default: the drafter's own "
                   "(a reasoning model spends part of the cap on thinking, "
                   "so too small a cap truncates the answer).")
@click.option("--scope", type=click.Choice(["pending", "orphans", "all"]),
              default=None,
              help="Phase 02 only: which accession bucket to propose from "
                   "(default pending). The new-pdfs routine passes all.")
def cleanup_propose_cmd(phase, year, llm, approve_usd, allow_unpriced, llm_limit,
                        estimate_only, max_output_tokens, scope):
    """Build a phase's decision file (never overwrites recorded decisions)."""
    from core import driver
    block = None
    if llm:
        # Validated BEFORE the server call, like `_parse_targets`: a typo in
        # the model id should not surface after a full inventory read.
        provider, _, model = llm.partition(":")
        if not provider or not model:
            raise click.UsageError("--llm takes PROVIDER:MODEL, e.g. google:gemini-2.5-pro.")
        if approve_usd is None and not estimate_only:
            raise click.UsageError(
                "--llm needs --approve-usd CEILING (or --estimate-only to see the price first).")
        from core.config import load_config
        block = {"provider": provider, "model": model, "approved_usd": approve_usd,
                 "allow_unpriced": allow_unpriced, "limit": llm_limit,
                 "estimate_only": estimate_only,
                 "max_output_tokens": max_output_tokens, "cfg": load_config()}
    elif any(v for v in (approve_usd, allow_unpriced, llm_limit, estimate_only,
                         max_output_tokens)):
        raise click.UsageError("those options apply to --llm only.")
    if driver.resolve_base_url() is None:
        raise click.ClickException(_SERVER_DOWN_MSG)
    extra = {}
    if scope is not None:
        extra["scope"] = scope
    res = _cleanup_run(lambda r: r.propose(
        phase, driver, year=year, llm=block,
        echo=lambda m: click.echo(m, err=True), **extra))
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cleanup_group.command("digest")
@click.argument("phase")
@click.option("--year", "--run", "year", default=2026, show_default=True,
              help="Cleanup year, or a new-pdfs run as YYYYMMDD.")
def cleanup_digest_cmd(phase: str, year: int):
    """The approval digest of a decision file and its decision counts."""
    res = _cleanup_run(lambda r: r.digest(phase, year=year))
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cleanup_group.command("normalise")
@click.argument("phase")
@click.option("--write", is_flag=True,
              help="Record the changes in the decision file (default: preview only).")
@click.option("--year", "--run", "year", default=2026, show_default=True,
              help="Cleanup year, or a new-pdfs run as YYYYMMDD.")
def cleanup_normalise_cmd(phase: str, write: bool, year: int):
    """Clean drafted fields of UNDECIDED create rows (layout line breaks, ALL CAPS,
    written-out dates, postcodes). Keeps every replaced value on the row."""
    res = _cleanup_run(lambda r: r.normalise(phase, year=year, write=write))
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cleanup_group.command("decide")
@click.argument("phase")
@click.argument("decision")
@click.argument("row_ids", nargs=-1, required=True)
@click.option("--note", default="", help="A note stored on every named row.")
@click.option("--year", "--run", "year", default=2026, show_default=True,
              help="Cleanup year, or a new-pdfs run as YYYYMMDD.")
def cleanup_decide_cmd(phase: str, decision: str, row_ids, note: str, year: int):
    """Record DECISION (accept | reject | other:<text> | "" to undecide) on the
    named rows. All or nothing."""
    res = _cleanup_run(lambda r: r.decide(phase, list(row_ids), decision,
                                          note=note, year=year))
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cleanup_group.command("draft")
@click.argument("phase")
@click.argument("row_id")
@click.argument("item_type")
@click.option("--year", "--run", "year", default=2026, show_default=True,
              help="Cleanup year, or a new-pdfs run as YYYYMMDD.")
def cleanup_draft_cmd(phase: str, row_id: str, item_type: str, year: int):
    """Give an undecided row with no create step an empty one of ITEM_TYPE,
    for `cleanup edit` to fill."""
    res = _cleanup_run(lambda r: r.draft(phase, row_id, item_type, year=year))
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cleanup_group.command("edit")
@click.argument("phase")
@click.argument("row_id")
@click.argument("field")
@click.argument("value")
@click.option("--source", default="maintainer", show_default=True,
              help="Where the value comes from (maintainer, pdf, filename…); stored with the edit.")
@click.option("--year", "--run", "year", default=2026, show_default=True,
              help="Cleanup year, or a new-pdfs run as YYYYMMDD.")
def cleanup_edit_cmd(phase: str, row_id: str, field: str, value: str, source: str, year: int):
    """Set FIELD (a Zotero field, or item_type) of an undecided create row;
    an empty VALUE removes the field. The draft's value is kept on the row."""
    res = _cleanup_run(lambda r: r.edit(phase, row_id, field, value, source=source,
                                        year=year))
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@cleanup_group.command("apply")
@click.argument("phase")
@click.option("--dry-run", is_flag=True, help="Report what each accepted row would do; write nothing.")
@click.option("--approved", "approved_digest", default=None,
              help="Required for a real run: the digest printed by `cleanup digest` at approval.")
@click.option("--category", "categories", multiple=True, help="Only rows of this category.")
@click.option("--limit", type=int, default=None, help="At most N accepted rows.")
@click.option("--year", "--run", "year", default=2026, show_default=True,
              help="Cleanup year, or a new-pdfs run as YYYYMMDD.")
def cleanup_apply_cmd(phase, dry_run, approved_digest, categories, limit, year):
    """Apply the accepted rows of a decision file (undecided rows are never touched)."""
    from core import driver
    if not dry_run and not approved_digest:
        raise click.UsageError("A real apply needs --approved DIGEST; run --dry-run first.")
    if driver.resolve_base_url() is None:
        raise click.ClickException(_SERVER_DOWN_MSG)
    res = _cleanup_run(lambda r: r.apply(
        phase, driver, year=year, dry_run=dry_run, approved_digest=approved_digest,
        categories=list(categories) or None, limit=limit,
        echo=lambda m: click.echo(m, err=True)))
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))
    if res.get("failed"):
        sys.exit(1)


@cli.command("mcp")
def mcp_cmd():
    """Launch the CHERQAiN Model Context Protocol (MCP) Server over stdio."""
    from mcp_server.server import main as run_mcp_server

    run_mcp_server()


def main():
    cli()


if __name__ == "__main__":
    main()
