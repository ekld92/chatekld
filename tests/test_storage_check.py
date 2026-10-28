"""Phase 6 reduced (2026-09-16): the storage control is read-only and names the
manual correction for every kind of misplaced attachment."""
import sqlite3

from accession import reconciliation as rec, storage_check
from test_pdf_reconciliation import world as _world

world = _world


def _add_attachment(settings, item_id, key, parent_id, path, link_mode):
    with sqlite3.connect(settings.zotero_sqlite) as db:
        db.execute('INSERT INTO items VALUES (?,?,1,2)', (item_id, key))
        db.execute('INSERT INTO itemAttachments VALUES (?,?,?,?)', (item_id, parent_id, path, link_mode))


def test_control_classifies_every_attachment_and_writes_nothing(world, tmp_path):
    settings, pdf = world
    outside = tmp_path / 'elsewhere.pdf'
    outside.write_bytes(b'%PDF-elsewhere')
    _add_attachment(settings, 10, 'STOREDA1', 1, 'storage:one.pdf', 0)
    _add_attachment(settings, 11, 'OUTSIDE1', 1, str(outside), 2)
    _add_attachment(settings, 12, 'BROKEN01', 1, str(settings.biblio_articles_dir / 'gone.pdf'), 2)
    _add_attachment(settings, 13, 'RELATIV1', 3, 'attachments:sub/rel.pdf', 2)
    _add_attachment(settings, 14, 'DUPLICA1', 3, str(pdf), 2)
    _add_attachment(settings, 15, 'REMOTE01', 3, 'https://example.invalid/paper', 3)
    before = settings.zotero_sqlite.read_bytes()
    rec.refresh(settings)
    report = storage_check.check(settings, rec.saved(settings), citation_keys={(1, 'PARENT01'): 'KeyOne'})
    by_key = {r['attachment_key']: r for r in report['results']}
    assert by_key['STOREDA1']['finding'] == 'stored' and 'Attanger' in ' '.join(by_key['STOREDA1']['guidance'])
    assert by_key['OUTSIDE1']['finding'] == 'outside_biblio'
    assert by_key['BROKEN01']['finding'] == 'broken_path' and 're-point' in ' '.join(by_key['BROKEN01']['guidance'])
    assert by_key['RELATIV1']['finding'] == 'relative_link'
    assert by_key['REMOTE01']['finding'] == 'remote'
    # The fixture's own linked attachment and the duplicate share one file.
    assert by_key['ATTACH01']['finding'] == 'duplicate_path' and by_key['DUPLICA1']['finding'] == 'duplicate_path'
    assert by_key['ATTACH01']['duplicate_of'] == ['DUPLICA1'] and by_key['ATTACH01']['citation_key'] == 'KeyOne'
    assert report['population']['attachments'] == 7 and report['counts']['stored'] == 1
    assert {r['attachment_key'] for r in report['findings']} == {'STOREDA1', 'OUTSIDE1', 'BROKEN01', 'RELATIV1', 'ATTACH01', 'DUPLICA1'}
    assert report['writes'] is False and report['applicable'] is False and report['conversion_offered'] is False
    assert all(r['applicable'] is False for r in report['results'])
    assert settings.zotero_sqlite.read_bytes() == before
    assert outside.exists() and pdf.exists()


def test_linked_file_under_the_bibliography_folder_is_clean(world):
    settings, pdf = world
    rec.refresh(settings)
    report = storage_check.check(settings, rec.saved(settings))
    assert [r['finding'] for r in report['results']] == ['linked_ok']
    assert report['findings'] == [] and 'Zero PDFs' in report['goal']


def test_empty_observation_is_reported_as_unavailable_not_clean(world):
    settings, _ = world
    report = storage_check.check(settings, None)
    assert report['results'] == [] and report['observation']['available'] is False


def _snapshot(*attachments):
    items = {"P": {"key": "P", "library": 1, "item_type": "journalArticle", "deleted": False, "fields": {"title": "Parent"}},
             "N": {"key": "N", "library": 1, "item_type": "note", "deleted": False, "fields": {}}}
    for a in attachments:
        items[a["key"]] = {"library": 1, "item_type": "attachment", "deleted": False, "fields": {}, **a}
    return {"database": "db", "checked_at": 1, "items": items}


