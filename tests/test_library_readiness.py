"""Saved evidence never certifies corpus coverage or performs recovery."""
import json

import pytest

from core.library_readiness import library_readiness


def seed(root, **changes):
    meta = {"vault_path": "/fixture", "has_vector_data": True,
            "partial": False, "indexed_at": "2026-10-26T12:00:00+00:00"}
    meta.update(changes)
    manifest = {"vault_path": "/fixture", "indexed_at": "2026-10-25T12:00:00+00:00",
                "materials": [{"source": "one.pdf", "chunk_count": 8},
                              {"source": "one.pdf", "chunk_count": 2},
                              {"source": "two.md", "chunk_count": 1}]}
    for name, data in [("obsidian_meta.json", meta), ("indexed_materials.json", manifest)]:
        (root / name).write_text(json.dumps(data))


def test_snapshot_counts_sources_not_chunks_or_searchable_documents(tmp_path, monkeypatch):
    seed(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    def forbidden(*a, **kw):
        pytest.fail("Status must not discover the corpus")
    monkeypatch.setattr('pathlib.Path.rglob', forbidden)
    result = library_readiness(str(tmp_path), "/fixture")
    assert result['state'] == 'snapshot'
    assert result['recorded_sources'] == 2
    assert result['counts'] == dict.fromkeys(
        ['discovered', 'complete_caches', 'searchable', 'pending_changed', 'deferred'])
    assert result['indexed_at'] == '2026-10-26T12:00:00+00:00'
    assert result['manifest_at'] == '2026-10-25T12:00:00+00:00'
    assert result['observed_at']
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize('changes,state', [({'partial': True}, 'partial'),
    ({'vault_path': '/other'}, 'stale'), ({'vault_path': ''}, 'stale'),
    ({'has_vector_data': False}, 'unknown')])
def test_partial_legacy_and_wrong_vault_never_claim_readiness(tmp_path, changes, state):
    seed(tmp_path, **changes)
    result = library_readiness(str(tmp_path), '/fixture')
    assert result['state'] == state
    assert result['counts']['searchable'] is None
    if state == 'stale':
        assert result['recorded_sources'] is None


@pytest.mark.parametrize('contents', ['{', '[]', '{"materials": [null]}',
    '{"vault_path":"/fixture","materials":[{"source":"a","chunk_count":true}]}'])
def test_invalid_evidence_is_unavailable_not_zero(tmp_path, contents):
    seed(tmp_path)
    (tmp_path / 'indexed_materials.json').write_text(contents)
    result = library_readiness(str(tmp_path), '/fixture')
    assert result['recorded_sources'] is None
    assert result['state'] in {'stale', 'unavailable'}


def test_absent_evidence_does_not_create_files(tmp_path):
    assert library_readiness(str(tmp_path), '/fixture')['state'] == 'unavailable'
    assert library_readiness(str(tmp_path), None)['state'] == 'not_configured'
    assert list(tmp_path.iterdir()) == []


def test_route_uses_saved_evidence_without_manager_recovery(tmp_path, monkeypatch):
    from flask import Flask
    from api.routes import vault
    seed(tmp_path)
    monkeypatch.setattr('core.constants.OBSIDIAN_INDEX_DIR', str(tmp_path))
    monkeypatch.setattr(vault.obsidian_manager, 'get_vault_path', lambda: '/fixture')
    def forbidden(*a, **kw):
        pytest.fail('Readiness may not consume status or recover materials')
    monkeypatch.setattr(vault.obsidian_manager, 'get_status_payload', forbidden)
    monkeypatch.setattr(vault.obsidian_manager, 'get_indexed_materials', forbidden)
    app = Flask(__name__)
    from api.security import register_origin_guard
    register_origin_guard(app)
    app.register_blueprint(vault.vault_bp)
    client = app.test_client()
    assert client.get('/api/obsidian/readiness').status_code == 403
    headers = {'X-Requested-With': 'ChatEKLD'}
    assert client.get('/api/obsidian/readiness', headers={**headers, 'Origin': 'https://foreign.example'}).status_code == 403
    assert client.get('/api/obsidian/readiness', headers={**headers, 'Host': 'foreign.example'}).status_code == 403
    response = client.get('/api/obsidian/readiness', headers=headers)
    assert response.status_code == 200
    assert response.json['recorded_sources'] == 2
    assert response.json['counts']['pending_changed'] is None


@pytest.mark.parametrize('contents', [b'\xff', b'{"nested":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}'], ids=['encoding', 'deep-json'])
def test_bad_encoding_and_deep_json_fail_closed(tmp_path, contents):
    seed(tmp_path)
    (tmp_path / 'indexed_materials.json').write_bytes(contents)
    result = library_readiness(str(tmp_path), '/fixture')
    assert result['state'] == 'unavailable'
    assert result['recorded_sources'] is None
    assert result['indexed_at'] is None


@pytest.mark.parametrize('changes', [{'partial': 'false'}, {'indexed_at': {}},
                                    {'indexed_at': 'not-a-date'}])
def test_malformed_metadata_does_not_supply_dated_counts(tmp_path, changes):
    seed(tmp_path, **changes)
    assert library_readiness(str(tmp_path), '/fixture')['state'] == 'unavailable'


def test_byte_limit_and_non_regular_evidence(tmp_path, monkeypatch):
    from core import library_readiness as module
    seed(tmp_path)
    monkeypatch.setattr(module, '_MAX_EVIDENCE_BYTES', 32)
    assert module.library_readiness(str(tmp_path), '/fixture')['state'] == 'unavailable'
    path = tmp_path / 'indexed_materials.json'
    path.unlink()
    path.mkdir()
    assert module.library_readiness(str(tmp_path), '/fixture')['state'] == 'unavailable'


@pytest.mark.parametrize('changed_name', ['obsidian_meta.json', 'indexed_materials.json'])
def test_concurrent_atomic_publication_invalidates_observation(tmp_path, monkeypatch, changed_name):
    from core import library_readiness as module
    seed(tmp_path)
    original = module._read_object
    def read_and_publish(path):
        value = original(path)
        if path.name == 'indexed_materials.json':
            target = tmp_path / changed_name
            replacement = tmp_path / 'replacement.json'
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
        return value
    monkeypatch.setattr(module, '_read_object', read_and_publish)
    result = module.library_readiness(str(tmp_path), '/fixture')
    assert result['state'] == 'unavailable'
    assert result['recorded_sources'] is None
    assert result['manifest_at'] is None


def test_missing_dates_and_empty_manifest_are_explicit_unknowns(tmp_path):
    seed(tmp_path, indexed_at=None)
    (tmp_path / 'indexed_materials.json').write_text(
        json.dumps({'vault_path': '/fixture', 'materials': []}))
    result = library_readiness(str(tmp_path), '/fixture')
    assert result['recorded_sources'] == 0
    assert result['manifest_at'] is None
    assert result['indexed_at'] is None
    assert result['counts']['searchable'] is None
