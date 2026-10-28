"""Batch C of the 2026-09-23 CLI proof-run fixes: small fixes.

C1 — CLI help texts name what the commands really take.
C3 — the `pdf_reconciliation` block rides only the reports that render it.
C4 — the headless `papermind server` loads `.env` like the GUI does.
"""
from __future__ import annotations

import os

from unittest import mock

from click.testing import CliRunner


# ---------------------------------------------------------------------------
# C1
# ---------------------------------------------------------------------------

def test_library_run_apply_help_names_every_approvals_key():
    from cli.main import cli
    out = CliRunner().invoke(cli, ["library", "run", "apply", "--help"]).output
    for key in ("mirror", "pdf_links", "tags", "renames", "leftovers"):
        assert key in out, key


def test_audit_report_help_lists_every_report():
    from cli.main import cli
    from core.driver import AUDIT_REPORT_NAMES
    out = " ".join(CliRunner().invoke(cli, ["audit", "report", "--help"]).output.split())
    for name in AUDIT_REPORT_NAMES:
        assert name in out, name


def test_the_driver_report_list_is_the_route_registry():
    # The CLI help and the MCP enum read the driver's tuple; it must be the
    # route's allow-list exactly, not a stale subset.
    from api.routes.audit import _REPORT_NAMES
    from core.driver import AUDIT_REPORT_NAMES
    assert set(AUDIT_REPORT_NAMES) == set(_REPORT_NAMES)
    assert len(AUDIT_REPORT_NAMES) == len(set(AUDIT_REPORT_NAMES))


def test_audit_scan_points_at_audit_wait():
    from core import driver
    with mock.patch.object(driver, "resolve_base_url", return_value="http://x"), \
            mock.patch.object(driver, "_http_json", return_value={"ok": True, "started": True}):
        res = driver.audit_scan()
    assert "cherqain audit wait" in res["note"]
    assert "papermind status" not in res["note"]


# ---------------------------------------------------------------------------
# C3
# ---------------------------------------------------------------------------

def _report_client(tmp_path):
    from app import create_app
    from audit.config import Settings
    from audit.engine.bridge import BridgeResult
    from audit.engine.inventory import Inventory
    inv = Inventory(records={}, bridge=BridgeResult(), pdfs_skipped=[], zotero_matched_by_title=0)
    return create_app().test_client(), inv, Settings(vault_root=tmp_path)


def test_the_pdf_block_rides_only_the_pdf_reports(tmp_path):
    from audit.manager import audit_manager
    client, inv, settings = _report_client(tmp_path)
    calls = []
    with mock.patch.object(audit_manager, "get_inventory", return_value=(inv, settings)), \
            mock.patch("api.routes.audit._pdf_projection_summary",
                       side_effect=lambda s: calls.append(s) or {"marker": 1}):
        other = client.get("/api/audit/reports/orphan_zotero_items",
                           headers={"X-Requested-With": "ChatEKLD"}).get_json()
        assert "pdf_reconciliation" not in other
        assert calls == []  # not even computed for a report that never shows it
        pdf = client.get("/api/audit/reports/skipped_pdfs",
                         headers={"X-Requested-With": "ChatEKLD"}).get_json()
    assert pdf["pdf_reconciliation"] == {"marker": 1}
    assert len(calls) == 1


def test_no_after_request_hook_reparses_report_responses():
    from app import create_app
    app = create_app()
    hooks = [f.__name__ for fs in app.after_request_funcs.values() for f in fs]
    assert "_include_pdf_projection" not in hooks


def test_the_js_pdf_report_set_is_the_server_definition():
    import re
    from pathlib import Path

    from api.routes.audit import PDF_PROJECTION_REPORTS, _REPORT_NAMES
    src = (Path(__file__).resolve().parents[1] / "static" / "js" / "audit.js").read_text(encoding="utf-8")
    m = re.search(r"const _PDF_REPORTS = new Set\(\[(.*?)\]\);", src, re.S)
    assert m, "the _PDF_REPORTS literal moved; update this ratchet"
    js = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert js == set(PDF_PROJECTION_REPORTS)
    assert set(PDF_PROJECTION_REPORTS) <= set(_REPORT_NAMES)


# ---------------------------------------------------------------------------
# C4 — the headless server loads .env like the GUI
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture
def restore_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_the_shared_loader_reads_the_first_file_and_never_overrides(tmp_path, restore_environ, monkeypatch):
    from core.env_files import load_env_files
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / ".env").write_text("CHATEKLD_T_ONE=from_a\nCHATEKLD_T_SHELL=file\n", encoding="utf-8")
    (b / ".env").write_text("CHATEKLD_T_ONE=from_b\nCHATEKLD_T_TWO=from_b\n", encoding="utf-8")
    os.chmod(a / ".env", 0o644)
    os.environ["CHATEKLD_T_SHELL"] = "shell"
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    loaded = load_env_files([str(a), str(b)])
    assert os.environ["CHATEKLD_T_ONE"] == "from_a"
    assert os.environ["CHATEKLD_T_TWO"] == "from_b"
    assert os.environ["CHATEKLD_T_SHELL"] == "shell"  # override=False
    assert (a / ".env").stat().st_mode & 0o777 == 0o600
    assert loaded == [str(a / ".env"), str(b / ".env")]


def test_the_loader_is_import_light():
    import subprocess
    import sys
    code = ("import sys; import core.env_files; "
            "print(any(m in sys.modules for m in ('flask', 'app', 'llama_index', 'dotenv')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    assert out.stdout.strip() == "False", out.stderr


def test_the_headless_server_loads_env_before_building_the_app(tmp_path, restore_environ, monkeypatch):
    from cli import main as cli_main
    (tmp_path / ".env").write_text("ZOTERO_API_KEY=from-temp-env\n", encoding="utf-8")
    os.environ.pop("ZOTERO_API_KEY", None)
    seen = {}

    class Stop(Exception):
        pass

    def fake_create_app():
        seen["key"] = os.environ.get("ZOTERO_API_KEY")
        raise Stop

    with mock.patch.object(cli_main, "_server_env_dirs", lambda: [str(tmp_path)]), \
            mock.patch("core.server_info.read_server_info", return_value=None), \
            mock.patch("app.create_app", fake_create_app):
        monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
        result = CliRunner().invoke(cli_main.cli, ["server"])
    assert isinstance(result.exception, Stop)
    assert seen["key"] == "from-temp-env"


def test_the_gui_entry_point_uses_the_same_loader():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "launch.py").read_text(encoding="utf-8")
    assert "from core.env_files import load_env_files" in src
