"""Synthetic filesystem/SQLite fixture for real backend-to-renderer acceptance."""
import os
from pathlib import Path
import sqlite3


def install():
    base = Path(os.environ['CHATEKLD_BASE_DIR']).resolve(strict=True)
    if not base.name.startswith('papermind-browser-'):
        raise RuntimeError('Browser fixture requires its own disposable application directory.')
    import fitz
    from audit.config import load_settings
    from audit.manager import audit_manager
    from audit.engine.inventory import Inventory
    from audit.engine.bridge import BridgeResult
    vault = base / 'fixture-vault'
    pdfs = vault / 'biblio_articles'
    pdfs.mkdir(parents=True)
    (vault / '_master.bib').write_text('')
    for name in ('backend-one.pdf', 'backend-two.pdf'):
        with fitz.open() as document:
            for number in (1, 2):
                page = document.new_page()
                page.insert_text((72, 100), f'CHERQAiN synthetic {name} page {number}', fontsize=20)
            document.save(pdfs / name)
    database = base / 'synthetic-zotero.sqlite'
    with sqlite3.connect(database) as con:
        con.executescript('''
            CREATE TABLE settings(setting,key,value);
            INSERT INTO settings VALUES('localAPI','serverID','browser-synthetic-db');
            CREATE TABLE items(itemID,key,libraryID,itemTypeID);
            CREATE TABLE itemTypes(itemTypeID,typeName);
            CREATE TABLE deletedItems(itemID);
            CREATE TABLE itemData(itemID,fieldID,valueID);
            CREATE TABLE fields(fieldID,fieldName);
            CREATE TABLE itemDataValues(valueID,value);
            CREATE TABLE creators(creatorID,firstName,lastName,fieldMode);
            CREATE TABLE creatorTypes(creatorTypeID,creatorType);
            CREATE TABLE itemCreators(itemID,creatorID,creatorTypeID,orderIndex);
            CREATE TABLE tags(tagID,name);
            CREATE TABLE itemTags(itemID,tagID);
            CREATE TABLE itemAttachments(itemID,parentItemID,path,linkMode);
        ''')
    from core.config import save_config
    save_config({'obsidian_vault_path': str(vault), 'audit_zotero_sqlite':str(database),
                 'audit_zotero_storage':str(base/'storage'), 'audit_attachments_subdir':'.'})
    settings = load_settings()
    audit_manager._settings = settings
    audit_manager._inventory = Inventory(records={}, bridge=BridgeResult(unmapped_pdfs=sorted(pdfs.glob('*.pdf'))), pdfs_skipped=[], zotero_matched_by_title=0)

    # Seed synthetic evidence through the real durable cache, then exercise
    # actual open/decision/preview routes in Chromium (no response substitution).
    from accession import cache, review
    evidence = {}
    for path in sorted(pdfs.glob('*.pdf')):
        cache.store(evidence, review.identity(path)['sha256'], dict(candidates=[dict(
            title='Synthetic review article', doi='10.1234/browserfixture', source='fixture')]))
    cache.save_cache(evidence)
    with sqlite3.connect(database) as con:
        con.executescript("""
            INSERT INTO itemTypes VALUES(1,'journalArticle');
            INSERT INTO items VALUES(1,'PARENT01',1,1);
            INSERT INTO fields VALUES(1,'publicationTitle');
            INSERT INTO itemDataValues VALUES(1,'Journal of Fixtures');
            INSERT INTO itemData VALUES(1,1,1);
            INSERT INTO creators VALUES(1,'Ada','Test',0),(2,'','Fixture Society',1);
            INSERT INTO creatorTypes VALUES(1,'author');
            INSERT INTO itemCreators VALUES(1,1,1,0),(1,2,1,1);
        """)

    # Separate entry for physical-delete/restore acceptance. No native writes.
    import json
    from audit.core.bib import BibEntry
    from audit.core.zotero import ZoteroItem
    from audit.engine.inventory import Record
    from accession import reconciliation
    (vault/'_master.bib').write_text('@article{Managed,title={Managed PDF fixture}}')
    for name in ('managed-keep.pdf','managed-remove.pdf'):
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((72,100), 'Managed duplicate PDF fixture')
            document.save(pdfs/name)
    with sqlite3.connect(database) as con:
        con.executescript("""
            INSERT INTO itemTypes VALUES(2,'attachment');
            INSERT INTO items VALUES(2,'MGDPAR01',1,1),(3,'MGDATT01',1,2),(4,'MGDATT02',1,2);
            INSERT INTO fields VALUES(2,'citationKey'),(3,'title');
            INSERT INTO itemDataValues VALUES(2,'Managed'),(3,'Managed PDF fixture');
            INSERT INTO itemData VALUES(2,2,2),(2,3,3);
        """)
        con.execute('INSERT INTO itemAttachments VALUES(3,2,?,2)',(str(pdfs/'managed-keep.pdf'),))
        con.execute('INSERT INTO itemAttachments VALUES(4,2,?,2)',(str(pdfs/'managed-keep.pdf'),))
    settings.mapping_file.parent.mkdir(parents=True,exist_ok=True)
    settings.mapping_file.write_text(json.dumps({'matches':{'Managed':['biblio_articles/managed-keep.pdf','biblio_articles/managed-remove.pdf']}}))
    audit_manager._inventory.records['Managed'] = Record(citation_key='Managed',bib_entry=BibEntry('Managed','article',title='Managed PDF fixture'),
        zotero_item=ZoteroItem(item_key='MGDPAR01',item_type='journalArticle',title='Managed PDF fixture'),
        pdf_paths=[pdfs/'managed-keep.pdf',pdfs/'managed-remove.pdf'])
    reconciliation.refresh(settings)
