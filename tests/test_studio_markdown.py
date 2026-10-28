"""Studio Markdown previews and journalled apply, with isolated fixture files."""
import hashlib
import json
from unittest.mock import Mock

import pytest
from flask import Flask


@pytest.fixture
def studio(tmp_path, monkeypatch):
    from api.routes import docgen as route
    root = tmp_path / 'vault'
    root.mkdir()
    cfg = {'refactor_archive_dir': str(tmp_path / 'archive')}
    monkeypatch.setattr(route, 'BASE_DIR', str(tmp_path / 'app'))
    monkeypatch.setattr(route, 'load_config', lambda: cfg)
    monkeypatch.setattr(route.obsidian_manager, 'get_vault_path', lambda: str(root))
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda **kw: 'epoch')
    monkeypatch.setattr(route.obsidian_manager, 'release_lock', lambda epoch: None)
    app = Flask(__name__)
    app.register_blueprint(route.docgen_bp)
    return route, app.test_client(), root, cfg


def stage(route, root, original, proposed):
    token = 'a' * 32
    record = dict(root=str(root), rel='source.md', before=hashlib.sha256(original).hexdigest(),
                  after=hashlib.sha256(proposed.encode()).hexdigest(), proposed=proposed,
                  created=route.time.time(), generation=getattr(route.obsidian_manager, '_index_generation', 0))
    record['session'] = getattr(route, '_MARKDOWN_SESSION_ID', 'previous-session')
    route._markdown_stage(token).write_text(json.dumps(record))
    return token


def test_apply_preserves_original_and_restore(studio):
    from vault_history import journal
    route, client, root, cfg = studio
    original = b'---\r\ntitle: Test\r\n---\r\n# Original\r\n```python\r\npass\r\n```\r\n'
    (root / 'source.md').write_bytes(original)
    proposed = original.decode() + '\n\n## Addition\nNew content\n'
    token = stage(route, root, original, proposed)
    assert client.post('/api/docgen/markdown-apply', json={'token': token}).status_code == 400
    response = client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True})
    assert response.status_code == 200, response.json
    assert (root / 'source.md').read_bytes() == proposed.encode()
    op = journal.find_op(journal.load(root, cfg), response.json['op_id'])
    assert journal.revert_op(root, cfg, op)['ok']
    assert (root / 'source.md').read_bytes() == original


def test_stale_and_escape_refused(studio):
    route, client, root, _ = studio
    original = b'# Original\n'
    (root / 'source.md').write_bytes(original)
    token = stage(route, root, original, '# Original\n\n## Addition\n')
    (root / 'source.md').write_text('User edit')
    assert client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True}).status_code == 409
    assert (root / 'source.md').read_text() == 'User edit'
    for path in ('../escape.md', '/tmp/escape.md'):
        assert client.post('/api/docgen/markdown-preview', json={'path': path, 'instruction': 'Add'}).status_code == 400


def test_preview_does_not_write(studio, monkeypatch):
    route, client, root, _ = studio
    original = b'---\ntitle: Test\n---\n# Original\n'
    (root / 'source.md').write_bytes(original)
    runner = Mock()
    runner.chat.return_value = Mock(error=None, text='## Addition\nEvidence (source: research.md)')
    monkeypatch.setattr(route, 'InProcessChatRunner', lambda **kw: runner)
    response = client.post('/api/docgen/markdown-preview', json={'path': 'source.md', 'instruction': 'Add limitations', 'output_kind': 'deck'})
    events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith('data: {')]
    proposal = next(e['markdown_preview'] for e in events if 'markdown_preview' in e)
    assert (root / 'source.md').read_bytes() == original
    saved = json.loads(route._markdown_stage(proposal['token']).read_text())
    assert saved['proposed'].startswith(original.decode() + '\n\n---\n\n')
    assert '<tool_output' in runner.chat.call_args.args[0]


def test_preview_rejects_symlink_and_large_source(studio):
    _, client, root, _ = studio
    (root / 'source.md').write_text('# Original')
    (root / 'alias.md').symlink_to(root / 'source.md')
    assert client.post('/api/docgen/markdown-preview', json={'path':'alias.md', 'instruction':'Add'}).status_code == 400
    (root / 'large.md').write_text('x' * 120_001)
    assert client.post('/api/docgen/markdown-preview', json={'path':'large.md', 'instruction':'Add'}).status_code == 400


