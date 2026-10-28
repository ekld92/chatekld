"""Unit tests for ``core.env_files`` and ``core.constants`` path overrides."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def restore_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_load_dotenv_file_enforces_owner_only_and_respects_override(tmp_path, restore_environ, monkeypatch):
    from core.env_files import load_dotenv_file

    env_file = tmp_path / ".env"
    env_file.write_text(
        "CHERQAIN_ENV_FILE_TEST=from_dotenv\nCHERQAIN_SHELL_WINS=from_dotenv\n",
        encoding="utf-8",
    )
    os.chmod(env_file, 0o644)
    os.environ["CHERQAIN_SHELL_WINS"] = "from_shell"
    os.environ.pop("CHERQAIN_ENV_FILE_TEST", None)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)

    assert load_dotenv_file(env_file, override=False) is True
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert os.environ["CHERQAIN_ENV_FILE_TEST"] == "from_dotenv"
    assert os.environ["CHERQAIN_SHELL_WINS"] == "from_shell"


def test_default_base_dir_loads_repo_dotenv_before_resolving_base_dir(tmp_path, restore_environ, monkeypatch):
    from core import constants

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    custom_base = tmp_path / "custom_data_dir"
    (fake_repo / ".env").write_text(
        f"CHERQAIN_BASE_DIR={custom_base}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(constants, "_REPO_ROOT", fake_repo)
    monkeypatch.setattr(constants.sys, "frozen", False, raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("CHERQAIN_BASE_DIR", raising=False)
    monkeypatch.delenv("CHATEKLD_BASE_DIR", raising=False)

    resolved = constants._default_base_dir()
    assert Path(resolved) == custom_base
    assert custom_base.is_dir()
    assert (custom_base.stat().st_mode & 0o777) == 0o700


def test_default_base_dir_shell_environment_wins_over_repo_dotenv(tmp_path, restore_environ, monkeypatch):
    from core import constants

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    dotenv_base = tmp_path / "dotenv_data_dir"
    shell_base = tmp_path / "shell_data_dir"
    (fake_repo / ".env").write_text(
        f"CHERQAIN_BASE_DIR={dotenv_base}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(constants, "_REPO_ROOT", fake_repo)
    monkeypatch.setattr(constants.sys, "frozen", False, raising=False)
    monkeypatch.setenv("CHERQAIN_BASE_DIR", str(shell_base))
    monkeypatch.delenv("CHATEKLD_BASE_DIR", raising=False)

    resolved = constants._default_base_dir()
    assert Path(resolved) == shell_base
    assert not dotenv_base.exists()


def test_resolve_storage_path_honors_cherqain_and_legacy_overrides(tmp_path, restore_environ, monkeypatch):
    from core import constants

    default_db = str(tmp_path / "default" / "uploads.db")
    custom_db = str(tmp_path / "custom" / "my_uploads.db")
    legacy_db = str(tmp_path / "legacy" / "legacy_uploads.db")

    monkeypatch.delenv("CHERQAIN_DB_PATH", raising=False)
    monkeypatch.delenv("CHATEKLD_DB_PATH", raising=False)
    assert constants._resolve_storage_path("CHATEKLD_DB_PATH", default_db) == default_db

    monkeypatch.setenv("CHATEKLD_DB_PATH", legacy_db)
    assert constants._resolve_storage_path("CHATEKLD_DB_PATH", default_db) == legacy_db

    monkeypatch.setenv("CHERQAIN_DB_PATH", custom_db)
    assert constants._resolve_storage_path("CHATEKLD_DB_PATH", default_db) == custom_db


def test_repo_dotenv_subpaths_do_not_leak_when_disposable_base_dir_is_set(tmp_path, restore_environ, monkeypatch):
    from core import constants

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    disposable_base = tmp_path / "disposable_base"
    leaked_db = tmp_path / "leaked_db.sqlite"
    leaked_idx = tmp_path / "leaked_index"
    leaked_cache = tmp_path / "leaked_cache"
    (fake_repo / ".env").write_text(
        f"CHERQAIN_BASE_DIR={tmp_path / 'leaked_base'}\n"
        f"CHERQAIN_DB_PATH={leaked_db}\n"
        f"CHERQAIN_INDEX_DIR={leaked_idx}\n"
        f"CHERQAIN_CACHE_DIR={leaked_cache}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(constants, "_REPO_ROOT", fake_repo)
    monkeypatch.setattr(constants.sys, "frozen", False, raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("CHERQAIN_BASE_DIR", raising=False)
    monkeypatch.setenv("CHATEKLD_BASE_DIR", str(disposable_base))
    for k in (
        "CHERQAIN_DB_PATH", "CHATEKLD_DB_PATH",
        "CHERQAIN_INDEX_DIR", "CHATEKLD_INDEX_DIR",
        "CHERQAIN_CACHE_DIR", "CHATEKLD_CACHE_DIR",
    ):
        monkeypatch.delenv(k, raising=False)

    resolved_base = constants._default_base_dir()
    assert Path(resolved_base) == disposable_base
    assert constants._LOADED_REPO_DOTENV is False
    assert constants._resolve_storage_path(
        "CHATEKLD_DB_PATH", os.path.join(resolved_base, "uploads.db")
    ) == os.path.join(resolved_base, "uploads.db")
    assert constants._resolve_storage_path(
        "CHATEKLD_INDEX_DIR", os.path.join(resolved_base, "obsidian_storage")
    ) == os.path.join(resolved_base, "obsidian_storage")
    assert constants._resolve_storage_path(
        "CHATEKLD_CACHE_DIR", os.path.join(resolved_base, "obsidian_cache")
    ) == os.path.join(resolved_base, "obsidian_cache")


def test_base_dir_dotenv_subpaths_load_and_precedence_order(tmp_path, restore_environ, monkeypatch):
    from core.env_files import load_env_files

    base_dir = tmp_path / "base_dir"
    repo_dir = tmp_path / "repo_dir"
    base_dir.mkdir()
    repo_dir.mkdir()
    (base_dir / ".env").write_text(
        "CHERQAIN_PRECEDENCE_A=from_base\nCHERQAIN_PRECEDENCE_B=from_base\n",
        encoding="utf-8",
    )
    (repo_dir / ".env").write_text(
        "CHERQAIN_PRECEDENCE_A=from_repo\nCHERQAIN_PRECEDENCE_B=from_repo\nCHERQAIN_PRECEDENCE_C=from_repo\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setenv("CHERQAIN_PRECEDENCE_A", "from_shell")
    monkeypatch.delenv("CHERQAIN_PRECEDENCE_B", raising=False)
    monkeypatch.delenv("CHERQAIN_PRECEDENCE_C", raising=False)

    load_env_files([str(base_dir), str(repo_dir)])
    assert os.environ["CHERQAIN_PRECEDENCE_A"] == "from_shell"
    assert os.environ["CHERQAIN_PRECEDENCE_B"] == "from_base"
    assert os.environ["CHERQAIN_PRECEDENCE_C"] == "from_repo"


def test_native_pick_folder_and_file_routes_guard_suffix_and_non_mutation(tmp_path, monkeypatch):
    from unittest import mock

    from app import app
    from core import config as cfg_mod

    app.config["TESTING"] = True
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}

    cfg_before = dict(cfg_mod.load_config())

    # 1. Missing X-Requested-With is rejected by the CSRF/origin guard (403).
    assert client.post("/api/config/native-pick-folder").status_code == 403
    assert client.post("/api/config/native-pick-file").status_code == 403

    # 2. Headless (no webview windows) returns 503.
    fake_webview = mock.MagicMock()
    fake_webview.windows = []
    fake_webview.FOLDER_DIALOG = 20
    fake_webview.OPEN_DIALOG = 10
    with mock.patch.dict("sys.modules", {"webview": fake_webview}):
        r_folder_503 = client.post("/api/config/native-pick-folder", headers=headers)
        r_file_503 = client.post("/api/config/native-pick-file", headers=headers)
    assert r_folder_503.status_code == 503
    assert r_file_503.status_code == 503

    # 3. Cancel returns {cancelled: True, path: None}.
    fake_window = mock.MagicMock()
    fake_window.create_file_dialog.return_value = None
    fake_webview.windows = [fake_window]
    with mock.patch.dict("sys.modules", {"webview": fake_webview}):
        r_cancel_folder = client.post("/api/config/native-pick-folder", headers=headers)
        r_cancel_file = client.post("/api/config/native-pick-file", headers=headers)
    assert r_cancel_folder.status_code == 200
    assert r_cancel_folder.get_json() == {"cancelled": True, "path": None}
    assert r_cancel_file.status_code == 200
    assert r_cancel_file.get_json() == {"cancelled": True, "path": None}

    # 4. Valid folder & valid SQLite file vs rejected file suffix.
    picked_dir = tmp_path / "ArchiveDir"
    picked_dir.mkdir()
    valid_sqlite = tmp_path / "zotero.sqlite"
    valid_sqlite.write_text("", encoding="utf-8")
    bad_file = tmp_path / "notes.txt"
    bad_file.write_text("hi", encoding="utf-8")

    with mock.patch.dict("sys.modules", {"webview": fake_webview}):
        fake_window.create_file_dialog.return_value = [str(picked_dir)]
        r_folder_ok = client.post("/api/config/native-pick-folder", headers=headers)
        assert r_folder_ok.status_code == 200
        assert r_folder_ok.get_json()["path"] == os.path.realpath(str(picked_dir))

        fake_window.create_file_dialog.return_value = [str(valid_sqlite)]
        r_file_ok = client.post("/api/config/native-pick-file", headers=headers)
        assert r_file_ok.status_code == 200
        assert r_file_ok.get_json()["path"] == os.path.realpath(str(valid_sqlite))

        fake_window.create_file_dialog.return_value = [str(bad_file)]
        r_file_bad = client.post("/api/config/native-pick-file", headers=headers)
        assert r_file_bad.status_code == 400
        assert ".sqlite" in r_file_bad.get_json()["error"]

        # 5. Error in create_file_dialog returns 500 cleanly.
        fake_window.create_file_dialog.side_effect = RuntimeError("dialog boom")
        r_err_folder = client.post("/api/config/native-pick-folder", headers=headers)
        r_err_file = client.post("/api/config/native-pick-file", headers=headers)
        assert r_err_folder.status_code == 500
        assert r_err_file.status_code == 500

    # 6. Picker routes never mutate server config directly.
    assert dict(cfg_mod.load_config()) == cfg_before




def test_relative_native_picker_contains_selection_and_preserves_config(tmp_path, monkeypatch):
    from unittest import mock
    from app import app
    from api.routes import config as routes

    vault = tmp_path / 'vault'
    attachments = vault / 'Attachments'
    pdfs = attachments / 'papers'
    pdfs.mkdir(parents=True)
    bib = vault / 'library.bib'
    bib.write_text('')
    outside = tmp_path / 'outside.bib'
    outside.write_text('')
    link = vault / 'escape.bib'
    link.symlink_to(outside)
    cfg = {'obsidian_vault_path': str(vault), 'audit_attachments_subdir': 'Attachments'}
    monkeypatch.setattr(routes, 'load_config', lambda: dict(cfg))
    webview = mock.MagicMock()
    window = webview.windows[0]
    headers = {'X-Requested-With': 'ChatEKLD'}
    client = app.test_client()
    with mock.patch.dict('sys.modules', {'webview': webview}):
        for selected, endpoint, body, expected in [
            (pdfs, 'folder', {'relative_to_vault': True, 'under_attachments': True}, 'papers'),
            (attachments, 'folder', {'relative_to_vault': True}, 'Attachments'),
            (bib, 'file', {'relative_to_vault': True, 'kind': 'bib'}, 'library.bib'),
            (link, 'file', {'relative_to_vault': True, 'kind': 'bib'}, None),
            (outside, 'file', {'relative_to_vault': True, 'kind': 'bib'}, None),
        ]:
            window.create_file_dialog.return_value = [str(selected)]
            response = client.post('/api/config/native-pick-' + endpoint, json=body, headers=headers)
            assert response.status_code == (200 if expected else 400)
            if expected:
                assert response.json['relative_path'] == expected
    assert cfg == {'obsidian_vault_path': str(vault), 'audit_attachments_subdir': 'Attachments'}
