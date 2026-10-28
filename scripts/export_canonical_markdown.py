"""Export an explicit canonical PDF inventory, without scanning or extracting.

Inventory JSON contains ``entries`` accepted by export_markdown_batch, with inline
canonical payloads and expected recipes. Supply EVERY ingestion root. Use a new
output directory for changed inventories; existing differing files are refused.
This command does not establish that the inventory covers a whole live index.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.practical_payload import export_markdown_batch  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--input-root', type=Path, action='append', required=True)
    args = parser.parse_args()
    manifest = export_markdown_batch(json.loads(args.inventory.read_text())['entries'],
                                     args.destination, input_roots=args.input_root)
    print(json.dumps({'inventory_count': manifest['inventory_count'],
                      'coverage': manifest['coverage']}, sort_keys=True))


if __name__ == '__main__':
    main()
