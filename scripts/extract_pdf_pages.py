#!/usr/bin/env python3
"""Explicit bounded PDF extraction and immutable exports; never indexes or activates.

Writes only the specified external storage/export directories. Online mode
sends eligible scanned-page images to the selected external provider. This is
NOT a read-only diagnostic or a dry run.
"""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.page_ocr import TesseractOCR, extract_pages, export_pages  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--input-root', type=Path, required=True)
    parser.add_argument('--storage', type=Path, required=True)
    parser.add_argument('--exports', type=Path, required=True)
    parser.add_argument('--tessdata', type=Path)
    parser.add_argument('--tesseract', type=Path, default=Path('/opt/homebrew/bin/tesseract'))
    parser.add_argument('--provider', choices=('local', 'openai', 'google', 'deepseek', 'openrouter'),
                        default='local')
    parser.add_argument('--model', help='Explicit image-input model for online OCR')
    parser.add_argument('--first-page', type=int, default=1, help='Physical page, inclusive')
    parser.add_argument('--last-page', type=int, help='Physical page, inclusive')
    parser.add_argument('--retry-page', type=int, action='append', default=[],
                        help='Explicit failed/interrupted page retry; total allowance is two')
    parser.add_argument('--reextract', action='store_true',
                        help='Explicitly send PDF pages even when a text cache already exists')
    parser.add_argument('--budget-seconds', type=float, default=1200)
    args = parser.parse_args()
    if args.provider == 'local':
        if args.tessdata is None:
            parser.error('--tessdata is required for local OCR')
        engine = TesseractOCR(args.tesseract, args.tessdata)
    else:
        if not args.model:
            parser.error('--model is required for online OCR')
        from core.online_ocr import OnlineOCR
        engine = OnlineOCR(args.provider, args.model)
        if not args.reextract:
            import hashlib
            from core.constants import OBSIDIAN_CACHE_DIR, OBSIDIAN_INDEX_DIR
            from rag.storage_io import vault_key
            digest = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
            for root in (OBSIDIAN_CACHE_DIR, OBSIDIAN_INDEX_DIR):
                cache_dir = Path(root) / 'pdf_cache' / vault_key(args.input_root.resolve())
                if (cache_dir.is_dir() and not cache_dir.is_symlink() and any(
                        path.is_file() and not path.is_symlink() and path.stat().st_size
                        for path in cache_dir.glob(digest + '*.txt'))):
                    parser.error('Existing PDF text cache retained; pass --reextract explicitly')
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        result = extract_pages(args.pdf, args.input_root, args.storage, engine,
                               start_page=args.first_page - 1, end_page=args.last_page,
                               retry_pages=frozenset(args.retry_page),
                               cancelled=stop.is_set, budget_s=args.budget_seconds)
        output = export_pages(result, args.exports, args.input_root)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'Extraction refused: {exc}\n')
    print(json.dumps({'coverage': result['coverage'], 'markdown': str(output),
                      'stop_reason': result['stop_reason'],
                      'pages': [{k: r[k] for k in ('binding', 'status', 'attempt')} for r in result['pages']]}))
    return 1 if result['coverage'] == 'partial' else 0


if __name__ == '__main__':
    raise SystemExit(main())
