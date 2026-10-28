"""Opt-in profile routing and recovery; all source/data paths are disposable."""
import hashlib
import json
from unittest.mock import patch

import pytest

from core.pdf_text_profile import activate_profile, prepare_profile, selected_payload
from core.practical_payload import canonical


def setup_profile(tmp_path, text='Readable canonical text', outcome='processed'):
    vault = tmp_path / 'vault'
    vault.mkdir(exist_ok=True)
    source = vault / 'source.pdf'
    source.write_bytes(b'original PDF source identity')
    record = canonical(source, hashlib.sha256(source.read_bytes()).hexdigest(), 'automatic-recipe',
                       [(1, text)], outcome=outcome)
    root = tmp_path / 'app-data/pdf_text_profiles'
    generation = prepare_profile(root, vault, [record], qualification_sha256='a' * 64)
    return vault, source, record, root, generation


def test_preparation_never_selects_text_and_activation_needs_fresh_approval(tmp_path):
    vault, source, record, root, generation = setup_profile(tmp_path)
    assert selected_payload(root, vault, source) is None
    with pytest.raises(ValueError, match='confirmation'):
        activate_profile(root, generation, expected_current=None, confirm=False, approval='')
    activate_profile(root, generation, expected_current=None, confirm=True, approval='disposable test only')
    assert selected_payload(root, vault, source) == record
    with pytest.raises(ValueError, match='Stale activation'):
        activate_profile(root, generation, expected_current=None, confirm=True, approval='disposable test')
    before = (root / 'active.json').read_bytes()
    source.write_bytes(b'changed source')
    with pytest.raises(ValueError, match='Stale'):
        selected_payload(root, vault, source)
    assert (root / 'active.json').read_bytes() == before


def test_partial_candidate_cannot_be_prepared(tmp_path):
    with pytest.raises(ValueError, match='Partial text'):
        setup_profile(tmp_path, outcome='partial')
    assert not (tmp_path / 'app-data/pdf_text_profiles').exists()


def test_historical_partial_profile_refused_at_read_and_activation(tmp_path):
    # Construct the format accepted by the old writer, with valid hashes.
    from core.pdf_text_profile import _bytes, _hash
    vault, source, record, root, generation = setup_profile(tmp_path)
    manifest = json.loads((root / generation / 'manifest.json').read_text())
    record['outcome'] = 'partial'
    raw = _bytes(record)
    row = manifest['rows'][0]
    row.update(sha256=_hash(raw), payload=_hash(raw) + '.json')
    generation = _hash(_bytes(manifest))
    directory = root / generation
    directory.mkdir()
    (directory / row['payload']).write_bytes(raw)
    (directory / 'manifest.json').write_bytes(_bytes(manifest))
    with pytest.raises(ValueError, match='Partial text'):
        activate_profile(root, generation, expected_current=None,
                         confirm=True, approval='disposable old-format fixture')
    (root / 'active.json').write_text(json.dumps(
        {'schema': 1, 'generation': generation, 'approval': 'historical fixture'}))
    with pytest.raises(ValueError, match='Partial text'):
        selected_payload(root, vault, source)


def test_generation_switch_and_rollback_keep_before_images(tmp_path):
    vault, source, record, root, first = setup_profile(tmp_path)
    second_record = {**record, 'text': 'Second canonical payload',
                     'text_sha256': hashlib.sha256(b'Second canonical payload').hexdigest(),
                     'pages': [{'physical_page': 1, 'start': 0, 'end': 24}]}
    second = prepare_profile(root, vault, [second_record], qualification_sha256='b' * 64)
    activate_profile(root, first, expected_current=None, confirm=True, approval='fixture switch')
    activate_profile(root, second, expected_current=first, confirm=True, approval='fixture switch')
    assert selected_payload(root, vault, source)['text'] == second_record['text']
    activate_profile(root, first, expected_current=second, confirm=True, approval='fixture rollback')
    assert selected_payload(root, vault, source) == record
    assert len(list((root / 'activation-history').glob('*/before.json'))) == 3


def test_activated_payload_uses_pdf_loader_and_read_cache_without_extraction(tmp_path, monkeypatch):
    from rag import vault as module
    vault, source, record, root, generation = setup_profile(tmp_path)
    monkeypatch.setattr(module, 'OBSIDIAN_INDEX_DIR', str(root.parent / 'obsidian_storage'))
    monkeypatch.setattr(module, 'OBSIDIAN_CACHE_DIR', str(root.parent / 'obsidian_cache'))
    activate_profile(root, generation, expected_current=None, confirm=True, approval='fixture integration')
    manager = module.ObsidianVaultManager()
    with patch.object(module, 'extract_structured_from_pdf', side_effect=AssertionError('extraction')):
        docs = list(manager._load_vault_documents(str(vault)))
        read = manager._read_pdf_text(source, vault, 10)
    assert len(docs) == 1 and docs[0].metadata['extension'] == '.pdf'
    assert docs[0].text == record['text']
    assert read == (record['text'][:10], True)
    assert read.extraction_status.origin == 'practical_pdf_text'
    assert read.extraction_status.completeness == 'partial'
    assert not list((root.parent / 'readable_pdf_exports').rglob('*.md'))


def test_corrupt_payload_visible_fallback_preserves_legacy_cache(tmp_path, monkeypatch):
    from rag import vault as module
    vault, source, record, root, generation = setup_profile(tmp_path)
    monkeypatch.setattr(module, 'OBSIDIAN_INDEX_DIR', str(root.parent / 'obsidian_storage'))
    monkeypatch.setattr(module, 'OBSIDIAN_CACHE_DIR', str(root.parent / 'obsidian_cache'))
    activate_profile(root, generation, expected_current=None, confirm=True, approval='fixture integration')
    manifest = json.loads((root / generation / 'manifest.json').read_text())
    (root / generation / manifest['rows'][0]['payload']).write_text('corrupted payload')
    manager = module.ObsidianVaultManager()
    messages = []
    manager._emit = messages.append
    sig = manager._pdf_file_signature(source, {}, source.name)
    manager._save_pdf_cache_file(manager._pdf_cache_file(vault, sig), 'Preserved legacy content')
    with patch.object(module, 'extract_structured_from_pdf', side_effect=AssertionError('extraction')):
        docs = list(manager._load_vault_documents(str(vault)))
    assert docs[0].text == 'Preserved legacy content'
    assert any('profile refused' in message for message in messages)


def test_activation_refuses_profile_copied_inside_vault_before_creating_lock(tmp_path):
    import shutil
    vault, source, record, root, generation = setup_profile(tmp_path)
    unsafe = vault / 'untrusted-profile-copy'
    shutil.copytree(root, unsafe)
    with pytest.raises(ValueError, match='outside the vault'):
        activate_profile(unsafe, generation, expected_current=None, confirm=True, approval='fixture only')
    assert not (unsafe / 'activation.lock').exists()
