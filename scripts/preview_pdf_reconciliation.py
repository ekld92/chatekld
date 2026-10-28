"""Read-only migration preview. No migration-apply option exists.

Example: python scripts/preview_pdf_reconciliation.py --app-state PATH --output PATH
All application SQLite stores are copied through the strict snapshot reader
before interpretation. Zotero's live-backup fallback is forbidden here.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--validate-disposable-apply', action='store_true',
                        help='Exercise selected eligible mappings only on disposable copies of app stores.')
    args = parser.parse_args()
    source = args.app_state.resolve(strict=True)
    cfg = json.loads((source/'config.json').read_text())
    protected = [source / rel for rel in ('config.json','audit/mapping.json','audit/pdf_reviews.sqlite3','audit/reconciliation.sqlite3','zotcreate/create_attempts.sqlite3','audit/accession_cache.json')]
    protected += [p for folder in ('audit', 'zotcreate') for p in (source/folder).rglob('*') if p.is_file()]
    protected = sorted(set(protected))
    protected += [Path(str(p) + suffix) for p in list(protected) if p.suffix == '.sqlite3' for suffix in ('-wal','-shm','-journal')]
    def signatures():
        from hashlib import sha256
        current_paths = set(protected) | {p for folder in ('audit', 'zotcreate') for p in (source/folder).rglob('*') if p.is_file()}
        return {str(path):dict(size=path.stat().st_size,mtime_ns=path.stat().st_mtime_ns,
                              sha256=sha256(path.read_bytes()).hexdigest()) if path.exists() else None for path in sorted(current_paths)}
    before = signatures()
    # Import runtime constants only after isolating every possible output.
    with tempfile.TemporaryDirectory(prefix='papermind-migration-preview-') as tmp:
        os.environ['CHATEKLD_BASE_DIR'] = tmp
        os.environ['CHERQAIN_BASE_DIR'] = tmp
        from accession import reconciliation as rec, review, cache, migration
        from audit.config import Settings
        from audit.core import zotero
        from zotcreate import attempts
        vault = Path(cfg['obsidian_vault_path']).expanduser().resolve(strict=True)
        output = args.output.resolve()
        if output.is_relative_to(source) or output.is_relative_to(vault):
            raise ValueError('Preview output must be outside real application state and vault.')
        settings = Settings(vault_root=vault,
            attachments_subdir=cfg.get('audit_attachments_subdir','Z_attachments'),
            biblio_articles_subdir=cfg.get('audit_biblio_articles_subdir','biblio_articles'),
            master_bib_path=cfg.get('audit_master_bib_path','_master.bib'),
            zotero_sqlite=Path(cfg.get('audit_zotero_sqlite','~/Zotero/zotero.sqlite')).expanduser(),
            zotero_storage=Path(cfg.get('audit_zotero_storage','~/Zotero/storage')).expanduser(),
            mapping_file_override=source/'audit/mapping.json')
        if output.is_relative_to(settings.zotero_sqlite.resolve().parent) or output.is_relative_to(settings.zotero_storage.resolve()):
            raise ValueError('Preview output must be outside the Zotero data directory.')
        for rel in ('audit/pdf_reviews.sqlite3','zotcreate/create_attempts.sqlite3','audit/reconciliation.sqlite3'):
            original = source/rel
            if not original.exists():
                continue
            destination = Path(tmp)/rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zotero._open_snapshot(original, allow_live_fallback=False) as memory:
                with sqlite3.connect(destination) as copy:
                    memory.backup(copy)
        mapping = json.loads(settings.mapping_file.read_text())
        reviews = review.snapshot(vault)
        retained = attempts.list_attempts(vault)
        paths = set(mapping.get('pending', [])) | set(reviews)
        paths.update(a['retained']['pdf_rel'] for a in retained if a['retained'].get('pdf_rel'))
        automatic_path = source/'audit/accession_cache.json'
        automatic = json.loads(automatic_path.read_text()) if automatic_path.exists() else {}
        differences = [rel for rel,r in reviews.items() if r.get('search') and r.get('proposal') != cache.lookup(automatic,r['sha256'])]
        try:
            observed = rec.retain_history(rec.saved(settings), rec.observe(settings, allow_live_fallback=False))
            rows = rec.rows(settings, observed, only=paths)
            error = None
        except (OSError, ValueError, sqlite3.Error) as exc:
            observed, rows, error = None, [], str(exc)
        plan = migration.proposal(settings, rows) if observed else None
        fixture_validation = None
        if args.validate_disposable_apply and plan:
            from dataclasses import replace
            copied_mapping = Path(tmp)/'audit/mapping.json'
            copied_mapping.parent.mkdir(parents=True, exist_ok=True)
            copied_mapping.write_bytes(settings.mapping_file.read_bytes())
            isolated_settings = replace(settings, mapping_file_override=copied_mapping)
            selected = [r['id'] for r in plan['records'] if r['eligible']]
            if selected:
                first = migration.apply(isolated_settings, plan, approved_records=selected, confirm=True)
                second = migration.apply(isolated_settings, plan, approved_records=selected, confirm=True)
                fixture_validation = dict(disposable_only=True, first=first, repeated=second,
                    note='Synthetic approvals on disposable application-store copies; not approval of real bibliographic identity.')
        after = signatures()
        payload = dict(source_manifest_before=before, source_manifest_after=after, decision_sources_unchanged=before == after, preview_only=True, generated_at=time.time(), source=str(source),
            zotero_available=observed is not None, observation_error=error,
            database=(observed or {}).get('database'), checked_at=(observed or {}).get('checked_at'),
            pending_paths=len(mapping.get('pending', [])), attempts=retained,
            review_search_differences=differences, counts=dict(Counter(r['state'] for r in rows)), rows=rows, migration_proposal=plan if before == after else None, disposable_apply_validation=fixture_validation,
            limitations=['No real migration applied.', 'Legacy database-unbound acknowledgements are retained, not treated as verified current identities.',
                         'Possible merges require review; no merge inference from title.', 'Relative attachment-base paths are not guessed.'])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({k:payload[k] for k in ('preview_only','pending_paths','zotero_available','observation_error','counts')},indent=2))
        print(f'{len(retained)} attempts; {len(differences)} corrected-search differences; preview: {output}')


if __name__ == '__main__':
    main()
