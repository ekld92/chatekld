"""Canonical export safety, coverage and independence from ingestion."""
import hashlib
import json
from pathlib import Path

import pytest

from core.practical_payload import canonical, export_markdown_batch


def entry(root, name='paper', outcome='processed'):
    root.mkdir(exist_ok=True)
    source = root / (name + '.pdf')
    source.write_bytes(name.encode())
    payload = canonical(source, hashlib.sha256(source.read_bytes()).hexdigest(), 'recipe',
                        [(1, '# Original heading\n\nA | B\n\n1 | 2'), (2, 'Literal quasi- discrepancy')],
                        outcome=outcome)
    return dict(source=str(source), recipe='recipe', payload=payload)


def test_discoverable_exact_content_and_idempotent_export(tmp_path):
    item = entry(tmp_path / 'inputs')
    item['association'] = {'citation_keys': ['Smith2026'],
                           'verified_sha256': item['payload']['source_sha256']}
    before = json.dumps(item['payload'], sort_keys=True)
    target = tmp_path / 'exports'
    result = export_markdown_batch([item], target, input_roots=[tmp_path / 'inputs'])
    assert result['coverage'] == {'exported': 1}
    raw = (target / 'Smith2026.md').read_text()
    assert '# Original heading\n\nA | B\n\n1 | 2' in raw
    assert 'Physical PDF page 2' in raw and Path(item['source']).as_uri() in raw
    assert before == json.dumps(item['payload'], sort_keys=True)
    assert export_markdown_batch([item], target, input_roots=[tmp_path / 'inputs']) == result
    (target / 'Smith2026.md').write_text('User notes')
    with pytest.raises(ValueError, match='user-edited'):
        export_markdown_batch([item], target, input_roots=[tmp_path / 'inputs'])
    assert (target / 'Smith2026.md').read_text() == 'User notes'


def test_inventory_accounts_for_missing_partial_failed_empty_and_stale(tmp_path):
    root = tmp_path / 'inputs'
    items = [entry(root, 'partial', 'partial'), entry(root, 'stale'), entry(root, 'empty')]
    Path(items[1]['source']).write_bytes(b'changed')
    items[2]['payload'] = canonical(Path(items[2]['source']), items[2]['payload']['source_sha256'],
                                    'recipe', [(0, '')], outcome='processed')
    items += [dict(source=str(root / 'missing.pdf'), recipe='recipe'),
              dict(source=str(root / 'failed.pdf'), recipe='recipe', error='Extraction failed')]
    result = export_markdown_batch(items, tmp_path / 'exports', input_roots=[root])
    assert result['coverage'] == dict(partial=1, stale_or_invalid=1, empty=1, missing=1, failed=1)
    assert len(list((tmp_path / 'exports').glob('*.md'))) == 2
    assert 'Partial source content' in (tmp_path / 'exports/partial.md').read_text()


def test_collisions_and_unverified_citations_do_not_choose_wrong_source(tmp_path):
    roots = [tmp_path / 'a', tmp_path / 'b']
    items = [entry(root) for root in roots]
    items[0]['association'] = {'citation_keys': ['wrong'], 'verified_sha256': 'wrong'}
    result = export_markdown_batch(items, tmp_path / 'exports', input_roots=roots)
    names = [row['file'] for row in result['rows']]
    assert len(set(names)) == 2 and all(name.startswith('paper__') for name in names)
    assert all(row['citation_key'] is None for row in result['rows'])
    reverse = export_markdown_batch(list(reversed(items)), tmp_path / 'exports', input_roots=roots)
    assert reverse == result


def test_outside_all_ingestion_roots_and_symlink_refusal(tmp_path):
    root = tmp_path / 'inputs'
    item = entry(root)
    notes = tmp_path / 'notes'
    notes.mkdir()
    for destination in [root / 'exports', notes / 'exports', tmp_path]:
        with pytest.raises(ValueError, match='outside'):
            export_markdown_batch([item], destination, input_roots=[root, notes])
    (tmp_path / 'link').symlink_to(notes, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlink'):
        export_markdown_batch([item], tmp_path / 'link/exports', input_roots=[root])


def test_recipe_staleness_reserved_name_and_unknown_pages(tmp_path):
    root = tmp_path / 'inputs'
    item = entry(root, 'index')
    item['payload'] = canonical(Path(item['source']), item['payload']['source_sha256'],
                                'recipe', [(0, 'Legacy content')], outcome='baseline_fallback')
    result = export_markdown_batch([item], tmp_path / 'exports', input_roots=[root])
    assert result['rows'][0]['file'].startswith('index__')
    assert result['rows'][0]['page_provenance'] == 'unknown'
    item['recipe'] = 'different'
    result = export_markdown_batch([item], tmp_path / 'stale', input_roots=[root])
    assert result['coverage'] == {'stale_or_invalid': 1}
