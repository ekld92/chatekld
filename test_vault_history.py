"""Disposable tests for retained vault history and resolution primitives."""
import io
from pathlib import Path
from unittest import mock
import pytest

def _png_bytes(color=(10, 20, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def vault(tmp_path) -> Path:
    """A tiny vault: a scope sub-folder with a note + a central attachments dir."""
    root = Path(tmp_path / "vault_owner").resolve()
    scope = root / "study_notes"
    attachments = root / "Z_attachments"
    scope.mkdir(parents=True)
    attachments.mkdir(parents=True)

    (attachments / "7A27.png").write_bytes(_png_bytes((1, 2, 3)))
    (attachments / "fig.png").write_bytes(_png_bytes((9, 9, 9)))

    note = (
        "---\n"
        "tags: a, b, c\n"
        "related_notes: []\n"
        "---\n"
        "# Fluoxetine\n"
        "Dose habituelle **fluoxetine** 20 mg/j.\n"
        "\n"
        "![](7A27.png)\n"
        "\n"
        "## Schéma\n"
        "![[fig.png]]\n"
        "![](missing_image.png)\n"
    )
    (scope / "fluoxetine.md").write_text(note, encoding="utf-8")

    other = (
        "# Fluoxetine\n"
        "En pratique **fluoxetine** 200 mg/j (overdose).\n"
    )
    (scope / "autre.md").write_text(other, encoding="utf-8")
    return root


def test_build_name_index_honors_user_exclusions(vault):
    """A user vault_exclude_dirs entry hides those images from the resolver,
    matching the indexer's _should_skip_path (no spurious resolution)."""
    from core import vault_resolver as resolver

    with mock.patch.object(
        resolver, "load_config", return_value={"vault_exclude_dirs": ["Z_attachments"]}
    ):
        excluded = resolver.excluded_dirs(vault)
        idx = resolver.build_name_index(vault, excluded)
    assert resolver.is_excluded("Z_attachments/7A27.png", excluded)
    assert idx == {}  # every image lived under the excluded folder


def test_journal_prune_drops_reverted_and_reclaims_snapshot(vault):
    from core.config import load_config
    from vault_history import journal

    cfg = load_config()
    man = journal.load(vault, cfg)
    srel = journal.write_note_snapshot(vault, cfg, "study_notes/fluoxetine.md", "1-0", b"snap")
    snap_path = journal.archive_dir(vault, cfg) / srel
    assert snap_path.exists()
    man["ops"].append({"id": "1-0", "kind": "apply_note",
                       "note_rel": "study_notes/fluoxetine.md",
                       "snapshot_rel": srel, "state": "reverted"})
    dropped = journal.prune(vault, cfg, man)
    assert dropped == 1
    assert man["ops"] == []           # spent op removed
    assert not snap_path.exists()     # snapshot reclaimed


def test_journal_prune_caps_note_ops_but_keeps_applied_archive(vault, monkeypatch):
    from core.config import load_config
    from vault_history import journal

    monkeypatch.setattr(journal, "_MAX_OPS", 3)
    cfg = load_config()
    man = journal.load(vault, cfg)
    # An applied archive op (restore-critical — must never be evicted) ...
    man["ops"].append({"id": "0-arch", "kind": "archive_image", "state": "applied"})
    # ... plus 5 applied note-write ops, each with a snapshot on disk.
    snaps = []
    for i in range(5):
        sid = f"1-{i}"
        srel = journal.write_note_snapshot(vault, cfg, "study_notes/fluoxetine.md", sid, b"x")
        snaps.append(journal.archive_dir(vault, cfg) / srel)
        man["ops"].append({"id": sid, "kind": "apply_note",
                           "note_rel": "study_notes/fluoxetine.md",
                           "snapshot_rel": srel, "state": "applied"})
    dropped = journal.prune(vault, cfg, man)
    assert dropped == 3                                   # 6 ops, cap 3 → drop 3 oldest note-writes
    assert len(man["ops"]) == 3
    assert any(o["kind"] == "archive_image" for o in man["ops"])   # archive op survived
    assert not snaps[0].exists() and not snaps[1].exists() and not snaps[2].exists()
    assert snaps[3].exists() and snaps[4].exists()       # newest two kept


def test_journal_absent_manifest_loads_fresh(vault):
    from core.config import load_config
    from vault_history import journal
    man = journal.load(vault, load_config())
    assert man["ops"] == []


def test_journal_corrupt_manifest_raises_and_preserves_bytes(vault):
    from core.config import load_config
    from vault_history import journal
    cfg = load_config()
    p = journal.manifest_path(vault, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    corrupt = "{this is not json"
    p.write_text(corrupt, encoding="utf-8")
    with pytest.raises(journal.JournalUnreadableError):
        journal.load(vault, cfg)
    # Nothing rewrote the file while refusing.
    assert p.read_text(encoding="utf-8") == corrupt


def test_journal_wrong_shape_manifest_raises(vault):
    from core.config import load_config
    from vault_history import journal
    cfg = load_config()
    p = journal.manifest_path(vault, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"ops": "not-a-list"}', encoding="utf-8")
    with pytest.raises(journal.JournalUnreadableError):
        journal.load(vault, cfg)


def test_journal_missing_manifest_beside_snapshots_raises(vault):
    """A manifest that vanished while notes/ snapshots survive means the
    journal was lost (or an unmounted archive volume is a stub path) — a
    fresh manifest there would orphan every snapshot's restore mapping."""
    from core.config import load_config
    from vault_history import journal
    cfg = load_config()
    srel = journal.write_note_snapshot(
        vault, cfg, "study_notes/fluoxetine.md", "1-0", b"snap")
    assert (journal.archive_dir(vault, cfg) / srel).exists()
    assert not journal.manifest_path(vault, cfg).exists()
    with pytest.raises(journal.JournalUnreadableError):
        journal.load(vault, cfg)


def test_restore_route_requires_confirm(vault):
    """B8 (2026-07-13): the restore route mutates the vault and must reject an
    unconfirmed request (parity with apply/normalize/archive)."""
    from app import app
    from rag.vault import obsidian_manager

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(obsidian_manager, "get_vault_path", return_value=str(vault)):
        r0 = client.post("/api/history/restore", json={"all": True}, headers=headers)
        assert r0.status_code == 400
        r1 = client.post("/api/history/restore",
                         json={"all": True, "confirm": True}, headers=headers)
        # Confirmed: the empty manifest reverts nothing but the request is accepted.
        assert r1.status_code == 200
        assert r1.get_json()["reverted"] == 0


def test_build_file_index_matches_separate_builders(vault):
    # W1: the single-pass build_file_index must be output-identical to calling
    # the two legacy builders separately — same keys AND same per-basename path
    # lists (order included), so no consumer resolves differently.
    from core import vault_resolver as resolver

    (vault / "study_notes/ref.pdf").write_bytes(b"%PDF-1.4\n")
    excluded = resolver.excluded_dirs(vault)
    name_i, link_i = resolver.build_file_index(vault, excluded)
    assert name_i == resolver.build_name_index(vault, excluded)
    assert link_i == resolver.build_link_index(vault, excluded)


def test_get_file_index_caches_and_invalidates(vault):
    # W1: get_file_index returns the SAME object on a hit (proving the cache is
    # used), refresh=True rebuilds, and invalidate_index_cache drops the entry so
    # a newly-added file is picked up.
    from core import vault_resolver as resolver

    resolver.invalidate_index_cache(vault)
    first = resolver.get_file_index(vault)
    again = resolver.get_file_index(vault)
    assert again is first  # cache hit returns the identical cached tuple

    # A file added out-of-band is invisible until the cache is refreshed/dropped.
    (vault / "study_notes/newimg.png").write_bytes(b"\x89PNG\r\n")
    assert "newimg.png" not in resolver.get_file_index(vault)[0]
    fresh = resolver.get_file_index(vault, refresh=True)
    assert "newimg.png" in fresh[0]
    assert fresh is not first
    # invalidate → next access rebuilds a new object.
    resolver.invalidate_index_cache(vault)
    assert resolver.get_file_index(vault) is not fresh


def test_manager_heartbeat_keeps_oplock_alive_past_ttl():
    """obsidian_manager.heartbeat() must refresh the op-lock so it is not stolen.

    Directly exercises the fix's mechanism: acquire with a short TTL, sleep past
    it but heartbeat first, then assert a concurrent acquire is still rejected —
    i.e. the lock did not passively expire.
    """
    import time
    from rag.vault import obsidian_manager

    obsidian_manager.force_release()
    epoch = obsidian_manager.try_acquire_lock(ttl=1)
    assert epoch
    try:
        time.sleep(0.6)
        obsidian_manager.heartbeat(epoch)  # push the deadline out before it lapses
        time.sleep(0.6)               # total 1.2s > ttl, but heartbeated at 0.6s
        obsidian_manager.heartbeat(epoch)
        # A would-be concurrent indexing acquire must fail: the lock is still live.
        assert obsidian_manager._op_lock.try_acquire(1) is False
    finally:
        obsidian_manager.release_lock(epoch)


def test_manager_heartbeat_returns_false_after_lock_is_stolen():
    """heartbeat() must report False once another operation took the lock.

    Finding 1 (2026-07-20 audit): the batch writers can only stop if this signal
    reaches them. Force-release the acquisition (simulating a TTL lapse + steal),
    let a new operation acquire, and assert the old holder's heartbeat is False
    while the new holder's is True. epoch==0 (no lock) returns True.
    """
    from rag.vault import obsidian_manager

    obsidian_manager.force_release()
    old_epoch = obsidian_manager.try_acquire_lock(ttl=60)
    assert old_epoch
    # Simulate the lapse-and-steal: free the acquisition, a new op takes it.
    obsidian_manager.force_release()
    new_epoch = obsidian_manager.try_acquire_lock(ttl=60)
    assert new_epoch
    try:
        assert obsidian_manager.heartbeat(old_epoch) is False  # stolen → stop
        assert obsidian_manager.heartbeat(new_epoch) is True   # current holder
        assert obsidian_manager.heartbeat(0) is True           # no lock held
    finally:
        obsidian_manager.release_lock(new_epoch)


@pytest.mark.parametrize('kind', ['apply_note', 'normalize_note', 'llm_note', 'notesync_update'])
def test_legacy_snapshot_restore_and_stale_refusal(tmp_path, kind):
    import hashlib
    from vault_history import journal
    root = tmp_path / 'vault'
    root.mkdir()
    cfg = {'refactor_archive_dir': str(tmp_path / 'custom')}
    before, after = b'before\n', b'after\n'
    snapshot = journal.write_note_snapshot(root, cfg, 'note.md', 'old-id', before)
    op = dict(id='old-id', kind=kind, state='applied', note_rel='note.md',
              hash_before=hashlib.sha256(before).hexdigest(),
              hash_after=hashlib.sha256(after).hexdigest(), snapshot_rel=snapshot)
    journal.save(root, cfg, {'version': 1, 'ops': [op]})
    op = journal.load(root, cfg)['ops'][0]
    note = root / 'note.md'
    note.write_bytes(b'newer edit')
    assert journal.revert_op(root, cfg, op)['status'] == 'skipped'
    assert note.read_bytes() == b'newer edit'
    note.write_bytes(after)
    assert journal.revert_op(root, cfg, op)['status'] == 'reverted'
    assert note.read_bytes() == before


@pytest.mark.parametrize('conflict', ['none', 'note', 'original', 'snapshot'])
def test_legacy_image_restore_preserves_evidence_on_conflict(tmp_path, conflict):
    import hashlib
    from vault_history import journal
    def sha(b):
        return hashlib.sha256(b).hexdigest()
    root = tmp_path / 'vault'
    root.mkdir()
    cfg = {'refactor_archive_dir': str(tmp_path / 'custom')}
    before, after, image = b'![[image.png]]', b'![[thumb.png]]', b'original image'
    snapshot = journal.write_note_snapshot(root, cfg, 'note.md', 'old', before)
    arch = journal.archive_dir(root, cfg) / 'attachments/image.png'
    arch.parent.mkdir(parents=True)
    arch.write_bytes(image)
    (root / 'note.md').write_bytes(after)
    (root / 'thumb.png').write_bytes(b'thumbnail')
    op = dict(kind='archive_image', state='applied', note_rel='note.md',
              image_rel='image.png', archive_rel='attachments/image.png',
              thumb_rel='thumb.png', note_snapshot_rel=snapshot,
              note_hash_before=sha(before), note_hash_after=sha(after),
              digest=sha(image), original_deleted=True)
    if conflict == 'note':
        (root / 'note.md').write_bytes(b'later edit')
    if conflict == 'original':
        (root / 'image.png').write_bytes(b'foreign file')
    if conflict == 'snapshot':
        (journal.archive_dir(root, cfg) / snapshot).unlink()
    result = journal.revert_op(root, cfg, op)
    if conflict == 'none':
        assert result['status'] == 'reverted'
        assert (root / 'image.png').read_bytes() == image
        assert (root / 'note.md').read_bytes() == before
        assert not (root / 'thumb.png').exists()
    else:
        assert result['status'] in ('skipped', 'failed')
        assert arch.read_bytes() == image
        assert (root / 'thumb.png').exists()
        assert op['state'] == 'applied'


def test_default_and_custom_history_paths(tmp_path, monkeypatch):
    from vault_history import journal
    monkeypatch.setattr(journal, 'BASE_DIR', str(tmp_path / 'app'))
    root = tmp_path / 'vault'
    root.mkdir()
    key = journal._vault_key(root)
    assert journal.archive_dir(root, {}) == tmp_path / 'app/refactor/archive' / key
    cfg = {'refactor_archive_dir': str(tmp_path / 'old-custom')}
    assert journal.archive_dir(root, cfg) == tmp_path / 'old-custom' / key
    with pytest.raises(journal.ScopeError):
        journal.archive_dir(root, {'refactor_archive_dir': str(root)})


def test_retired_entry_points_are_unavailable():
    import app
    from cli.main import cli
    from core import driver
    from mcp_server.server import handle_request
    routes = {r.rule for r in app.app.url_map.iter_rules()}
    assert not any(r.startswith('/api/refactor/') for r in routes)
    assert {'/api/history/manifest', '/api/history/restore'} <= routes
    assert 'refactor' not in cli.commands
    assert not any(k.startswith('refactor_') for k in driver.ENDPOINTS)
    response = handle_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert not any(t['name'].startswith('refactor_') for t in response['result']['tools'])


def test_two_callers_sharing_an_action_name_serialise():
    import threading
    from api import bounded

    entered = threading.Event()
    release = threading.Event()
    out = {}

    def _blocked(should_cancel):
        entered.set()
        release.wait(timeout=10)
        return "first"

    t = threading.Thread(
        target=lambda: out.__setitem__(
            "first", bounded.run_bounded_action("t-shared", _blocked, deadline_s=5.0)),
        daemon=True)
    t.start()
    try:
        assert entered.wait(timeout=5)
        # A DIFFERENT caller, same action name → refused, not stacked.
        assert bounded.run_bounded_action(
            "t-shared", lambda c: "second", deadline_s=5.0) == (None, False, True)
    finally:
        release.set()
        t.join(timeout=5)
    assert out["first"] == ("first", False, False)

    # The slot is freed by the daemon's finally, so the name is reusable.
    assert bounded.run_bounded_action(
        "t-shared", lambda c: "third", deadline_s=5.0) == ("third", False, False)



def test_current_config_is_not_rewritten_for_retirement(tmp_path, monkeypatch):
    import json
    from core import config
    path = tmp_path / 'config.json'
    raw = json.dumps({'config_version': config.CONFIG_VERSION,
                      'refactor_archive_dir': str(tmp_path / 'custom'),
                      'refactor_review_model': 'historical-choice'})
    path.write_text(raw)
    monkeypatch.setattr(config, 'CONFIG_FILE', str(path))
    monkeypatch.setattr(config, '_config_cache', None)
    loaded = config.load_config()
    assert loaded['refactor_archive_dir'] == str(tmp_path / 'custom')
    assert path.read_text() == raw
    assert 'refactor_review_model' not in config._DEFAULTS
