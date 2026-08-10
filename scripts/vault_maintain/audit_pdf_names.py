#!/usr/bin/env python3
"""Audit biblio_articles PDFs for naming convention compliance.

Expected naming convention: lastname_YYYY.pdf or lastname_i_YYYY.pdf
(name segments may contain digits, e.g. who_rhr1806_2019.pdf).

Whitelisted: z_item*.pdf — the ECN/EDN curriculum items follow their own
deliberate naming scheme and are not audited. The prefix comes from ChatEKLD's
`audit_biblio_skip_prefix` when available, so the app and this audit exempt the
same files.

Paths are resolved by `vault_paths` (CLI arg → env → ChatEKLD config → marker
discovery → built-in default) rather than hardcoded, so renaming or moving the
vault, `Z_attachments/`, or `biblio_articles/` does not silently break the run.

Usage:
    audit_pdf_names.py                     # resolve everything automatically
    audit_pdf_names.py --biblio <path>     # audit a specific directory
    audit_pdf_names.py --quiet             # only report problems

Exit codes: 0 = compliant, 1 = non-compliant files found, 2 = setup error.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_paths import (  # noqa: E402
    VaultPathError,
    chatekld_config,
    resolve_biblio,
    resolve_vault,
    skip_prefixes,
)

# Matches:
# lastname_2026.pdf
# al_diwani_1_2026.pdf
# hyphenated-name_2026.pdf
# who_rhr1806_2019.pdf (digits allowed inside name segments)
# Case-SENSITIVE (2026-07-23 audit): the convention is lowercase, and the old
# re.IGNORECASE waved through Smith_2020.PDF — macOS's case-insensitive
# filesystem opens it fine, which is exactly why only this audit can flag it.
# (The `(?:_\d+)?` disambiguator group is subsumed by the segment group but
# kept for readability against the examples above.)
PATTERN = re.compile(r"^[a-z][a-z0-9\-]*(?:_[a-z0-9\-]+)*(?:_\d+)?_\d{4}\.pdf$")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--vault", help="vault root (default: auto-resolved)")
    parser.add_argument(
        "--biblio", help="biblio PDF directory (default: auto-resolved under the vault)"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the resolved-path banner"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    cfg = chatekld_config()
    try:
        vault = resolve_vault(args.vault, cfg)
        biblio = resolve_biblio(vault.path, args.biblio, cfg)
    except VaultPathError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    whitelist = skip_prefixes(cfg)

    if not args.quiet:
        print(f"Vault:  {vault.describe()}")
        print(f"Biblio: {biblio.describe()}")
    print(f"Auditing PDF naming convention in {biblio.path}...")

    non_compliant = []
    total = 0
    whitelisted = 0
    seen = set()

    for file_path in biblio.path.rglob("*"):
        if file_path.suffix.lower() != ".pdf" or file_path in seen:
            continue
        seen.add(file_path)
        total += 1
        if file_path.name.lower().startswith(whitelist):
            whitelisted += 1
            continue
        if not PATTERN.match(file_path.name):
            non_compliant.append(file_path.name)

    suffix = f" ({whitelisted} whitelisted: {', '.join(whitelist)}*)" if whitelisted else ""
    print(f"\nChecked {total} PDFs{suffix}.")

    if total == 0:
        # An empty sweep used to read as success. It almost always means the
        # resolved directory is the wrong one, so say so and fail.
        print(
            f"\n[ERROR] No PDFs found in {biblio.path} — the directory resolved but "
            f"is empty.\n  Resolution rule: {biblio.source}\n"
            f"  If the folder moved, pass --biblio <path> or set BIBLIO_DIR.",
            file=sys.stderr,
        )
        return 2

    if non_compliant:
        print(
            f"\n[WARNING] Found {len(non_compliant)} PDFs that do not match the "
            f"expected naming convention:",
            file=sys.stderr,
        )
        for name in sorted(non_compliant):
            print(f"  - {name}", file=sys.stderr)
        print(
            "\nPlease review and rename these files manually. We do not auto-rename "
            "to avoid breaking Zotero's attachment links if they were synced there.",
            file=sys.stderr,
        )
        return 1

    print("\nAll PDFs comply with the naming convention! 🎉")
    return 0


if __name__ == "__main__":
    sys.exit(main())
