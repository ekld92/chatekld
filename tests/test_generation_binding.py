"""Whole generation rehearsal on disposable bytes, not live index acceptance."""
import hashlib
import json

import pytest

from core.pdf_text_profile import activate_profile, prepare_profile
from core.practical_payload import canonical
from core.upgrade_snapshot import (create_generation_snapshot, generation_binding,
                                   restore_generation_to_empty)


def generation(tmp_path, name, text):
    base = tmp_path / name
    base.mkdir()
    vault = tmp_path / 'vault'
    vault.mkdir(exist_ok=True)
    source = vault / 'source.pdf'
    source.write_bytes(b'fixture pdf')
    record = canonical(source, hashlib.sha256(source.read_bytes()).hexdigest(), 'fixture',
                       [(1, text)], outcome='processed')
    root = base / 'pdf_text_profiles'
    identifier = prepare_profile(root, vault, [record], qualification_sha256='b' * 64)
    activate_profile(root, identifier, expected_current=None, confirm=True, approval='fixture')
    config = {'embed': 'fixture-model', 'embed_provider': 'lm_studio',
              'vault_vector_backend': 'simple', 'obsidian_vault_path': str(vault)}
    meta = {**config, 'vault_path': str(vault), 'vector_backend': 'simple',
            'pdf_text_profile': identifier, 'partial': False, 'phase': 'done', 'has_vector_data': True}
    (base / 'config.json').write_text(json.dumps(config))
    (base / 'embedding-identity.json').write_text(json.dumps(
        {'alias': 'fixture-model', 'backend': 'lm_studio', 'dimension': 2, 'artifact_sha256': 'a' * 64}))
    index = base / 'obsidian_storage'
    index.mkdir()
    (index / 'obsidian_meta.json').write_text(json.dumps(meta))
    (index / 'docstore.json').write_text(json.dumps({'fixture': text}))
    (index / 'default__vector_store.json').write_text('{"fixture": [1, 0]}')
    return base


def test_whole_generation_restore_rollback_and_mixed_pointer_refusal(tmp_path):
    a, b = generation(tmp_path, 'a', 'complete A'), generation(tmp_path, 'b', 'complete B')
    for base in (a, b):
        create_generation_snapshot(base, tmp_path / (base.name + '-snapshot'), source_state='fixture quiesced')
    for name in ('a', 'b', 'a'):
        dest = tmp_path / ('restore-' + name + str(len(list(tmp_path.glob('restore-*')))))
        result = restore_generation_to_empty(tmp_path / (name + '-snapshot'), dest)
        assert result['binding'] == generation_binding(tmp_path / name)
    (a / 'pdf_text_profiles/active.json').write_bytes((b / 'pdf_text_profiles/active.json').read_bytes())
    with pytest.raises(ValueError):
        generation_binding(a)


@pytest.mark.parametrize('change', ['partial', 'embed', 'vault_path'])
def test_incomplete_or_mismatched_generation_refused(tmp_path, change):
    base = generation(tmp_path, 'a', 'complete')
    path = base / 'obsidian_storage/obsidian_meta.json'
    meta = json.loads(path.read_text())
    meta[change] = True if change == 'partial' else 'other'
    path.write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        create_generation_snapshot(base, tmp_path / 'snapshot', source_state='fixture')
    assert not (tmp_path / 'snapshot').exists()


def test_changed_index_snapshot_or_source_refused(tmp_path):
    base = generation(tmp_path, 'a', 'complete')
    snapshot = tmp_path / 'snapshot'
    create_generation_snapshot(base, snapshot, source_state='fixture')
    (snapshot / 'data/obsidian_storage/docstore.json').write_text('{}')
    with pytest.raises(ValueError, match='inventory'):
        restore_generation_to_empty(snapshot, tmp_path / 'restore')
    (tmp_path / 'vault/source.pdf').write_bytes(b'changed')
    with pytest.raises(ValueError, match='Stale'):
        generation_binding(base)


def test_binding_receipt_cannot_be_changed_or_omitted(tmp_path):
    base = generation(tmp_path, 'a', 'complete')
    snapshot = tmp_path / 'snapshot'
    create_generation_snapshot(base, snapshot, source_state='fixture')
    (snapshot / 'generation-binding.json').write_text('{}')
    with pytest.raises(ValueError, match='receipt'):
        restore_generation_to_empty(snapshot, tmp_path / 'restore')
    assert not (tmp_path / 'restore').exists()


@pytest.mark.parametrize('raw', ['{"state": "promoting"}', '{}', '[]', 'broken'])
def test_done_metadata_cannot_hide_interrupted_checkpoint_promotion(tmp_path, raw):
    base = generation(tmp_path, 'a', 'complete')
    marker = base / 'obsidian_storage/promotion_state.json'
    marker.write_text(raw)
    with pytest.raises(ValueError, match='promotion'):
        create_generation_snapshot(base, tmp_path / 'snapshot', source_state='fixture')
    assert not (tmp_path / 'snapshot').exists()
    marker.write_text('{"state": "complete"}')
    assert generation_binding(base)['text_generation']
