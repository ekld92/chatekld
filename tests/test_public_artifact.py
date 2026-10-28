from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from scripts.build_public_artifact import (
    MANIFEST_NAME,
    _cleanup_verification_artifacts,
    _assert_manifest_tree,
    _write_manifest,
    build_artifact,
    is_private_path,
    strip_private_claude_line,
    verify_artifact,
    _publish_noreplace,
)


def test_private_path_policy_covers_every_documented_strip():
    for path in (
        ".github/workflows/ci.yml",
        ".github/workflows/future.yml",
        ".github/dependabot.yml",
        "screenshots/obsidian-agent.png",
        "screenshots/library-audit.png",
        "screenshots/deck-generator.png",
        "screenshots/single-paper.png",
        "screenshots/help-guide.png",
        "docs/new-secret-plan.md",
        "screenshots/field-test-2026-10-28/README.md",
        "maintenance/steps/private.py",
        "cleanup/runner.py",
        "campaigns/example/thesis_snapshot.md",
        "prototypes/zotero_desktop/README.md",
        ".codex/config.toml",
        ".claude/settings.json",
        "scripts/vault_maintain/revived.py",
        "scripts/batch_tagging/revived.py",
        "api/routes/maintenance.py",
        "core/candidate_docling.py",
        "core/candidate_extraction.py",
        "core/candidate_evidence.py",
        "core/candidate_reading_order.py",
        "core/extraction_profiles.py",
        "scripts/practical_retrieval.py",
        "scripts/eval_reviewed_development.py",
        "scripts/run_candidate_docling_page.py",
        "tests/test_candidate_docling.py",
        "tests/test_candidate_extraction.py",
        "tests/test_candidate_evidence.py",
        "tests/test_candidate_reading_order.py",
        "tests/test_extraction_profiles.py",
        "tests/test_practical_retrieval.py",
        "tests/test_integrity_cleanup_adversarial.py",
        "static/js/maintenance.js",
        "tests/js/maintenance.test.js",
        "tests/js/maintenanceStatusLine.test.js",
        "tests/js/zoteroNativeReviewSurface.test.js",
    ):
        assert is_private_path(path), path
    assert not is_private_path("api/routes/status.py")
    assert not is_private_path("static/js/app.js")
    assert not is_private_path("screenshots/blog-2026-10-28/ask-local-redacted.png")


def test_exclusive_publication_preserves_a_late_empty_destination(tmp_path):
    source = tmp_path / 'staging'
    destination = tmp_path / 'release'
    source.mkdir()
    (source / 'source.txt').write_text('ours')
    destination.mkdir()
    inode = destination.stat().st_ino
    with pytest.raises(FileExistsError):
        _publish_noreplace(source, destination)
    assert destination.stat().st_ino == inode
    assert list(destination.iterdir()) == []
    assert (source / 'source.txt').read_text() == 'ours'


def test_private_claude_strip_is_exact_and_fail_closed():
    source = "before\n**Not verified — the review private\nafter\n"
    assert strip_private_claude_line(source) == "before\nafter\n"
    assert strip_private_claude_line("no marker\n") == "no marker\n"
    with pytest.raises(ValueError):
        strip_private_claude_line(source + "**Not verified — the review second\n")


def test_build_uses_staging_no_replace_and_writes_reproducible_manifest(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "CLAUDE.md").write_text(
        "public\n**Not verified — the review private\n", encoding="utf-8"
    )
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "requirements-lock-linux.txt").write_text(
        "flask==3.1.3 --hash=sha256:abc\n", encoding="utf-8"
    )
    (source / "requirements-lock-macos.txt").write_text(
        "flask==3.1.3 --hash=sha256:abc\n", encoding="utf-8"
    )
    (source / "package-lock.json").write_text(
        json.dumps({"packages": {"": {"devDependencies": {"vitest": "3.2.7"}}}}),
        encoding="utf-8",
    )
    output = tmp_path / "public"
    visible = [
        "CLAUDE.md", "app.py", "requirements-lock-linux.txt",
        "requirements-lock-macos.txt", "package-lock.json",
    ]
    with mock.patch(
        "scripts.build_public_artifact.git_visible_files", return_value=visible
    ):
        build_artifact(source, output, verify=False)
    assert "Not verified" not in (output / "CLAUDE.md").read_text(encoding="utf-8")
    manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["dependency_versions"]["python_linux"] == {"flask": "3.1.3"}
    assert manifest["dependency_versions"]["javascript"] == {"vitest": "3.2.7"}
    assert list(manifest["included_paths"]) == sorted(manifest["included_paths"])
    with mock.patch(
        "scripts.build_public_artifact.git_visible_files", return_value=visible
    ), pytest.raises(FileExistsError):
        build_artifact(source, output, verify=False)


