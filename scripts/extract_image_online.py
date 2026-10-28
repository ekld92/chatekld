#!/usr/bin/env python3
"""Explicit external image transcription. Sends the image to an API; never indexes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.online_ocr import OnlineOCR, extract_image  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--input-root', type=Path, required=True)
    parser.add_argument('--storage', type=Path, required=True)
    parser.add_argument('--provider', required=True,
                        choices=('openai', 'google', 'deepseek', 'openrouter'))
    parser.add_argument('--model', required=True)
    parser.add_argument('--retry', action='store_true', help='Retry a failed attempt once')
    parser.add_argument('--reextract', action='store_true',
                        help='Explicitly send an image even when usable cached text exists')
    args = parser.parse_args()
    try:
        result = extract_image(args.image, args.input_root, args.storage,
                               OnlineOCR(args.provider, args.model), retry=args.retry,
                               reextract=args.reextract)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'Extraction refused: {exc}\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('ocr', 'existing_text') else 1


if __name__ == '__main__':
    raise SystemExit(main())
