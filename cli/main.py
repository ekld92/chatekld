"""PaperMind / ChatEKLD CLI.

Exposes the application workflows through a unified command line interface —
a thin client over the running server (GUI app or `papermind server`), which
the driver discovers via ``BASE_DIR/server.json`` / ``CHATEKLD_API_URL``.
Only the read-only ``status`` / ``config`` reads have an in-process fallback;
every other command requires the server (its locking/validation/journalling).

Notes:
- Click (shipped via Flask) provides the hierarchical subcommands with no new
  third-party dependency.
- Commands are grouped by domain (`paper`, `vault`, `audit`, `tagging`,
  `refactor`, `deck`, `docgen`).
- Errors surface formatted message bodies and exit 1; valid responses print
  readable output or JSON. Deeper input validation is the driver's/server's
  job (`_safe_subfolder`, the route validators) — the CLI layer itself only
  does argument-shape checks.
"""

import json
import os
import sys
import click

# Ensure repository root is on sys.path BEFORE domain imports
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.driver import (  # noqa: E402
    audit_scan,
    generate_deck,
    generate_doc,
    get_config,
    get_status,
    refactor_apply,
    refactor_plan,
    refactor_restore,
    set_config,
    summarize_paper,
    tagging_apply,
    tagging_propose,
    tagging_restore,
    vault_ask,
    vault_index,
    vault_search,
)


@click.group()
def cli():
    """PaperMind / ChatEKLD CLI - Perform PDF summarization, Vault RAG, Audit, Tagging, and Generation without GUI."""
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
        click.echo("=== PaperMind Status ===")
        click.echo(f"Server: {'running' if res.get('server_running') else 'not running (in-process snapshot)'}")
        if "config" in res:
            click.echo(f"Provider: {res['config'].get('provider')}")
            click.echo(f"Model: {res['config'].get('model')}")
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

    from core.driver import is_server_running
    from core.server_info import (clear_server_info, pid_alive,
                                  read_server_info, write_server_info)

    # Refuse a SECOND server (2026-07-22 audit C0): two processes would hold
    # two independent in-process op-locks over the same vault/index dirs —
    # exactly the lost-update class the HTTP-first driver design exists to
    # prevent ("the server's op-lock arbitrates all writes").
    info = read_server_info()
    if info and pid_alive(info["pid"]) \
            and is_server_running(f"http://{info['host']}:{info['port']}"):
        click.echo(
            f"A ChatEKLD server is already running at http://{info['host']}:"
            f"{info['port']} (pid {info['pid']}). The CLI and MCP tools target "
            f"it automatically — a second server would hold an independent "
            f"op-lock over the same vault. Stop it (or quit the app) before "
            f"starting another.", err=True)
        sys.exit(1)

    click.echo(f"Starting PaperMind headless server on http://{host}:{port} ...")
    from app import create_app

    app = create_app()
    # Written slightly BEFORE bind (app.run blocks): harmless, since every
    # reader re-probes /api/health before trusting the record.
    write_server_info(port, host=host)
    import atexit
    atexit.register(clear_server_info)
    try:
        # Werkzeug dev server, deliberately NOT the GUI's Waitress path: the
        # headless server is a single-user dev/agent convenience; threaded=True
        # (the Flask default) covers concurrent SSE reads at this scale, and
        # keeping app.run avoids importing/configuring waitress here. If SSE
        # pool exhaustion ever bites headless use, mirror launch.py's
        # waitress.serve call.
        app.run(host=host, port=port, threaded=True)
    finally:
        clear_server_info()


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
    for idx, item in enumerate(res.get("results", []), 1):
        click.echo(f"[{idx}] File: {item.get('file_path')} (Score: {item.get('score')})")
        click.echo(f"    {item.get('text', '').strip()[:200]}...\n")


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


# ---------------------------------------------------------------------------
# Biblio Tagging
# ---------------------------------------------------------------------------


@cli.group("tagging")
def tagging_group():
    """Biblio article tagging operations."""
    pass


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
    target_list = [t.strip() for t in targets.split(",") if t.strip()]
    click.echo(f"Applying tags {tag_list} to {citation_key} on targets {target_list}...")
    res = tagging_apply(citation_key, tag_list, target_list)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


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
# Note Refactor
# ---------------------------------------------------------------------------


@cli.group("refactor")
def refactor_group():
    """Note Refactor analyzer and vault note writer operations."""
    pass


@refactor_group.command("plan")
@click.option("--subfolder", default="", help="Subfolder within vault to scope plan")
def refactor_plan_cmd(subfolder: str):
    """Run read-only Note Refactor analysis on notes in vault subfolder."""
    click.echo(f"Planning refactor for subfolder '{subfolder}'...")
    res = refactor_plan(subfolder=subfolder)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@refactor_group.command("apply")
@click.option("--subfolder", default="", help="Subfolder within vault to apply plan")
@click.option("--rels", default="",
              help="Comma-separated vault-relative note paths to restrict the "
                   "write to (default: every changed note in scope)")
def refactor_apply_cmd(subfolder: str, rels: str):
    """Apply approved image callouts to notes in subfolder."""
    rel_list = [r.strip() for r in rels.split(",") if r.strip()] or None
    click.echo(f"Applying refactor to subfolder '{subfolder}'...")
    res = refactor_apply(subfolder=subfolder, rels=rel_list)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


@refactor_group.command("restore")
@click.argument("op_id")
def refactor_restore_cmd(op_id: str):
    """Reverse one journalled Note Refactor write by its op id."""
    res = refactor_restore(op_id)
    if "error" in res:
        click.echo(f"Error: {res['error']}", err=True)
        sys.exit(1)
    click.echo(json.dumps(res, indent=2, ensure_ascii=False))


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


@cli.command("mcp")
def mcp_cmd():
    """Launch the PaperMind Model Context Protocol (MCP) Server over stdio."""
    from mcp_server.server import main as run_mcp_server

    run_mcp_server()


def main():
    cli()


if __name__ == "__main__":
    main()