def test_verification_uses_an_isolated_app_base(tmp_path, monkeypatch):
    monkeypatch.setenv("CHERQAIN_BASE_DIR", str(tmp_path / "must-not-use"))
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "CLAUDE.md").write_text("public\n", encoding="utf-8")
    calls = []

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return mock.Mock(returncode=0)

    with mock.patch("scripts.build_public_artifact.subprocess.run",
                    side_effect=capture):
        verify_artifact(staging)

    assert len(calls) == 9
    assert any(args[0][1:3] == ["scripts/check_docs.py", "--root"]
               for args, _kwargs in calls)
    bases = {kwargs["env"]["CHATEKLD_BASE_DIR"]
             for _args, kwargs in calls}
    assert len(bases) == 1
    assert all(kwargs["env"]["CHERQAIN_BASE_DIR"] == kwargs["env"]["CHATEKLD_BASE_DIR"]
               for _args, kwargs in calls)
    runtime = next(iter(bases))
    assert Path(runtime).parent == tmp_path
    assert not Path(runtime).exists()


def test_verification_caches_are_removed_without_touching_source(tmp_path):
    staging = tmp_path / "staging"
    source = staging / "source.py"
    cache = staging / "pkg" / "__pycache__"
    node_modules = staging / "node_modules" / "dep"
    cache.mkdir(parents=True)
    node_modules.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (staging / "requirements-lock-linux.txt").write_text("", encoding="utf-8")
    (staging / "requirements-lock-macos.txt").write_text("", encoding="utf-8")
    (staging / "package-lock.json").write_text(
        '{"packages":{"":{"devDependencies":{}}}}', encoding="utf-8")
    _write_manifest(staging)
    ruff_cache = staging / ".ruff_cache"
    ruff_cache.mkdir()
    (ruff_cache / "cache").write_bytes(b"lint cache")
    (cache / "source.pyc").write_bytes(b"cache")
    (node_modules / "index.js").write_text("module.exports = 1\n",
                                            encoding="utf-8")

    _cleanup_verification_artifacts(staging)

    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not cache.exists()
    assert not ruff_cache.exists()
    assert not (staging / "node_modules").exists()
    _assert_manifest_tree(staging)


def test_public_markdown_preserves_labels_without_dangling_private_links():
    from scripts.build_public_artifact import strip_private_links
    source = ('[evidence](../docs/receipt.md#results)\n'
              '[public](../README.md) [web](https://example.org/docs/receipt.md)\n'
              '`[literal](../docs/receipt.md)`\n'
              '```md\n[example](../docs/receipt.md)\n```\n')
    result = strip_private_links(source, 'docgen/README.md')
    assert result == source.replace('[evidence](../docs/receipt.md#results)',
                                    'evidence (private reference omitted)')
    assert strip_private_links(result, 'docgen/README.md') == result


def test_private_link_rewrite_precedes_manifest_and_leaves_source_intact(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    text = '[private](docs/receipt.md) [public](README.md)\n'
    for name in ('AGENTS.md', 'CLAUDE.md', 'README.md'):
        (source / name).write_text(text)
    (source / 'package-lock.json').write_text('{"packages": {}}')
    with mock.patch('scripts.build_public_artifact.git_visible_files',
                    return_value=['AGENTS.md', 'CLAUDE.md', 'README.md', 'package-lock.json']):
        output = build_artifact(source, tmp_path / 'artifact', verify=False)
    assert (source / 'README.md').read_text() == text
    assert (output / 'AGENTS.md').read_bytes() == (output / 'CLAUDE.md').read_bytes()
    assert '(docs/receipt.md)' not in (output / 'README.md').read_text()
    _assert_manifest_tree(output)
