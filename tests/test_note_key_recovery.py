import pytest
from notesync.migrate import set_frontmatter_keys


@pytest.mark.parametrize('body', [
    'zotero-key: OTHER234\nzotero-key: PARENT23',
    'zotero-key:',
    'base: &base {zotero-key: PARENT23}\n<<: *base',
    '"zotero-key": OTHER234\nzotero-key: PARENT23',
])
def test_repair_refuses_ambiguous_manual_keys(body):
    text = '---\n' + body + '\n---\n\nKeep all prose\n'
    proposed, reason = set_frontmatter_keys(text, {'zotero-key': 'PARENT23'}, strict_keys=True)
    assert proposed is None
    assert reason == 'ambiguous_frontmatter_keys'
