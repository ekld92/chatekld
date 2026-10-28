import pytest
import importlib.util

if importlib.util.find_spec('prototypes') is None:
    pytest.skip('Private prototype fixtures are absent from public source artifacts', allow_module_level=True)

from prototypes.zotero_desktop.metadata import preview, draft


def snapshot(items=()):
    return dict(server_id='fixture', library='users/0', library_version=3, total=len(items),
                items=list(items), item_type='journalArticle', valid_fields=['title', 'DOI'])


def make(current=None, items=None):
    return preview(current, {'title':'Proposed', 'DOI':'10.1234/new'},
                   {'title':['fixture source'], 'DOI':['printed DOI']},
                   snapshot=snapshot(items if items is not None else [current] if current else []))


def test_update_chooses_fields_and_preserves_manual_edits():
    current = dict(key='PARENT23', version=3, itemType='journalArticle', title='Manual',
                   extra='Citation Key: my_key', tags=[{'tag':'manual'}], collections=['COLL2345'])
    review = make(current)
    assert all(f['choice']=='current' for f in review['fields'])
    result = draft(review, {'title':'current','DOI':'proposed'}, fresh=current,
                   snapshot=snapshot([current]))
    assert result['payload']=={'DOI':'10.1234/new'} and not result['executable']
    with pytest.raises(ValueError, match='changed'):
        draft(review, {}, fresh={**current,'title':'Later edit'}, snapshot=snapshot([current]))


def test_creation_blocks_duplicate_and_requires_every_field_choice():
    items=[{'key':'EXIST234','DOI':'10.1234/new'}]
    review=make(items=items)
    with pytest.raises(ValueError, match='duplicate'):
        draft(review, {'title':'proposed','DOI':'proposed'}, fresh=None,
              snapshot=snapshot(items), item_type='journalArticle')
    with pytest.raises(ValueError, match='every'):
        draft(make(), {'title':'proposed'}, fresh=None, snapshot=snapshot())


def test_protected_fields_and_unattributed_proposals_are_refused():
    for field in ['extra','citationKey','collections','relations','tags','parentItem']:
        with pytest.raises(ValueError, match='protected'):
            preview({}, {field:'replacement'}, {field:['source']},
                    snapshot={**snapshot(), 'valid_fields':[field]})


def test_unselected_duplicate_doi_does_not_block_unique_selected_title():
    items = [{'key':'EXIST234', 'itemType':'journalArticle', 'DOI':'10.1234/new', 'title':'Different'}]
    result = draft(make(items=items), {'title':'proposed', 'DOI':'current'}, fresh=None,
                   snapshot=snapshot(items), item_type='journalArticle')
    assert not result['duplicates']
    assert result['payload'] == {'title':'Proposed', 'itemType':'journalArticle'}


@pytest.mark.parametrize('change', [{'total':1}, {'library_version':None}, {'server_id':''},
                                  {'valid_fields':[]}, {'item_type':'attachment'}])
def test_incomplete_or_unbound_evidence_is_refused(change):
    with pytest.raises(ValueError, match='complete'):
        preview(None, {'title':'X'}, {'title':['fixture']}, snapshot={**snapshot(), **change})


def test_creation_cannot_change_reviewed_item_type():
    with pytest.raises(ValueError, match='item type'):
        draft(make(), {'title':'proposed', 'DOI':'current'}, fresh=None,
              snapshot=snapshot(), item_type='book')
