#!/usr/bin/env python3
"""Explicit cache-only export; reads a reviewed selection JSON, never app state."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.cache_exports import export_selected  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('selection', type=Path, help='Explicit selection JSON (see CLI_GUIDE.md)')
    parser.add_argument('--destination', type=Path, help='External immutable snapshot directory')
    args = parser.parse_args()
    try:
        result = export_selected(json.loads(args.selection.read_text(encoding='utf-8')), args.destination)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'Export refused: {exc}\n')
    print(json.dumps(result, sort_keys=True))
    return 1 if result['missing_cache'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
