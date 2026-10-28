"""Apply explicitly approved current-association mappings from a fresh preview.

This is a real application-store write command. It never writes Zotero/PDF/bib
or original attempt/review records. Authorization and semantic identity approval
must be obtained separately before invoking --confirm and named record IDs.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-state', type=Path, required=True)
    parser.add_argument('--preview', type=Path, required=True)
    parser.add_argument('--approve-record', action='append', required=True)
    parser.add_argument('--confirm', action='store_true', required=True)
    args = parser.parse_args()
    preview = json.loads(args.preview.read_text())
    if not preview.get('decision_sources_unchanged') or not preview.get('migration_proposal'):
        raise ValueError('Preview lacks unchanged-source evidence and a valid proposal.')
    source = args.app_state.resolve(strict=True)
    if source != Path(preview['source']).resolve():
        raise ValueError('Application store differs from the preview.')
    os.environ['CHATEKLD_BASE_DIR'] = str(source)
    os.environ['CHERQAIN_BASE_DIR'] = str(source)
    from accession import migration
    from audit.config import load_settings
    result = migration.apply(load_settings(), preview['migration_proposal'],
                             approved_records=args.approve_record, confirm=args.confirm)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