def test_note_embedded_images_are_excluded_and_disclosed_not_findings(tmp_path):
    """2026-09-17 review, C1 (decision D3): 12 of the 14 live `stored` findings
    were images embedded in Zotero notes. A note image is not a PDF."""
    from types import SimpleNamespace
    settings = SimpleNamespace(biblio_articles_dir=tmp_path)
    report = storage_check.check(settings, _snapshot(
        {"key": "IMG1", "parent": "N", "path": "storage:image.png", "link_mode": "embedded_image"},
        {"key": "IMG2", "parent": "N", "path": "storage:x.png", "link_mode": "imported_file"},
    ))
    assert report["findings"] == []
    assert report["excluded"] == {"embedded_image": 1, "note_child": 1}
    assert all(r["attachment_key"] not in ("IMG1", "IMG2") for r in report["results"])


def test_an_attachment_under_a_trashed_parent_is_in_the_trash_too(tmp_path):
    """2026-09-18 (annual cleanup, Phase 3): a record merge trashed the
    retired DSM-5-TR record; Zotero flags only the PARENT deleted, so its
    linked copy of apa_2022.pdf stayed a `duplicate_path` finding here while
    the audit's own attachment report (rightly) no longer listed it."""
    from types import SimpleNamespace
    settings = SimpleNamespace(biblio_articles_dir=tmp_path)
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    snap = _snapshot({"key": "LIVE", "parent": "P", "path": str(tmp_path / "a.pdf"),
                      "link_mode": "linked_file"},
                     {"key": "GONE", "parent": "T", "path": str(tmp_path / "a.pdf"),
                      "link_mode": "linked_file"})
    snap["items"]["T"] = {"key": "T", "library": 1, "item_type": "book", "deleted": True,
                          "fields": {"title": "Retired"}}
    report = storage_check.check(settings, snap)
    assert [r["attachment_key"] for r in report["results"]] == ["LIVE"]
    assert report["findings"] == []


def test_an_html_imported_url_is_excluded_a_pdf_one_is_stored(tmp_path):
    from types import SimpleNamespace
    settings = SimpleNamespace(biblio_articles_dir=tmp_path)
    report = storage_check.check(settings, _snapshot(
        {"key": "HTML1", "parent": "P", "path": "https://example.invalid/x",
         "link_mode": "imported_url", "content_type": "text/html"},
        {"key": "PDFU1", "parent": "P", "path": "https://example.invalid/y.pdf",
         "link_mode": "imported_url", "content_type": "application/pdf"},
        {"key": "UNKU1", "parent": "P", "path": "https://example.invalid/z",
         "link_mode": "imported_url"},
        # Not a web page and not a PDF: real bytes in Zotero's storage, which
        # is the whole point of the `stored` finding. The first cut excluded
        # every non-PDF type and hid these (2026-09-20).
        {"key": "EPUB1", "parent": "P", "path": "https://example.invalid/b.epub",
         "link_mode": "imported_url", "content_type": "application/epub+zip"},
        {"key": "DOCX1", "parent": "P", "path": "https://example.invalid/c.docx",
         "link_mode": "imported_url",
         "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        {"key": "IMG1", "parent": "P", "path": "https://example.invalid/d.png",
         "link_mode": "imported_url", "content_type": "image/png"},
        {"key": "XHTML1", "parent": "P", "path": "https://example.invalid/e",
         "link_mode": "imported_url", "content_type": "application/xhtml+xml"},
    ))
    by = {r["attachment_key"]: r["finding"] for r in report["results"]}
    assert "HTML1" not in by
    assert "XHTML1" not in by
    assert report["excluded"]["web_snapshot"] == 2
    assert by["PDFU1"] == "stored"
    assert by["UNKU1"] == "stored"
    assert by["EPUB1"] == "stored"
    assert by["DOCX1"] == "stored"
    assert by["IMG1"] == "stored"


def test_observe_copies_content_type_when_the_column_exists(world):
    settings, pdf = world
    with sqlite3.connect(settings.zotero_sqlite) as db:
        db.execute("ALTER TABLE itemAttachments ADD COLUMN contentType")
        db.execute("UPDATE itemAttachments SET contentType=?", ("application/pdf",))
        db.execute("INSERT INTO items VALUES (20,?,1,2)", ("HTMLSNAP",))
        db.execute("INSERT INTO itemAttachments VALUES (20,1,?,?,?)",
                   ("https://example.invalid/page", 1, "text/html"))
    rec.refresh(settings)
    snap = rec.saved(settings)
    by = {i["key"]: i for i in snap["items"].values()}
    assert by["ATTACH01"].get("content_type") == "application/pdf"
    assert by["HTMLSNAP"].get("content_type") == "text/html"
    report = storage_check.check(settings, snap)
    findings = {r["attachment_key"]: r["finding"] for r in report["results"]}
    assert "HTMLSNAP" not in findings
    assert report["excluded"].get("web_snapshot") == 1
    assert findings.get("ATTACH01") == "linked_ok"