def test_generation_change_invalidates_preview(studio, monkeypatch):
    route, client, root, _ = studio
    original = b'# Original\n'
    (root / 'source.md').write_bytes(original)
    token = stage(route, root, original, '# Original\n\n## Addition\n')
    monkeypatch.setattr(route.obsidian_manager, '_index_generation', getattr(route.obsidian_manager, '_index_generation', 0) + 1)
    response = client.post('/api/docgen/markdown-apply', json={'token':token, 'confirm':True})
    assert response.status_code == 409
    assert response.json['publication_outcome'] == 'not_written'
    assert (root / 'source.md').read_bytes() == original


def test_restart_with_reused_generation_rejects_preview(studio, monkeypatch):
    route, client, root, _ = studio
    original = b'# Original\n'
    (root / 'source.md').write_bytes(original)
    token = stage(route, root, original, '# Original\n\n## Old corpus addition\n')
    # A restarted manager can reuse the same integer for a different corpus.
    monkeypatch.setattr(route, '_MARKDOWN_SESSION_ID', 'new-process', raising=False)
    response = client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True})
    assert response.status_code == 409
    assert response.json['publication_outcome'] == 'not_written'
    assert (root / 'source.md').read_bytes() == original


def preview_token(studio, monkeypatch):
    route, client, _, _ = studio
    runner = Mock()
    runner.chat.return_value = Mock(error=None, text='## Addition\nFixture content')
    monkeypatch.setattr(route, 'InProcessChatRunner', lambda **kw: runner)
    response = client.post('/api/docgen/markdown-preview', json={'path': 'source.md', 'instruction': 'Add'})
    events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
              if line.startswith('data: {')]
    return next(e['markdown_preview']['token'] for e in events if 'markdown_preview' in e)


def test_preview_cleanup_bounds_count_and_expires_old_records(studio, monkeypatch):
    route, client, root, _ = studio
    (root / 'source.md').write_text('# Original\n')
    old = route._markdown_stage('b' * 32)
    old.write_text(json.dumps({'created': route.time.time() - route._STAGING_TTL_S - 1}))
    monkeypatch.setattr(route, '_MARKDOWN_STAGE_LIMIT', 2)
    first = preview_token(studio, monkeypatch)
    second = preview_token(studio, monkeypatch)
    third = preview_token(studio, monkeypatch)
    assert not old.exists()
    assert not route._markdown_stage(first).exists()
    assert route._markdown_stage(second).exists()
    assert route._markdown_stage(third).exists()
    assert client.post('/api/docgen/markdown-apply', json={'token': first, 'confirm': True}).status_code == 409
    assert client.post('/api/docgen/markdown-apply', json={'token': third, 'confirm': True}).status_code == 200


def test_uncertain_apply_survives_cleanup_and_cannot_be_replayed(studio, monkeypatch):
    import os
    from vault_history import _write
    route, client, root, _ = studio
    original = b'# Original\n'
    (root / 'source.md').write_bytes(original)
    token = stage(route, root, original, '# Original\n\n## Addition\n')
    real_write = _write.journalled_write_note

    def interrupted_after_write(*args, **kwargs):
        real_write(*args, **kwargs)
        raise OSError('fixture interruption after publication')

    monkeypatch.setattr(_write, 'journalled_write_note', interrupted_after_write)
    response = client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True})
    assert response.status_code == 500
    assert response.json['publication_outcome'] == 'unknown'
    assert response.json['retry_allowed'] is False
    landed = (root / 'source.md').read_bytes()
    assert landed == b'# Original\n\n## Addition\n'
    saved = route._markdown_stage(token)
    record = json.loads(saved.read_text())
    assert record['apply_started'] is True
    record['created'] = route.time.time() - route._STAGING_TTL_S - 1
    saved.write_text(json.dumps(record))
    os.utime(saved, (record['created'], record['created']))
    # Both the legacy LaTeX sweep and new Markdown cleanup preserve recovery.
    route._sweep_staging()
    preview_token(studio, monkeypatch)
    assert saved.exists()
    response = client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True})
    assert response.status_code == 409
    assert 'History and Restore' in response.json['error']
    assert response.json['publication_outcome'] == 'unknown'
    assert (root / 'source.md').read_bytes() == landed


