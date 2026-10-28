#!/usr/bin/env python3
"""Explicit, cache-only Markdown backfill for reviewed PDFs (no app imports)."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.cache_exports import _directory, _read_regular  # noqa: E402
from core.readable_pdf_export import export_pdf_text  # noqa: E402

_RANGE = re.compile(r"(?P<digest>[0-9a-f]{64})-p(?P<start>\d{5})-(?P<end>\d{5})\.txt")


def backfill(selection: dict) -> list[str]:
    vault = Path(selection["vault_root"]).expanduser().resolve(strict=True)
    cache = Path(selection["cache_dir"]).expanduser().absolute()
    destination = Path(selection["destination"]).expanduser().absolute()
    resolved = destination.resolve()
    if (resolved == vault or resolved.is_relative_to(vault)
            or resolved == cache or resolved.is_relative_to(cache)
            or cache.is_relative_to(resolved)):
        raise ValueError("Destination must be outside the vault and cache")
    exported = []
    with _directory(cache) as cache_fd:
        for entry in selection["entries"]:
            source = vault / entry["source_pdf"]
            if (source.suffix.lower() != ".pdf" or source.is_symlink()
                    or not source.resolve(strict=True).is_relative_to(vault)
                    or not source.is_file()):
                raise ValueError("PDF symlink or path escape refused")
            before = source.stat()
            with source.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            stat = source.stat()
            if (before.st_size, before.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError(f"PDF changed while hashing: {source}")
            if digest != entry["sha256"]:
                raise ValueError(f"PDF hash mismatch: {source}")
            signature = {"sha256": digest, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            names = entry.get("cache_files", [digest + ".txt"])
            keys = entry.get("citation_keys", [])
            key = keys[0] if (entry.get("verified_sha256") == digest
                              and len(keys) == 1 and isinstance(keys[0], str)
                              and keys[0].strip()) else None
            for name in names:
                match = _RANGE.fullmatch(name)
                if name != digest + ".txt" and (not match or match["digest"] != digest):
                    raise ValueError(f"Invalid cache filename: {name}")
                bounds = None
                if match:
                    bounds = (int(match["start"]), int(match["end"]))
                    if bounds[0] >= bounds[1]:
                        raise ValueError(f"Invalid page range: {name}")
                raw = _read_regular(cache_fd, name)
                if not raw:
                    raise ValueError(f"Missing or empty cache: {name}")
                text = raw.decode("utf-8")
                result = export_pdf_text(source, vault, destination, signature, text,
                                         page_range=bounds, citation_key=key)
                exported.append(str(result))
    return exported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection", type=Path, help="Reviewed JSON selection")
    args = parser.parse_args()
    try:
        print(json.dumps(backfill(json.loads(args.selection.read_text(encoding="utf-8")))))
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        parser.exit(2, f"Export refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
