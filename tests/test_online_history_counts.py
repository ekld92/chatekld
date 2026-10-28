"""History pagination stays observational and invalidates cached receipt counts."""
import json
from unittest.mock import patch

import pytest

from core import online_runs


def history(tmp_path, count=120):
    directory = tmp_path / 'online_index_runs' / ('a' * 32)
    directory.mkdir(parents=True)
    (directory / 'run.json').write_text(json.dumps({'vault_key': 'v', 'source_count': count}))
    (directory / 'inventory.json').write_text(json.dumps({'sources': [
        {'source': f'{i}.pdf'} for i in range(count)]}))
    for i in range(count):
        (directory / f'source-{i:06d}.json').write_text(json.dumps({
            'source': f'{i}.pdf', 'status': 'ocr'}))
    return directory


def inspect(tmp_path, **kwargs):
    return online_runs.inspect_run(tmp_path, 'a' * 32, 'v', limit=50, **kwargs)


def test_next_page_reads_only_page_receipts_and_keeps_exact_totals(tmp_path):
    directory = history(tmp_path)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert inspect(tmp_path)['completed_count'] == 120
    with patch.object(online_runs, 'read_record', wraps=online_runs.read_record) as read:
        result = inspect(tmp_path, offset=50)
    assert read.call_count == 52  # run, inventory, and fifty source receipts
    assert result['completed_count'] == 120
    assert result['unfinished_count'] == 0
    assert result['sources'][0]['source'] == '50.pdf'
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize('change', ['retry', 'edit', 'delete', 'malformed', 'symlink'])
def test_off_page_changes_invalidate_counts(tmp_path, change):
    directory = history(tmp_path)
    inspect(tmp_path)
    source = directory / 'source-000000.json'
    failed = json.dumps({'source': '0.pdf', 'status': 'failed'})
    if change == 'retry':
        (directory / 'source-000000-r001.json').write_text(failed)
    elif change == 'edit':
        source.write_text(failed)
    elif change == 'delete':
        source.unlink()
    elif change == 'malformed':
        source.write_text('{broken')
    else:
        source.unlink()
        source.symlink_to(directory / 'source-000001.json')
    if change in {'malformed', 'symlink'}:
        with pytest.raises(ValueError):
            inspect(tmp_path, offset=50)
    else:
        result = inspect(tmp_path, offset=50)
        assert result['completed_count'] == 119
        assert result['unfinished_count'] == 1


def test_known_failed_content_cache_hit_and_skip_read_record(tmp_path):
    run_dir = tmp_path / 'online_index_runs' / ('b' * 32)
    run_dir.mkdir(parents=True)
    (run_dir / 'run.json').write_text(json.dumps({'vault_key': 'v_test'}))
    (run_dir / 'state-000000.json').write_text(json.dumps({'state': 'text_prepared'}))
    (run_dir / 'source-000000.json').write_text(json.dumps({
        'source': 'a.pdf', 'status': 'failed', 'source_sha256': 'a' * 64,
        'document_identity': {'ino': 10, 'size': 20},
        'recorded_at_ns': 1000,
    }))
    (run_dir / 'source-000001.json').write_text(json.dumps({
        'source': 'b.pdf', 'status': 'failed', 'source_sha256': 'b' * 64,
        'recorded_at_ns': 2000,
    }))

    # Initial call populates the cache
    res1 = online_runs.known_failed_content(tmp_path, 'v_test', all_evidence=True)
    assert len(res1) == 2
    assert {r['source_sha256'] for r in res1} == {'a' * 64, 'b' * 64}

    # Second call hits cache: read_record is not called at all
    with patch.object(online_runs, 'read_record', wraps=online_runs.read_record) as read:
        res2 = online_runs.known_failed_content(tmp_path, 'v_test', all_evidence=True)
        assert res2 == res1
        assert read.call_count == 0

    # Caller mutating returned copy (outer or nested document_identity) must NOT poison cache
    res2[0]['source'] = 'poisoned.pdf'
    res2[0]['document_identity']['ino'] = 9999
    res3 = online_runs.known_failed_content(tmp_path, 'v_test', all_evidence=True)
    assert all(r['source'] != 'poisoned.pdf' for r in res3)
    assert res3[0]['document_identity']['ino'] == 10


def test_known_failed_content_invalidation_on_new_retry_or_change(tmp_path):
    run_dir = tmp_path / 'online_index_runs' / ('c' * 32)
    run_dir.mkdir(parents=True)
    (run_dir / 'run.json').write_text(json.dumps({'vault_key': 'v_retry'}))
    (run_dir / 'state-000000.json').write_text(json.dumps({'state': 'text_prepared'}))
    (run_dir / 'source-000000.json').write_text(json.dumps({
        'source': 'retry.pdf', 'status': 'failed', 'source_sha256': 'c' * 64,
        'recorded_at_ns': 1000,
    }))

    res1 = online_runs.known_failed_content(tmp_path, 'v_retry')
    assert 'c' * 64 in res1

    # Adding a retry receipt invalidates the signature and clears failure
    (run_dir / 'source-000000-r001.json').write_text(json.dumps({
        'source': 'retry.pdf', 'status': 'processed_unverified', 'source_sha256': 'c' * 64,
        'retry_authorized': True, 'recorded_at_ns': 2000,
    }))

    res2 = online_runs.known_failed_content(tmp_path, 'v_retry')
    assert 'c' * 64 not in res2


def test_known_failed_content_symlink_rejection(tmp_path):
    run_dir = tmp_path / 'online_index_runs' / ('d' * 32)
    run_dir.mkdir(parents=True)
    (run_dir / 'run.json').write_text(json.dumps({'vault_key': 'v_sym'}))
    (run_dir / 'source-000000.json').write_text(json.dumps({
        'source': 's.pdf', 'status': 'failed', 'source_sha256': 'd' * 64,
        'recorded_at_ns': 1000,
    }))

    # Cache the valid run
    online_runs.known_failed_content(tmp_path, 'v_sym')

    # Replace source receipt with symlink
    source = run_dir / 'source-000000.json'
    target = tmp_path / 'outside.json'
    target.write_text('{}')
    source.unlink()
    source.symlink_to(target)

    # Must raise ValueError
    with pytest.raises(ValueError, match="Missing, unsafe or oversized run evidence"):
        online_runs.known_failed_content(tmp_path, 'v_sym')