def test_expiry_rejects_apply_without_waiting_for_cleanup(studio):
    route, client, root, _ = studio
    original = b'# Original\n'
    (root / 'source.md').write_bytes(original)
    token = stage(route, root, original, '# Original\n\n## Addition\n')
    saved = route._markdown_stage(token)
    record = json.loads(saved.read_text())
    record['created'] -= route._STAGING_TTL_S + 1
    saved.write_text(json.dumps(record))
    response = client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True})
    assert response.status_code == 409
    assert response.json['publication_outcome'] == 'not_written'
    assert saved.exists()  # Expiry invalidates the token; it is not scheduled deletion.
    assert (root / 'source.md').read_bytes() == original


def test_preview_cleanup_preserves_malformed_evidence(studio, monkeypatch):
    route, _, root, _ = studio
    (root / 'source.md').write_text('# Original\n')
    malformed = route._markdown_stage('c' * 32)
    malformed.write_text('{broken')
    preview_token(studio, monkeypatch)
    assert malformed.read_text() == '{broken'


@pytest.mark.parametrize('failure', ['snapshot', 'write'])
def test_write_rejection_requires_fresh_preview_and_preserves_barrier(studio, monkeypatch, failure):
    from vault_history import journal, _write
    route, client, root, _ = studio
    original = b'# Original\n'
    (root / 'source.md').write_bytes(original)
    token = stage(route, root, original, '# Original\n\n## Addition\n')
    failed_calls = []

    def refuse_write(*args, **kwargs):
        failed_calls.append(1)
        raise OSError('Fixture storage unavailable')

    with monkeypatch.context() as patch:
        if failure == 'snapshot':
            patch.setattr(journal, 'write_note_snapshot', refuse_write)
        else:
            patch.setattr(_write, 'write_text_atomic', refuse_write)
        response = client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True})
        assert response.status_code == 409
        assert response.json['publication_outcome'] == 'not_written'
        assert response.json['retry_allowed'] is False
        assert 'write failed' in response.json['error']
        assert json.loads(route._markdown_stage(token).read_text())['apply_started'] is True
        replay = client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True})
        assert replay.status_code == 409
        assert replay.json['retry_allowed'] is False
        assert replay.json['publication_outcome'] == 'unknown'
        assert len(failed_calls) == 1
        assert (root / 'source.md').read_bytes() == original

    # After fixing storage, a NEW reviewed preview can be applied; the old
    # attempt barrier remains intact and is never cleared to permit replay.
    fresh = preview_token(studio, monkeypatch)
    assert fresh != token
    expected = json.loads(route._markdown_stage(fresh).read_text())['proposed'].encode()
    applied = client.post('/api/docgen/markdown-apply', json={'token': fresh, 'confirm': True})
    assert applied.status_code == 200
    assert (root / 'source.md').read_bytes() == expected
    assert expected.startswith(original) and len(expected) > len(original)
    assert json.loads(route._markdown_stage(token).read_text())['apply_started'] is True


@pytest.mark.parametrize('busy', ['path', 'vault'])
def test_busy_rejection_leaves_same_preview_retryable(studio, monkeypatch, busy):
    route, client, root, _ = studio
    original = b'# Original\n'
    proposed = '# Original\n\n## Addition\n'
    (root / 'source.md').write_bytes(original)
    token = stage(route, root, original, proposed)
    with monkeypatch.context() as patch:
        if busy == 'path':
            patch.setattr(route._DOCGEN_OP_LOCK, 'acquire', lambda *a, **kw: None)
        else:
            patch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda **kw: None)
        response = client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True})
        assert response.status_code == 409
        assert response.json['publication_outcome'] == 'not_written'
        assert response.json['retry_allowed'] is True
        assert not json.loads(route._markdown_stage(token).read_text()).get('apply_started')
        assert (root / 'source.md').read_bytes() == original
    response = client.post('/api/docgen/markdown-apply', json={'token': token, 'confirm': True})
    assert response.status_code == 200
    assert (root / 'source.md').read_bytes() == proposed.encode()
