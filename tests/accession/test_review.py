from concurrent.futures import ThreadPoolExecutor

import pytest
from accession import review


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(review, 'store_path', lambda: tmp_path / 'reviews.sqlite3')
    root = tmp_path / 'vault'
    root.mkdir()
    return root


def proposal(doi='10.1234/one'):
    return dict(candidates=[dict(doi=doi, title='A paper', authors=['Smith'])])


def opened(vault, rel='one.pdf'):
    path = vault / rel
    path.write_bytes(b'pdf content')
    return review.open_review(vault, rel, review.identity(path), proposal())


def confirm(vault, record):
    return review.update(vault, record['rel'], record['token'], review.identity(vault / record['rel']),
                         action='confirm', candidate_id=review.digest(record['proposal']['candidates'][0]))


def test_separate_copies_rename_and_change(vault):
    first = confirm(vault, opened(vault))
    copy = opened(vault, 'copy.pdf')
    assert not copy['export_eligible']
    path = vault / 'one.pdf'
    path.rename(vault / 'renamed.pdf')
    renamed = review.open_review(vault, 'renamed.pdf', review.identity(vault / 'renamed.pdf'), proposal())
    if review.fingerprint(vault / 'renamed.pdf')['birth'] is not None:
        assert renamed['id'] == first['id']
        assert renamed['export_eligible']
    (vault / 'renamed.pdf').write_bytes(b'different pdf')
    changed = review.open_review(vault, 'renamed.pdf', review.identity(vault / 'renamed.pdf'), proposal())
    assert changed['status'] == 'unreviewed'
    assert not changed['export_eligible']


def test_stale_revision_concurrent_decisions_and_undo(vault):
    record = opened(vault)
    def attempt():
        try:
            return confirm(vault, record)
        except review.ReviewConflict:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(r is not None for r in results) == 1
    winner = next(r for r in results if r)
    undone = review.update(vault, winner['rel'], winner['token'], review.identity(vault / winner['rel']), action='undo')
    assert undone['status'] == 'unreviewed'
    assert not undone['export_eligible']


def test_same_bytes_replacement_does_not_inherit_confirmation(vault):
    record = confirm(vault, opened(vault))
    path = vault / record['rel']
    replacement = vault / 'replacement.pdf'
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    with pytest.raises(review.ReviewConflict):
        review.validate(vault, record['rel'], record['token'])
    reopened = review.open_review(vault, record['rel'], review.identity(path), proposal())
    assert reopened['status'] == 'unreviewed'


def test_reject_only_candidate_and_keep_confirmation_on_new_evidence(vault):
    record = opened(vault)
    record = confirm(vault, record)
    updated = review.update(vault, record['rel'], record['token'], review.identity(vault / record['rel']),
                            action='proposal', proposal=proposal('10.1234/two'), search={'title':'corrected'})
    assert updated['chosen']['doi'] == '10.1234/one'
    assert updated['conflicting_evidence']
    rejected = review.update(vault, record['rel'], updated['token'], review.identity(vault / record['rel']),
                             action='reject', candidate_id=review.digest(updated['proposal']['candidates'][0]))
    assert rejected['chosen']['doi'] == '10.1234/one'
    assert rejected['status'] == 'confirmed'
    reopened = review.open_review(vault, record['rel'], review.identity(vault / record['rel']), proposal())
    assert reopened['proposal'] == updated['proposal']


def test_read_does_not_initialize_and_errors_do_not_become_unreviewed(vault):
    path = review.store_path()
    assert review.snapshot(vault) == {}
    assert not path.exists()
    path.write_bytes(b'broken database')
    with pytest.raises(Exception, match='database'):
        review.snapshot(vault)


def test_export_checks_content_and_vault_scope(vault):
    record = confirm(vault, opened(vault))
    other = vault.parent / 'other'
    other.mkdir()
    (other / record['rel']).write_bytes((vault / record['rel']).read_bytes())
    assert review.snapshot(other) == {}
    review.validate(vault, record['rel'], record['token'])
    (vault / record['rel']).write_bytes(b'changed')
    with pytest.raises(review.ReviewConflict, match='content changed'):
        review.validate(vault, record['rel'], record['token'])


def test_file_mutation_during_identity_is_refused(vault, monkeypatch):
    opened(vault)
    original = review.fingerprint
    calls = 0
    def changing(path):
        nonlocal calls
        result = original(path)
        calls += 1
        if calls > 1:
            result['mtime'] += 1
        return result
    monkeypatch.setattr(review, 'fingerprint', changing)
    with pytest.raises(review.ReviewConflict):
        review.identity(vault / 'one.pdf')
