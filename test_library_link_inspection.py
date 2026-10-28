"""Current dependent-link checks use only temporary files and known scan paths."""
from types import SimpleNamespace

from accession import dependent_links as links


def inspect(tmp_path, text, files=('papers/a.pdf',)):
    note = tmp_path / 'notes' / 'n.md'
    note.parent.mkdir(exist_ok=True)
    note.write_text(text)
    for file in files:
        path = tmp_path / file
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'%PDF-fixture')
    inventory = SimpleNamespace(records={'key': SimpleNamespace(obsidian_note=SimpleNamespace(path=note))})
    documents = [dict(kind='file', row={'pdf': {'rel': file}}) for file in files]
    return links.inspect(SimpleNamespace(vault_root=tmp_path), inventory, documents)


def test_current_paths_encoded_fragments_and_versions(tmp_path):
    report = inspect(tmp_path, '[[papers/a.pdf#page=2|read]] [file](../papers/a.pdf)\n[[a.pdf]]\n[[a_1.pdf]]')
    states = {link['target']: link['status'] for link in report['notes'][0]['links']}
    assert states['papers/a.pdf#page=2'] == 'resolved_path'
    assert states['../papers/a.pdf'] == 'resolved_path'
    assert states['a.pdf'] == 'resolved_candidate'
    assert states['a_1.pdf'] == 'unresolved'
    assert not report['writes'] and not report['repair_available']


def test_ambiguity_external_traversal_and_nonexistent_path(tmp_path):
    report = inspect(tmp_path, '[[a.pdf]] [outside](../../secret.pdf) [remote](https://example.org/a.pdf) [[papers/missing.pdf]]',
                     files=('papers/a.pdf', 'other/a.pdf'))
    states = [link['status'] for link in report['notes'][0]['links']]
    assert set(states) == {'ambiguous', 'outside_vault', 'external_or_unsupported', 'not_found_at_path'}


def test_note_symlinks_and_target_symlinks_are_unavailable(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside.md')
    outside.write_text('personal content')
    note = tmp_path / 'n.md'
    note.symlink_to(outside)
    inventory = SimpleNamespace(records={'key': SimpleNamespace(obsidian_note=SimpleNamespace(path=note))})
    report = links.inspect(SimpleNamespace(vault_root=tmp_path), inventory, [])
    assert report['notes'][0]['status'] == 'unavailable'
    assert not report['complete']
    target = tmp_path / 'target.pdf'
    target.symlink_to(outside)
    result = links._resolve(tmp_path, 'n.md', 'wikilink', 'target.pdf', {'target.pdf'})
    assert result['status'] == 'unavailable'
    assert outside.read_text() == 'personal content'


def test_changed_note_is_not_hidden_by_saved_links(tmp_path):
    note = tmp_path / 'n.md'
    note.write_text('[[new.pdf]]')
    inventory = SimpleNamespace(records={'key': SimpleNamespace(obsidian_note=SimpleNamespace(path=note, pdf_links={'old.pdf'}))})
    result = links.inspect(SimpleNamespace(vault_root=tmp_path), inventory, [])
    assert result['notes'][0]['links'][0]['target'] == 'new.pdf'
    assert result['notes'][0]['evidence']['sha256']
    assert note.read_text() == '[[new.pdf]]'


def test_bounds_and_orphan_notes(tmp_path, monkeypatch):
    note = tmp_path / 'orphan.md'
    note.write_text('too much')
    monkeypatch.setattr(links, 'MAX_NOTE_BYTES', 3)
    report = links.inspect(SimpleNamespace(vault_root=tmp_path), SimpleNamespace(records={}, notes_without_record=[note]), [])
    assert report['notes'][0]['status'] == 'unavailable'
    assert report['known_notes'] == 1


def test_code_is_not_a_dependency_and_reference_definitions_are_seen():
    found = links.extract('`[[code.pdf]]`\n```md\n[[also-code.pdf]]\n```\n[pdf]: <papers/a%20b.pdf>\n')
    assert found == [('reference_definition', 'papers/a%20b.pdf')]


def test_encoded_hash_in_filename_is_not_treated_as_fragment(tmp_path):
    report = inspect(tmp_path, '[pdf](../papers/a%23b.pdf#page=2)', files=('papers/a#b.pdf',))
    assert report['notes'][0]['links'][0]['status'] == 'resolved_path'


def test_replaced_note_during_read_is_unavailable(tmp_path, monkeypatch):
    note = tmp_path / 'n.md'
    note.write_text('[[old.pdf]]')
    original = links._open_inside
    calls = []
    def changed(root, relative):
        calls.append(relative)
        if len(calls) == 2:
            replacement = tmp_path / 'replacement.md'
            replacement.write_text('[[new.pdf]]')
            replacement.replace(note)
        return original(root, relative)
    monkeypatch.setattr(links, '_open_inside', changed)
    report = links.inspect(SimpleNamespace(vault_root=tmp_path), SimpleNamespace(records={}, notes_without_record=[note]), [])
    assert report['notes'][0]['status'] == 'unavailable'


def test_replaced_pdf_target_is_unavailable(tmp_path, monkeypatch):
    target = tmp_path / 'target.pdf'
    target.write_bytes(b'old PDF')
    original = links._open_inside
    calls = []
    def replaced(root, relative):
        calls.append(relative)
        if len(calls) == 2:
            new = tmp_path / 'new.pdf'
            new.write_bytes(b'new PDF')
            new.replace(target)
        return original(root, relative)
    monkeypatch.setattr(links, '_open_inside', replaced)
    result = links._resolve(tmp_path, 'note.md', 'wikilink', 'target.pdf', {'target.pdf'})
    assert result['status'] == 'unavailable'
    assert target.read_bytes() == b'new PDF'
