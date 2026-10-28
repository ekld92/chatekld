#!/usr/bin/env python3
"""Explicit download of pinned Tesseract EN/FR data; never edits app settings."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.cache_exports import _directory, _publish_new  # noqa: E402
from core.page_ocr import LANGUAGE_COMMIT, LANGUAGE_HASHES  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    receipts = []
    with _directory(args.destination.absolute(), create=True) as fd:
        for name, digest in LANGUAGE_HASHES.items():
            url = f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{LANGUAGE_COMMIT}/{name}'
            path = args.destination / name
            if path.is_symlink():
                raise ValueError('Symlink model refused')
            if path.exists():
                raw = path.read_bytes()
            else:
                with urllib.request.urlopen(url, timeout=60) as response:  # nosec B310
                    raw = response.read(20 * 1024**2 + 1)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError(f'Artifact hash mismatch: {name}')
            _publish_new(fd, name, raw)
            receipts.append({'name': name, 'url': url, 'sha256': digest, 'bytes': len(raw)})
        _publish_new(fd, 'receipt.json', json.dumps({'commit': LANGUAGE_COMMIT, 'artifacts': receipts},
                                                  indent=2, sort_keys=True).encode())
    print(json.dumps({'destination': str(args.destination), 'verified_artifacts': len(receipts)}))


if __name__ == '__main__':
    main()
