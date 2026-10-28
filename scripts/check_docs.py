#!/usr/bin/env python3
"""Check evergreen Markdown links and root agent-instruction parity.

Run on this checkout with no options. Pass --root PATH for a built public
artifact; the same public entrypoints must resolve without private docs/.
This deliberately checks maintained entrypoints, not dated evidence records.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


ENTRYPOINTS = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "PROJECT_REFERENCE.md",
    "KNOWN_ISSUES.md",
    "CLI_GUIDE.md",
    "LIBRARY_WORKFLOWS.md",
    "LIBRARY_CAMPAIGNS.md",
    "static/help/guide.md",
    "api/README.md",
    "core/README.md",
    "services/README.md",
    "tagging/README.md",
    "deckgen/README.md",
    "docgen/README.md",
)
LINK = re.compile(r"!?\[[^\]]+\]\(([^)]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
INLINE_CODE = re.compile(r"`+[^`]*`+")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
COMMAND = re.compile(r"(?:^\s*|`)(?:papermind|cherqain)\s+([a-z][a-z0-9-]*)\b")


def slug(label: str) -> str:
    """Approximate GitHub's heading anchors for maintained prose headings."""
    label = INLINE_CODE.sub(lambda m: m.group().strip("`"), label)
    label = re.sub(r"!?\[([^\]]+)\]\([^)]+\)", r"\1", label)
    label = re.sub(r"<[^>]+>", "", label).lower()
    label = re.sub(r"[^\w\s-]", "", label, flags=re.UNICODE)
    return re.sub(r"\s", "-", label)


def prose_lines(path: Path):
    source = HTML_COMMENT.sub("", path.read_text(encoding="utf-8"))
    fenced = False
    marker = ""
    for number, line in enumerate(source.splitlines(), 1):
        found = FENCE.match(line)
        if found:
            if not fenced:
                fenced, marker = True, found.group(1)[:3]
            elif found.group(1)[:3] == marker:
                fenced = False
            continue
        if not fenced:
            yield number, line


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    result: set[str] = set()
    for _, line in prose_lines(path):
        match = HEADING.match(line)
        if not match:
            continue
        basic = slug(match.group(1))
        count = seen.get(basic, 0)
        seen[basic] = count + 1
        result.add(basic if count == 0 else f"{basic}-{count}")
    return result


def cli_roots(path: Path) -> set[str]:
    """Read top-level Click command names without importing application code."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            fn = decorator.func
            if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                    and fn.value.id == "cli" and fn.attr in {"command", "group"}
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)):
                names.add(decorator.args[0].value)
    return names


def check(root: Path) -> list[str]:
    errors: list[str] = []
    commands = cli_roots(root / "cli/main.py")
    agent = root / "AGENTS.md"
    claude = root / "CLAUDE.md"
    if agent.is_file() and claude.is_file() and agent.read_bytes() != claude.read_bytes():
        errors.append("AGENTS.md and CLAUDE.md differ")
    for relative in ENTRYPOINTS:
        source = root / relative
        if not source.is_file():
            errors.append(f"missing entrypoint: {relative}")
            continue
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            for command in COMMAND.findall(line):
                if command not in commands:
                    errors.append(f"{relative}:{number}: unknown CLI group {command}")
        for number, line in prose_lines(source):
            line = INLINE_CODE.sub("", line)
            for match in LINK.finditer(line):
                raw = match.group(1).strip().strip("<>")
                url = urlsplit(raw)
                if url.scheme or url.netloc or raw.startswith("//"):
                    continue
                target = (source.parent / unquote(url.path)).resolve() if url.path else source
                if not target.is_file():
                    errors.append(f"{relative}:{number}: missing {raw}")
                elif url.fragment and unquote(url.fragment) not in anchors(target):
                    errors.append(f"{relative}:{number}: missing anchor {raw}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="checkout or built public artifact root")
    args = parser.parse_args()
    errors = check(args.root.resolve())
    for error in errors:
        print(error)
    if errors:
        print(f"Documentation check failed: {len(errors)} issue(s)")
        return 1
    print("Documentation entrypoint links and agent parity OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
