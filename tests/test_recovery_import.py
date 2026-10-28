"""Fixture-only import admission; no vault, providers or production writes."""
import hashlib
import json

import fitz
import pytest

from core.page_ocr import extract_pages, export_pages
from core.recovery_import import _read, prepare


class NoOCR:
    def identity(self):
        return {'fixture': 'native-only'}

    def recognize(self, *_):
        pytest.fail('Native fixture should not invoke OCR')


def fixture(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'paper.pdf'
    with fitz.open() as doc:
        doc.new_page().insert_text((40, 40), 'Full native evidence with enough readable text to require no OCR.')
        doc.save(source)
    base = tmp_path / 'base'
    root = base / 'document_recovery' / '2026-10-27-12345678'
    bundle = extract_pages(source, vault, root / 'attempts', NoOCR(), resource_guard=lambda: '')
    export = export_pages(bundle, root / 'exports', vault)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    review = root / 'review.md'
    review.write_text(bundle['payload']['text'])
    (root / 'selection.json').write_text(json.dumps({'vault': str(vault), 'provider': 'deepseek',
        'sources': [{'source': 'paper.pdf', 'source_sha256': sha}]}))
    catalog = {'accepted': [{'sources': ['paper.pdf'], 'source_sha256': sha,
        'structurally_complete': True, 'review_copy': str(review),
        'review_copy_sha256': hashlib.sha256(review.read_bytes()).hexdigest(), 'raw_export': str(export)}]}
    raw = json.dumps(catalog).encode()
    (root / 'review-catalog-v2.json').write_bytes(raw)
    return base, vault, root, hashlib.sha256(raw).hexdigest()


def test_complete_source_import_is_observational(tmp_path):
    base, vault, root, binding = fixture(tmp_path)
    before = (vault / 'paper.pdf').read_bytes()
    _, rows, deferred = prepare(base, vault, root.name, binding)
    assert len(rows) == 1 and deferred == []
    assert 'Full native evidence' in rows[0][4]
    assert (vault / 'paper.pdf').read_bytes() == before
    assert not (root / 'import-started.json').exists()


@pytest.mark.parametrize('changed', ['source', 'review', 'snapshot', 'catalog'])
def test_changed_evidence_refuses_before_publication(tmp_path, changed):
    base, vault, root, binding = fixture(tmp_path)
    if changed == 'source':
        (vault / 'paper.pdf').write_bytes(b'changed')
    elif changed == 'review':
        (root / 'review.md').write_text('changed')
    elif changed == 'catalog':
        (root / 'review-catalog-v2.json').write_text('{}')
    else:
        path = next((root / 'exports').glob('*.json'))
        data = json.loads(path.read_text())
        data['pages'][0]['text'] = 'invented'
        path.write_text(json.dumps(data))
    with pytest.raises((ValueError, fitz.FileDataError)):
        prepare(base, vault, root.name, binding)
    assert not (root / 'import-started.json').exists()


def test_evidence_traversal_and_symlink_are_refused(tmp_path):
    root = tmp_path / 'recovery'
    root.mkdir()
    other = tmp_path / 'other'
    other.write_text('outside')
    (root / 'link').symlink_to(other)
    for path in (root / '..' / 'other', root / 'link'):
        with pytest.raises(ValueError):
            _read(root, path)


def test_import_endpoint_requires_both_confirmations(monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda **_: pytest.fail('Unconfirmed import acquired lock'))
    assert app.test_client().post('/api/online-ocr/recovery/import', json={}).status_code == 400


def test_confirmed_import_uses_shared_writer_and_preserves_replay_barrier(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import online_ocr as route
    base, vault, root, binding = fixture(tmp_path)
    (vault / 'copy.pdf').write_bytes((vault / 'paper.pdf').read_bytes())
    selection = json.loads((root / 'selection.json').read_text())
    selection['sources'].append({**selection['sources'][0], 'source': 'copy.pdf'})
    (root / 'selection.json').write_text(json.dumps(selection))
    catalog = json.loads((root / 'review-catalog-v2.json').read_text())
    catalog['accepted'][0]['sources'].append('copy.pdf')
    raw = json.dumps(catalog).encode()
    (root / 'review-catalog-v2.json').write_bytes(raw)
    binding = hashlib.sha256(raw).hexdigest()
    app = Flask(__name__)
    app.register_blueprint(route.online_ocr_bp)
    monkeypatch.setattr(route, 'BASE_DIR', str(base))
    monkeypatch.setattr(route, '_job', {'state': 'idle'})
    monkeypatch.setattr(route, 'load_config', lambda: {'obsidian_vault_path': str(vault)})
    monkeypatch.setattr(route, '_index_settings', lambda cfg: {'fixture': 'same-local-model'})
    monkeypatch.setattr(route, '_preflight_index_settings', lambda *_: None)
    monkeypatch.setattr(route.obsidian_manager, 'wait_for_indexing', lambda **_: True)
    monkeypatch.setattr(route.obsidian_manager, 'try_acquire_lock', lambda **_: 77)
    monkeypatch.setattr(route.obsidian_manager, 'release_lock', lambda *_: None)
    monkeypatch.setattr(route.obsidian_manager, 'register_index_thread', lambda *_: None)
    monkeypatch.setattr(route.obsidian_manager, '_selected_pdf_payload', lambda *_: None)
    digest = hashlib.sha256((vault / 'paper.pdf').read_bytes()).hexdigest()
    monkeypatch.setattr(route, '_session_inventory', lambda *_, **__: {
        'sources': [{'source': rel, 'source_sha256': digest} for rel in ('paper.pdf', 'copy.pdf')], 'reused': []})
    calls = []
    def shared(selected, epoch, **kwargs):
        calls.append((selected, epoch, kwargs))
        route._job['state'] = 'indexed'
    monkeypatch.setattr(route, '_promote_session_selected', shared)
    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(route.threading, 'Thread', Thread)
    body = {'recovery_id': root.name, 'catalog_sha256': binding,
            'confirm_import': True, 'confirm_index': True}
    response = app.test_client().post('/api/online-ocr/recovery/import', json=body)
    assert response.status_code == 200
    assert len(calls) == 1 and calls[0][1] == 77
    assert len(calls[0][0]) == 1 and response.json['accepted'] == 2
    assert response.json['cache_count'] == 1
    assert calls[0][2]['run_index'] is True
    assert calls[0][2]['manifest']['approved'] == {'paper.pdf': digest, 'copy.pdf': digest}
    assert (root / 'import-started.json').is_file()
    assert app.test_client().post('/api/online-ocr/recovery/import', json=body).status_code == 409
    assert len(calls) == 1


def test_source_symlink_is_refused_before_pdf_parser_reads_it(tmp_path, monkeypatch):
    base, vault, root, binding = fixture(tmp_path)
    source = vault / 'paper.pdf'
    outside = tmp_path / 'outside.pdf'
    source.rename(outside)
    source.symlink_to(outside)
    monkeypatch.setattr(fitz, 'open', lambda *_: pytest.fail('Parser read an uncontained source'))
    with pytest.raises(ValueError, match='Unsafe recovery source'):
        prepare(base, vault, root.name, binding)
    assert not (root / 'import-started.json').exists()
