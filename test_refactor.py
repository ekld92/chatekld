"""Hermetic tests for the Note Refactor analyzer (Phase 1, read-only).

Runs inside the hermetic suite (root conftest.py points CHATEKLD_BASE_DIR at a
temp dir before app import), so importing rag.vault / refactor / the route
blueprint is safe and never touches the user's real app data or vault.

The cardinal invariants pinned here:
  * the default plan makes **zero vision calls** and **zero vault writes**;
  * cache reuse is keyed by image-bytes sha256 (the §13 reuse surface);
  * path validators reject traversal / absolute / NUL.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from unittest import mock

import pytest


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _png_bytes(color=(10, 20, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def vault(tmp_path) -> Path:
    """A tiny vault: a scope sub-folder with a note + a central attachments dir."""
    root = Path(tmp_path / "vault_owner").resolve()
    scope = root / "study_notes"
    attachments = root / "Z_attachments"
    scope.mkdir(parents=True)
    attachments.mkdir(parents=True)

    (attachments / "7A27.png").write_bytes(_png_bytes((1, 2, 3)))
    (attachments / "fig.png").write_bytes(_png_bytes((9, 9, 9)))

    note = (
        "---\n"
        "tags: a, b, c\n"
        "related_notes: []\n"
        "---\n"
        "# Fluoxetine\n"
        "Dose habituelle **fluoxetine** 20 mg/j.\n"
        "\n"
        "![](7A27.png)\n"
        "\n"
        "## Schéma\n"
        "![[fig.png]]\n"
        "![](missing_image.png)\n"
    )
    (scope / "fluoxetine.md").write_text(note, encoding="utf-8")

    other = (
        "# Fluoxetine\n"
        "En pratique **fluoxetine** 200 mg/j (overdose).\n"
    )
    (scope / "autre.md").write_text(other, encoding="utf-8")
    return root


def _seed_description(vault_root: Path, rel: str, text: str) -> str:
    """Write *text* into the indexer's base image cache for *rel*; return digest."""
    from rag.vault import obsidian_manager
    import hashlib

    data = (vault_root / rel).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    obsidian_manager._atomic_write_text(
        obsidian_manager._image_cache_file(vault_root, digest), text
    )
    return digest


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
def test_resolver_builds_name_index_and_resolves_occurrences(vault):
    from refactor.resolver import build_name_index, scan_embeds

    name_index = build_name_index(vault)
    assert "7a27.png" in name_index and "fig.png" in name_index

    note_path = vault / "study_notes" / "fluoxetine.md"
    text = note_path.read_text(encoding="utf-8")
    occ = scan_embeds(text, note_path, vault, name_index)

    by_target = {o["target"]: o for o in occ}
    # Inline embed resolves bare-name to the central attachments folder.
    assert by_target["7A27.png"]["rel_path"] == "Z_attachments/7A27.png"
    assert by_target["7A27.png"]["is_image"] is True
    # Wikilink resolves the same way (Obsidian shortest-path).
    assert by_target["fig.png"]["rel_path"] == "Z_attachments/fig.png"
    # A bare name with no vault match falls back to the parent-relative path
    # (Obsidian's "where it would live next to the note") — which does not exist
    # on disk, so the plan later classifies it as a missing/broken embed. Only a
    # vault *escape* (e.g. "../x") yields an empty rel_path.
    assert by_target["missing_image.png"]["rel_path"] == "study_notes/missing_image.png"
    # Per-occurrence line numbers are populated and in document order.
    assert by_target["7A27.png"]["line"] == 8
    assert occ == sorted(occ, key=lambda o: o["start"])


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
def test_cache_digest_and_description_roundtrip(vault):
    from refactor import cache

    info = cache.digest_for("Z_attachments/7A27.png", vault)
    assert info["status"] == "ok" and len(info["digest"]) == 64

    missing = cache.digest_for("Z_attachments/nope.png", vault)
    assert missing["status"] == "missing" and missing["digest"] == ""

    digest = info["digest"]
    assert cache.read_description(digest, vault) == ""  # nothing seeded yet
    _seed_description(vault, "Z_attachments/7A27.png", "a cached description")
    assert cache.read_description(digest, vault) == "a cached description"

    # Per-mode caches are independent and never touch the base <sha256>.txt.
    cache.write_mode(digest, vault, "table", "| a | b |")
    assert cache.read_mode(digest, vault, "table") == "| a | b |"
    cache.write_mode(digest, vault, "redescribe", "fresh desc")
    # best_description prefers a refactor re-description over the base cache.
    assert cache.best_description(digest, vault) == "fresh desc"
    # Base cache untouched.
    assert cache.read_description(digest, vault) == "a cached description"


def test_cache_write_mode_rejects_unknown_mode(vault):
    from refactor import cache

    with pytest.raises(ValueError):
        cache.write_mode("deadbeef", vault, "../escape", "x")


# --------------------------------------------------------------------------- #
# hints
# --------------------------------------------------------------------------- #
def test_likely_table_heuristic():
    from refactor.hints import likely_table

    assert likely_table("")[0] is False
    assert likely_table("A simple sentence about a molecule.")[0] is False
    assert likely_table("| Drug | Dose |\n| fluox | 20 mg |")[0] is True
    assert likely_table("Voici un tableau des posologies.")[0] is True
    assert likely_table("doses: 5 mg, 10 mg, 20 mg here")[0] is True


# --------------------------------------------------------------------------- #
# hygiene
# --------------------------------------------------------------------------- #
def test_hygiene_flags_broken_embeds_and_frontmatter(vault):
    from refactor import hygiene
    from refactor.result import ImageRef, STATUS_MISSING, STATUS_UNRESOLVED

    images = [
        ImageRef(raw_link="![](x)", target="x.png", rel_path="", line=3, status=STATUS_UNRESOLVED),
        ImageRef(raw_link="![](y)", target="y.png", rel_path="a/y.png", line=5, status=STATUS_MISSING),
    ]
    notes = hygiene.embed_notes(images)
    kinds = {n.kind for n in notes}
    assert kinds == {"unresolved_embed", "broken_embed"}

    fm = hygiene.frontmatter_notes("---\ntags: a, b, c\n---\nbody\n")
    assert any(n.kind == "frontmatter" for n in fm)
    assert hygiene.frontmatter_notes("no frontmatter here") == []


# --------------------------------------------------------------------------- #
# discrepancy
# --------------------------------------------------------------------------- #
def test_discrepancy_flags_disjoint_doses_across_notes():
    from refactor import discrepancy

    doses = (
        discrepancy.extract_doses("a.md", "**fluoxetine** 20 mg/j")
        + discrepancy.extract_doses("b.md", "**fluoxetine** 200 mg/j")
    )
    flagged = discrepancy.cross_check(doses)
    assert len(flagged) == 1
    assert flagged[0].subject == "fluoxetine"
    assert len(flagged[0].occurrences) == 2


def test_discrepancy_ignores_small_spread_and_single_note():
    from refactor import discrepancy

    # Same note only — not cross-note.
    one = discrepancy.extract_doses("a.md", "**x** 20 mg and **x** 200 mg")
    assert discrepancy.cross_check(one) == []

    # Cross-note but spread under the ratio threshold.
    small = (
        discrepancy.extract_doses("a.md", "**y** 20 mg")
        + discrepancy.extract_doses("b.md", "**y** 40 mg")
    )
    assert discrepancy.cross_check(small) == []


# --------------------------------------------------------------------------- #
# extract (single-image vision path) — vision mocked
# --------------------------------------------------------------------------- #
def test_extract_table_double_read_flags_suspect_cells(vault):
    from refactor import cache, extract

    reads = ["| Drug | Dose |\n|---|---|\n| fluox | 20 mg |",
             "| Drug | Dose |\n|---|---|\n| fluox | 25 mg |"]
    with mock.patch.object(extract, "_vision_call", side_effect=reads) as m:
        res = extract.extract_table("Z_attachments/7A27.png", vault, double_read=True)

    assert m.call_count == 2
    assert res["error"] == ""
    assert res["text"].startswith("| Drug | Dose |")
    # The differing cell (20 vs 25 mg) is flagged suspect.
    assert res["suspect_cells"]
    assert res["cached"] is True

    # Result cached to obsidian_cache under the table mode (not the base cache).
    info = cache.digest_for("Z_attachments/7A27.png", vault)
    assert cache.read_mode(info["digest"], vault, "table") == res["text"]
    assert cache.read_description(info["digest"], vault) == ""  # base untouched


def test_extract_table_agreeing_reads_have_no_suspect(vault):
    from refactor import extract

    same = "| Drug | Dose |\n|---|---|\n| fluox | 20 mg |"
    with mock.patch.object(extract, "_vision_call", side_effect=[same, same]):
        res = extract.extract_table("Z_attachments/7A27.png", vault, double_read=True)
    assert res["suspect_cells"] == []


def test_extract_redescribe_caches_redescribe_mode(vault):
    from refactor import cache, extract

    with mock.patch.object(
        extract.vision_manager, "describe_image", return_value="a fresh description"
    ):
        res = extract.redescribe("Z_attachments/7A27.png", vault)
    assert res["text"] == "a fresh description" and res["cached"] is True
    info = cache.digest_for("Z_attachments/7A27.png", vault)
    assert cache.read_mode(info["digest"], vault, "redescribe") == "a fresh description"


# --------------------------------------------------------------------------- #
# plan — the read-only orchestrator
# --------------------------------------------------------------------------- #
def test_build_plan_is_read_only_and_makes_zero_vision_calls(vault):
    """The default plan never calls vision and never mutates the vault."""
    from refactor import plan
    import services.vision as vision

    # Record vault file mtimes before.
    md_files = list((vault / "study_notes").rglob("*.md"))
    before = {p: p.stat().st_mtime_ns for p in md_files}
    img_before = (vault / "Z_attachments" / "7A27.png").stat().st_mtime_ns

    _seed_description(vault, "Z_attachments/7A27.png", "Tableau des posologies : 5 mg, 10 mg, 20 mg.")

    boom = mock.Mock(side_effect=AssertionError("vision must not be called by the plan"))
    events = []
    with mock.patch.object(vision, "_chat_ollama_image", boom), \
         mock.patch.object(vision, "_chat_lm_studio_image", boom), \
         mock.patch.object(vision.vision_manager, "describe_image", boom):
        result = plan.build_plan(vault, "study_notes", on_event=events.append)

    # Vault files unchanged.
    for p, mt in before.items():
        assert p.stat().st_mtime_ns == mt
    assert (vault / "Z_attachments" / "7A27.png").stat().st_mtime_ns == img_before

    assert result.note_count == 2
    # The seeded description is inlined as a callout in the proposal diff.
    fluox = next(n for n in result.notes if n.rel_path.endswith("fluoxetine.md"))
    assert fluox.changed
    assert "[!extracted]" in fluox.proposed
    assert "Tableau des posologies" in fluox.proposed
    # Streamed per-note frames were emitted.
    assert any("note" in e for e in events)
    # The seeded description trips the likely-table hint.
    img = next(i for i in fluox.images if i.rel_path == "Z_attachments/7A27.png")
    assert img.likely_table is True
    # The missing embed is flagged in hygiene.
    assert any(h.kind in ("broken_embed", "unresolved_embed") for h in fluox.hygiene_notes)
    # Cross-note dose discrepancy (20 vs 200 mg) surfaces.
    assert any(d.subject == "fluoxetine" for d in result.discrepancies)


def test_build_plan_diff_empty_when_no_descriptions(vault):
    from refactor import plan
    import services.vision as vision

    boom = mock.Mock(side_effect=AssertionError("no vision"))
    with mock.patch.object(vision, "_chat_ollama_image", boom), \
         mock.patch.object(vision, "_chat_lm_studio_image", boom):
        result = plan.build_plan(vault, "study_notes")
    # No cached descriptions seeded ⇒ no inlines ⇒ no proposed change.
    for n in result.notes:
        assert not n.changed
        assert n.proposed == n.original


# --------------------------------------------------------------------------- #
# route validators + blueprint
# --------------------------------------------------------------------------- #
def test_resolve_scope_and_image_validators(vault):
    from api.routes import refactor as route

    root = str(vault)
    assert route._resolve_scope("study_notes", root) == "study_notes"
    assert route._resolve_scope("../etc", root) is None
    assert route._resolve_scope("/abs", root) is None
    assert route._resolve_scope("does_not_exist", root) is None
    assert route._resolve_scope("med\x00", root) is None

    assert route._resolve_image_rel("Z_attachments/7A27.png", root) == "Z_attachments/7A27.png"
    assert route._resolve_image_rel("../secret.png", root) is None
    assert route._resolve_image_rel("/etc/passwd.png", root) is None
    assert route._resolve_image_rel("study_notes/fluoxetine.md", root) is None  # not an image
    assert route._resolve_image_rel("Z_attachments/missing.png", root) is None  # not a file
    assert route._resolve_image_rel("Z_attachments/7A27\n.png", root) is None


def test_blueprint_registered():
    from app import app

    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/refactor/plan" in rules
    assert "/api/refactor/extract-image" in rules
    assert "/api/refactor/image" in rules
    assert "/api/refactor/native-pick-folder" in rules
    assert "/api/refactor/ignore" in rules


def test_routes_require_local_origin():
    from app import app

    client = app.test_client()
    assert client.post("/api/refactor/plan", json={}).status_code == 403
    assert client.post("/api/refactor/extract-image", json={}).status_code == 403
    assert client.get("/api/refactor/image?rel=x.png").status_code == 403
    assert client.post("/api/refactor/native-pick-folder", json={}).status_code == 403
    assert client.get("/api/refactor/ignore").status_code == 403
    assert client.post("/api/refactor/ignore", json={}).status_code == 403


def test_plan_route_streams_notes_and_summary(vault):
    from app import app
    from rag.vault import obsidian_manager
    import services.vision as vision

    _seed_description(vault, "Z_attachments/7A27.png", "cached dose description")
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}

    boom = mock.Mock(side_effect=AssertionError("vision must not be called"))
    with mock.patch.object(obsidian_manager, "get_vault_path", return_value=str(vault)), \
         mock.patch.object(vision, "_chat_ollama_image", boom), \
         mock.patch.object(vision, "_chat_lm_studio_image", boom):
        r = client.post("/api/refactor/plan", json={"scope_subdir": "study_notes"}, headers=headers)
        body = r.get_data(as_text=True)

    assert r.status_code == 200
    assert '"note"' in body
    assert '"refactor"' in body
    assert "[DONE]" in body


def test_plan_route_400_without_vault(vault):
    from app import app
    from rag.vault import obsidian_manager

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(obsidian_manager, "get_vault_path", return_value=""):
        r = client.post("/api/refactor/plan", json={}, headers=headers)
    assert r.status_code == 400


def test_extract_image_route(vault):
    from app import app
    from rag.vault import obsidian_manager
    from refactor import extract as extract_mod

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    fake = {"mode": "table", "text": "| a |\n|---|\n| 1 |", "suspect_cells": [], "cached": True, "error": ""}
    with mock.patch.object(obsidian_manager, "get_vault_path", return_value=str(vault)), \
         mock.patch.object(extract_mod, "extract_table", return_value=fake) as m:
        r = client.post(
            "/api/refactor/extract-image",
            json={"rel": "Z_attachments/7A27.png", "mode": "table"},
            headers=headers,
        )
    assert r.status_code == 200
    assert m.call_count == 1
    payload = r.get_json()
    assert payload["ok"] is True and payload["cached"] is True

    # Bad mode / bad rel rejected before any vision work.
    with mock.patch.object(obsidian_manager, "get_vault_path", return_value=str(vault)):
        bad_mode = client.post(
            "/api/refactor/extract-image",
            json={"rel": "Z_attachments/7A27.png", "mode": "nope"}, headers=headers)
        bad_rel = client.post(
            "/api/refactor/extract-image",
            json={"rel": "../x.png", "mode": "table"}, headers=headers)
    assert bad_mode.status_code == 400
    assert bad_rel.status_code == 400


def test_image_route_serves_bytes(vault):
    from app import app
    from rag.vault import obsidian_manager

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(obsidian_manager, "get_vault_path", return_value=str(vault)):
        ok = client.get("/api/refactor/image?rel=Z_attachments/7A27.png", headers=headers)
        bad = client.get("/api/refactor/image?rel=../secret.png", headers=headers)
    assert ok.status_code == 200
    assert ok.mimetype == "image/png"
    assert bad.status_code == 400


# --------------------------------------------------------------------------- #
# config validators
# --------------------------------------------------------------------------- #
def test_config_validators_for_refactor_keys():
    from api.routes.config import _CONFIG_VALIDATORS

    scope = _CONFIG_VALIDATORS["refactor_scope_subdir"]
    assert scope("study_notes") == "study_notes"
    assert scope("../escape") is None
    assert scope("/abs") is None
    assert scope("") is None

    model = _CONFIG_VALIDATORS["refactor_extract_model"]
    assert model("") == ""            # "" kept (means: use vision_model)
    assert model("gemma-4-e2b") == "gemma-4-e2b"
    assert model("bad\nname") is None

    assert _CONFIG_VALIDATORS["refactor_table_double_read"](True) is True


# --------------------------------------------------------------------------- #
# audit-fix regressions
# --------------------------------------------------------------------------- #
def test_build_name_index_honors_user_exclusions(vault):
    """A user vault_exclude_dirs entry hides those images from the resolver,
    matching the indexer's _should_skip_path (no spurious resolution)."""
    from refactor import resolver

    with mock.patch.object(
        resolver, "load_config", return_value={"vault_exclude_dirs": ["Z_attachments"]}
    ):
        excluded = resolver.excluded_dirs(vault)
        idx = resolver.build_name_index(vault, excluded)
    assert resolver.is_excluded("Z_attachments/7A27.png", excluded)
    assert idx == {}  # every image lived under the excluded folder


def test_plan_skips_user_excluded_notes(vault):
    """A note inside an excluded sub-folder is not analyzed by the plan."""
    from refactor import plan, resolver
    import services.vision as vision

    drafts = vault / "study_notes" / "drafts"
    drafts.mkdir()
    (drafts / "wip.md").write_text("# WIP\n", encoding="utf-8")

    boom = mock.Mock(side_effect=AssertionError("no vision"))
    with mock.patch.object(
        resolver, "load_config",
        return_value={"vault_exclude_dirs": ["study_notes/drafts"]},
    ), mock.patch.object(vision, "_chat_ollama_image", boom), \
         mock.patch.object(vision, "_chat_lm_studio_image", boom):
        result = plan.build_plan(vault, "study_notes")

    rels = {n.rel_path for n in result.notes}
    assert "study_notes/fluoxetine.md" in rels
    assert "study_notes/drafts/wip.md" not in rels


def test_frontmatter_notes_handles_crlf():
    """CRLF frontmatter is still detected (Obsidian uses LF, but be tolerant)."""
    from refactor import hygiene

    notes = hygiene.frontmatter_notes("---\r\ntags: a, b, c\r\n---\r\nbody\r\n")
    assert any(n.kind == "frontmatter" for n in notes)
    # A mid-note '---' horizontal rule is not mistaken for frontmatter.
    assert hygiene.frontmatter_notes("text\n\n---\ntags: a, b\n---\n") == []


def test_scan_embeds_preserves_bang_prefix(vault):
    """Inline image embeds keep their leading '!' in the displayed raw_link."""
    from refactor.resolver import build_name_index, scan_embeds

    note_path = vault / "study_notes" / "fluoxetine.md"
    text = note_path.read_text(encoding="utf-8")
    occ = scan_embeds(text, note_path, vault, build_name_index(vault))
    inline = next(o for o in occ if o["target"] == "7A27.png")
    assert inline["raw"].startswith("![](")


# --------------------------------------------------------------------------- #
# Hub: (a) folder picker scope conversion
# --------------------------------------------------------------------------- #
def test_abs_to_scope_validator(vault):
    from api.routes import refactor as route

    root = str(vault)  # the fixture root is already resolve()'d (== its realpath)
    assert route._abs_to_scope(str(vault / "study_notes"), root) == "study_notes"
    assert route._abs_to_scope(root, root) is None                       # vault root itself
    assert route._abs_to_scope(str(vault.parent), root) is None          # outside the vault
    assert route._abs_to_scope(
        str(vault / "study_notes" / "fluoxetine.md"), root) is None  # a file, not a dir
    assert route._abs_to_scope("", root) is None
    assert route._abs_to_scope(None, root) is None


# --------------------------------------------------------------------------- #
# Hub: (b) note frame now carries the rendered-preview bodies
# --------------------------------------------------------------------------- #
def test_note_frame_includes_bodies(vault):
    from refactor import plan
    import services.vision as vision

    _seed_description(vault, "Z_attachments/7A27.png", "a cached description")
    boom = mock.Mock(side_effect=AssertionError("no vision"))
    with mock.patch.object(vision, "_chat_ollama_image", boom), \
         mock.patch.object(vision, "_chat_lm_studio_image", boom):
        result = plan.build_plan(vault, "study_notes")

    fluox = next(n for n in result.notes if n.rel_path.endswith("fluoxetine.md"))
    frame = fluox.frame()
    assert frame["original"] == fluox.original
    assert frame["proposed"] == fluox.proposed
    assert frame["diff"] == fluox.diff  # diff view still available alongside bodies


# --------------------------------------------------------------------------- #
# Hub: (c) classify mode
# --------------------------------------------------------------------------- #
def test_parse_label_maps_replies():
    from refactor.extract import _parse_label

    assert _parse_label("handwritten") == "handwritten"
    assert _parse_label("printed-table") == "printed-table"
    assert _parse_label("This is a printed table of doses.") == "printed-table"
    assert _parse_label("a hand-written note") == "handwritten"
    assert _parse_label("a handwritten table") == "handwritten"   # handwritten wins
    assert _parse_label("schéma synaptique") == "figure-diagram"
    assert _parse_label("a photograph of pills") == "photo"
    assert _parse_label("???") == "other"
    assert _parse_label("") == "other"


def test_classify_caches_label_not_base(vault):
    from refactor import cache, extract

    with mock.patch.object(extract, "_vision_call", return_value="handwritten") as m:
        res = extract.classify("Z_attachments/7A27.png", vault)
    assert m.call_count == 1
    assert res["label"] == "handwritten" and res["cached"] is True
    info = cache.digest_for("Z_attachments/7A27.png", vault)
    assert cache.read_mode(info["digest"], vault, "classify") == "handwritten"
    assert cache.read_description(info["digest"], vault) == ""  # base cache untouched


def test_classify_empty_reply_errors_and_does_not_cache(vault):
    """An empty (non-exception) model reply is a failed pass — surfaced as an
    error and NOT cached, so a later plan run never shows a bogus 'other'."""
    from refactor import cache, extract

    with mock.patch.object(extract, "_vision_call", return_value="   "):  # whitespace-only
        res = extract.classify("Z_attachments/7A27.png", vault)
    assert res["error"] and res["label"] == "" and res["cached"] is False
    info = cache.digest_for("Z_attachments/7A27.png", vault)
    assert cache.read_mode(info["digest"], vault, "classify") == ""  # nothing written


# --------------------------------------------------------------------------- #
# Hub: (c) sticky ignore-list
# --------------------------------------------------------------------------- #
def test_ignore_list_roundtrip_and_location(vault):
    from refactor import ignore
    from core.constants import OBSIDIAN_CACHE_DIR

    assert ignore.load_ignored(vault) == set()
    assert ignore.add(vault, "Z_attachments/7A27.png") == ["Z_attachments/7A27.png"]
    assert ignore.load_ignored(vault) == {"Z_attachments/7A27.png"}
    ignore.add(vault, "Z_attachments/7A27.png")  # idempotent
    assert ignore.list_ignored(vault) == ["Z_attachments/7A27.png"]
    assert ignore.remove(vault, "Z_attachments/7A27.png") == []
    assert ignore.load_ignored(vault) == set()

    # The sidecar lives under obsidian_cache (the app cache dir) — NEVER the vault.
    sidecar = ignore._ignore_file(vault)
    assert str(sidecar).startswith(str(OBSIDIAN_CACHE_DIR))
    assert str(vault) not in str(sidecar)


def test_ignore_tolerates_missing_and_corrupt(vault):
    from refactor import ignore

    assert ignore.load_ignored(vault) == set()           # missing → empty
    path = ignore._ignore_file(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert ignore.load_ignored(vault) == set()           # corrupt → empty
    path.write_text('{"images": "nope"}', encoding="utf-8")
    assert ignore.load_ignored(vault) == set()           # wrong shape → empty


def test_ignore_concurrent_adds_do_not_lose_updates(vault):
    """Regression for the lost-update race: many threads adding distinct paths
    must all survive (the load→mutate→save runs under ignore._LOCK). Without the
    lock, concurrent read-modify-writes would clobber each other."""
    import threading
    from refactor import ignore

    rels = [f"Z_attachments/img_{i:03d}.png" for i in range(40)]
    barrier = threading.Barrier(len(rels))

    def _worker(rel):
        barrier.wait()           # release all threads together to maximize contention
        ignore.add(vault, rel)

    threads = [threading.Thread(target=_worker, args=(r,)) for r in rels]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert ignore.load_ignored(vault) == set(rels)   # every add survived


def test_plan_ignored_image_skipped_and_classification_surfaced(vault):
    from refactor import cache, ignore, plan
    import services.vision as vision

    digest = _seed_description(
        vault, "Z_attachments/7A27.png", "Tableau des posologies : 5 mg, 10 mg, 20 mg.")
    cache.write_mode(digest, vault, "classify", "handwritten")
    ignore.add(vault, "Z_attachments/7A27.png")

    boom = mock.Mock(side_effect=AssertionError("no vision"))
    with mock.patch.object(vision, "_chat_ollama_image", boom), \
         mock.patch.object(vision, "_chat_lm_studio_image", boom):
        result = plan.build_plan(vault, "study_notes")

    fluox = next(n for n in result.notes if n.rel_path.endswith("fluoxetine.md"))
    img = next(i for i in fluox.images if i.rel_path == "Z_attachments/7A27.png")
    assert img.ignored is True
    assert img.classification == "handwritten"
    # Ignored ⇒ no inlined callout ⇒ the note body is left unchanged by it.
    assert "[!extracted]" not in fluox.proposed
    assert not fluox.changed
    # Surfaced in the summary counts.
    assert result.ignored_count == 1
    assert result.handwritten_count == 1


# --------------------------------------------------------------------------- #
# Hub: (a)/(c) route layer
# --------------------------------------------------------------------------- #
def test_resolve_ignore_rel_validator(vault):
    from api.routes import refactor as route

    root = str(vault)
    # Shape-valid; the file need NOT exist (so a moved/dataless image can be un-ignored).
    assert route._resolve_ignore_rel("Z_attachments/gone.png", root) == "Z_attachments/gone.png"
    assert route._resolve_ignore_rel("Z_attachments/7A27.png", root) == "Z_attachments/7A27.png"
    assert route._resolve_ignore_rel("../escape.png", root) is None
    assert route._resolve_ignore_rel("/etc/x.png", root) is None
    assert route._resolve_ignore_rel("study_notes/fluoxetine.md", root) is None  # not an image
    assert route._resolve_ignore_rel("Z_attachments/7A27\n.png", root) is None


def test_extract_image_route_classify(vault):
    from app import app
    from rag.vault import obsidian_manager
    from refactor import extract as extract_mod

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    fake = {"mode": "classify", "label": "handwritten", "text": "handwritten",
            "suspect_cells": [], "cached": True, "error": ""}
    with mock.patch.object(obsidian_manager, "get_vault_path", return_value=str(vault)), \
         mock.patch.object(extract_mod, "classify", return_value=fake) as m:
        r = client.post("/api/refactor/extract-image",
                        json={"rel": "Z_attachments/7A27.png", "mode": "classify"}, headers=headers)
    assert r.status_code == 200 and m.call_count == 1
    assert r.get_json()["label"] == "handwritten"


def test_ignore_route_get_and_toggle(vault):
    from app import app
    from rag.vault import obsidian_manager

    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    with mock.patch.object(obsidian_manager, "get_vault_path", return_value=str(vault)):
        assert client.get("/api/refactor/ignore", headers=headers).get_json()["ignored"] == []

        added = client.post("/api/refactor/ignore",
                            json={"rel": "Z_attachments/7A27.png", "action": "add"}, headers=headers)
        assert added.status_code == 200
        assert added.get_json()["ignored"] == ["Z_attachments/7A27.png"]
        assert client.get("/api/refactor/ignore", headers=headers).get_json()["ignored"] == [
            "Z_attachments/7A27.png"]

        removed = client.post("/api/refactor/ignore",
                              json={"rel": "Z_attachments/7A27.png", "action": "remove"}, headers=headers)
        assert removed.get_json()["ignored"] == []

        bad_rel = client.post("/api/refactor/ignore",
                              json={"rel": "../x.png", "action": "add"}, headers=headers)
        bad_act = client.post("/api/refactor/ignore",
                              json={"rel": "Z_attachments/7A27.png", "action": "nope"}, headers=headers)
    assert bad_rel.status_code == 400
    assert bad_act.status_code == 400


# --------------------------------------------------------------------------- #
# Phase 2 — vault writes (apply / archive / restore)
# --------------------------------------------------------------------------- #
def _plan_proposal(vault, rel):
    """Run the read-only plan and return the NoteProposal frame for *rel*."""
    from refactor.plan import build_plan
    res = build_plan(vault, "study_notes")
    for n in res.notes:
        if n.rel_path == rel:
            return n.frame()
    raise AssertionError(f"no proposal for {rel}")


def _no_vision():
    """Context managers asserting the write paths make zero vision calls."""
    import services.vision as vision
    boom = mock.Mock(side_effect=AssertionError("vision must not be called"))
    return (
        mock.patch.object(vision, "_chat_ollama_image", boom),
        mock.patch.object(vision, "_chat_lm_studio_image", boom),
        mock.patch.object(vision.vision_manager, "describe_image", boom),
    )


def test_apply_writes_callout_keeps_embed_and_snapshots(vault):
    from core.config import load_config
    from refactor import apply as apply_mod, journal

    _seed_description(vault, "Z_attachments/7A27.png", "A dopamine pathway diagram.")
    frame = _plan_proposal(vault, "study_notes/fluoxetine.md")
    assert frame["changed"] is True
    cfg = load_config()
    approved = [{"rel": "study_notes/fluoxetine.md",
                 "content_sha256": frame["content_sha256"],
                 "proposed_sha256": frame["proposed_sha256"]}]

    import contextlib
    with mock.patch("refactor.journal.log_vault_write") as spy, contextlib.ExitStack() as stack:
        for cm in _no_vision():
            stack.enter_context(cm)
        results = apply_mod.apply_notes(vault, cfg, approved)

    assert results[0]["status"] == "applied"
    body = (vault / "study_notes/fluoxetine.md").read_text(encoding="utf-8")
    assert "[!extracted]" in body           # callout inlined
    assert "![](7A27.png)" in body          # original embed KEPT (callout-only)
    # a snapshot of the pre-write note exists under the archive dir
    man = journal.load(vault, cfg)
    op = [o for o in man["ops"] if o["kind"] == "apply_note"][0]
    assert (journal.archive_dir(vault, cfg) / op["snapshot_rel"]).exists()
    assert spy.called  # the mutation was audit-logged


def test_apply_stale_guard_skips_and_leaves_file(vault):
    from core.config import load_config
    from refactor import apply as apply_mod

    _seed_description(vault, "Z_attachments/7A27.png", "desc")
    frame = _plan_proposal(vault, "study_notes/fluoxetine.md")
    before = (vault / "study_notes/fluoxetine.md").read_bytes()
    approved = [{"rel": "study_notes/fluoxetine.md",
                 "content_sha256": "deadbeef" * 8,   # wrong on-disk hash
                 "proposed_sha256": frame["proposed_sha256"]}]
    results = apply_mod.apply_notes(vault, load_config(), approved)
    assert results[0]["status"] == "skipped" and "stale" in results[0]["message"]
    assert (vault / "study_notes/fluoxetine.md").read_bytes() == before


def test_apply_wysiwyg_drift_skips_and_leaves_file(vault):
    from core.config import load_config
    from refactor import apply as apply_mod

    _seed_description(vault, "Z_attachments/7A27.png", "desc")
    frame = _plan_proposal(vault, "study_notes/fluoxetine.md")
    before = (vault / "study_notes/fluoxetine.md").read_bytes()
    approved = [{"rel": "study_notes/fluoxetine.md",
                 "content_sha256": frame["content_sha256"],
                 "proposed_sha256": "f00dface" * 8}]   # preview drifted
    results = apply_mod.apply_notes(vault, load_config(), approved)
    assert results[0]["status"] == "skipped" and "drift" in results[0]["message"]
    assert (vault / "study_notes/fluoxetine.md").read_bytes() == before


def test_archive_refuses_shared_image_without_moving(vault):
    from core.config import load_config
    from refactor import archive as archive_mod
    from refactor.result import sha256_bytes

    # A SECOND scope note also embeds 7A27.png → shared → must not move.
    (vault / "study_notes/shared_ref.md").write_text(
        "# Other\n![](7A27.png)\n", encoding="utf-8")
    note = vault / "study_notes/fluoxetine.md"
    ch = sha256_bytes(note.read_bytes())
    res = archive_mod.archive_image(
        vault, load_config(), "study_notes",
        "study_notes/fluoxetine.md", "Z_attachments/7A27.png", ch)
    assert res["status"] == "shared" and res["shared"] is True
    assert (vault / "Z_attachments/7A27.png").exists()   # original untouched


def test_archive_moves_thumbnails_swaps_embed_and_excludes_thumbs(vault):
    from core.config import load_config
    from refactor import archive as archive_mod, journal
    from refactor.result import sha256_bytes

    note = vault / "study_notes/fluoxetine.md"
    ch = sha256_bytes(note.read_bytes())
    with mock.patch("refactor.journal.log_vault_write") as spy:
        res = archive_mod.archive_image(
            vault, load_config(), "study_notes",
            "study_notes/fluoxetine.md", "Z_attachments/7A27.png", ch)
    assert res["ok"] is True, res
    # original moved OUT of the vault into the archive dir
    assert not (vault / "Z_attachments/7A27.png").exists()
    arch = journal.archive_dir(vault, load_config())
    assert (arch / res["archive_rel"]).exists()
    # a PNG thumbnail now lives under <scope>/_thumbs and the embed points at it
    thumb = vault / res["thumb_rel"]
    assert thumb.exists() and thumb.suffix == ".png"
    body = note.read_text(encoding="utf-8")
    assert "_thumbs/7A27.png" in body
    # _thumbs is excluded from indexing
    assert "study_notes/_thumbs" in load_config().get("vault_exclude_dirs", [])
    assert spy.called


def test_restore_reverts_apply(vault):
    from core.config import load_config
    from refactor import apply as apply_mod, journal

    _seed_description(vault, "Z_attachments/7A27.png", "desc")
    frame = _plan_proposal(vault, "study_notes/fluoxetine.md")
    cfg = load_config()
    original = (vault / "study_notes/fluoxetine.md").read_bytes()
    apply_mod.apply_notes(vault, cfg, [{
        "rel": "study_notes/fluoxetine.md",
        "content_sha256": frame["content_sha256"],
        "proposed_sha256": frame["proposed_sha256"]}])
    assert (vault / "study_notes/fluoxetine.md").read_bytes() != original

    man = journal.load(vault, cfg)
    op = [o for o in man["ops"] if o["kind"] == "apply_note"][0]
    r = journal.revert_op(vault, cfg, op)
    journal.save(vault, cfg, man)
    assert r["status"] == "reverted"
    assert (vault / "study_notes/fluoxetine.md").read_bytes() == original
    assert journal.find_op(journal.load(vault, cfg), op["id"])["state"] == "reverted"


def test_restore_reverts_archive(vault):
    from core.config import load_config
    from refactor import archive as archive_mod, journal
    from refactor.result import sha256_bytes

    note = vault / "study_notes/fluoxetine.md"
    before = note.read_bytes()
    img_before = (vault / "Z_attachments/7A27.png").read_bytes()
    cfg = load_config()
    res = archive_mod.archive_image(
        vault, cfg, "study_notes",
        "study_notes/fluoxetine.md", "Z_attachments/7A27.png",
        sha256_bytes(before))
    assert res["ok"]
    thumb = vault / res["thumb_rel"]

    man = journal.load(vault, cfg)
    op = [o for o in man["ops"] if o["kind"] == "archive_image"][0]
    r = journal.revert_op(vault, cfg, op)
    journal.save(vault, cfg, man)
    assert r["status"] == "reverted"
    assert (vault / "Z_attachments/7A27.png").read_bytes() == img_before  # original back
    assert note.read_bytes() == before                                    # embed restored
    assert not thumb.exists()                                             # thumbnail gone


def test_make_thumbnail_is_downscaled_png():
    from refactor.archive import make_thumbnail
    from PIL import Image

    big = io.BytesIO()
    Image.new("RGB", (2000, 1000), (5, 5, 5)).save(big, format="PNG")
    out = make_thumbnail(big.getvalue(), 384)
    assert out[:8] == b"\x89PNG\r\n\x1a\n"   # PNG signature
    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) <= 384


# --- route layer ----------------------------------------------------------- #
def test_phase2_routes_registered_and_local_origin():
    from app import app

    rules = {r.rule for r in app.url_map.iter_rules()}
    assert {"/api/refactor/apply", "/api/refactor/archive",
            "/api/refactor/restore", "/api/refactor/manifest"} <= rules

    client = app.test_client()
    assert client.post("/api/refactor/apply", json={}).status_code == 403
    assert client.post("/api/refactor/archive", json={}).status_code == 403
    assert client.post("/api/refactor/restore", json={}).status_code == 403
    assert client.get("/api/refactor/manifest").status_code == 403


def test_apply_route_requires_confirm_and_applies(vault):
    from app import app
    from rag.vault import obsidian_manager

    _seed_description(vault, "Z_attachments/7A27.png", "desc")
    frame = _plan_proposal(vault, "study_notes/fluoxetine.md")
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    note = {"rel": "study_notes/fluoxetine.md",
            "content_sha256": frame["content_sha256"],
            "proposed_sha256": frame["proposed_sha256"]}

    with mock.patch.object(obsidian_manager, "get_vault_path", return_value=str(vault)):
        # no confirm → 400, nothing written
        r0 = client.post("/api/refactor/apply",
                         json={"scope_subdir": "study_notes", "notes": [note]},
                         headers=headers)
        assert r0.status_code == 400
        # confirmed → applied
        r1 = client.post("/api/refactor/apply",
                         json={"scope_subdir": "study_notes", "confirm": True, "notes": [note]},
                         headers=headers)
    assert r1.status_code == 200
    assert r1.get_json()["applied"] == 1
    assert "[!extracted]" in (vault / "study_notes/fluoxetine.md").read_text(encoding="utf-8")


def test_apply_route_503_when_indexing(vault):
    from app import app
    from rag.vault import obsidian_manager

    _seed_description(vault, "Z_attachments/7A27.png", "desc")
    frame = _plan_proposal(vault, "study_notes/fluoxetine.md")
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    note = {"rel": "study_notes/fluoxetine.md",
            "content_sha256": frame["content_sha256"],
            "proposed_sha256": frame["proposed_sha256"]}
    assert obsidian_manager.try_acquire_lock(ttl=30)   # simulate an indexing run holding it
    try:
        with mock.patch.object(obsidian_manager, "get_vault_path", return_value=str(vault)):
            r = client.post("/api/refactor/apply",
                            json={"scope_subdir": "study_notes", "confirm": True, "notes": [note]},
                            headers=headers)
        assert r.status_code == 503
    finally:
        obsidian_manager.release_lock()


def test_scope_note_rel_validator_rejects_escapes(vault):
    from api.routes.refactor import _resolve_scope_note_rel
    root = str(vault)
    ok = _resolve_scope_note_rel("study_notes/fluoxetine.md", root, "study_notes")
    assert ok == "study_notes/fluoxetine.md"
    # outside the scope sub-folder
    assert _resolve_scope_note_rel("autre_folder/x.md", root, "study_notes") is None
    # traversal / absolute / non-md
    assert _resolve_scope_note_rel("../x.md", root, "study_notes") is None
    assert _resolve_scope_note_rel("/abs/x.md", root, "study_notes") is None
    assert _resolve_scope_note_rel("study_notes/x.txt", root, "study_notes") is None


def test_config_validators_phase2_keys():
    from api.routes.config import _CONFIG_VALIDATORS

    arch = _CONFIG_VALIDATORS["refactor_archive_dir"]
    assert arch("") == ""                       # cleared → default
    assert arch("/Users/me/backups/x") == "/Users/me/backups/x"
    assert arch("~/x") == "~/x"
    assert arch("relative/path") is None        # must be absolute
    assert arch("bad\npath") is None

    side = _CONFIG_VALIDATORS["refactor_thumb_max_side"]
    assert side(384) == 384
    assert side(50) == 96                        # clamped up
    assert side(5000) == 1024                    # clamped down


# --------------------------------------------------------------------------- #
# Phase 2 — self-audit fixes (revert atomicity, reference-check breadth)
# --------------------------------------------------------------------------- #
def _archive_one(vault, image_rel="Z_attachments/7A27.png",
                 note_rel="study_notes/fluoxetine.md"):
    """Archive one image and return (cfg, result)."""
    from core.config import load_config
    from refactor import archive as archive_mod
    from refactor.result import sha256_bytes
    cfg = load_config()
    note = vault / note_rel
    res = archive_mod.archive_image(
        vault, cfg, "study_notes", note_rel, image_rel,
        sha256_bytes(note.read_bytes()))
    assert res["ok"], res
    return cfg, res


def test_archive_reference_check_includes_excluded_folders(vault):
    # FIX #3: a destructive move-safety gate must count references from notes in
    # the user's vault_exclude_dirs too (they break just the same when moved).
    from core.config import load_config, save_config
    from refactor import archive as archive_mod
    from refactor.result import sha256_bytes

    (vault / "Templates").mkdir()
    (vault / "Templates/ref.md").write_text("# T\n![](7A27.png)\n", encoding="utf-8")
    cfg = load_config()
    cfg["vault_exclude_dirs"] = ["Templates"]
    save_config(cfg)

    note = vault / "study_notes/fluoxetine.md"
    res = archive_mod.archive_image(
        vault, load_config(), "study_notes",
        "study_notes/fluoxetine.md", "Z_attachments/7A27.png",
        sha256_bytes(note.read_bytes()))
    assert res["status"] == "shared", res                       # excluded ref still counts
    assert (vault / "Z_attachments/7A27.png").exists()          # original not moved


def test_restore_archive_refuses_when_note_edited(vault):
    # FIX #1: reverting an archive of a since-edited note must NOT half-revert
    # (which would delete the thumbnail the note still embeds → broken embed).
    from refactor import journal
    cfg, res = _archive_one(vault)
    thumb = vault / res["thumb_rel"]
    note = vault / "study_notes/fluoxetine.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nedited later\n", encoding="utf-8")

    man = journal.load(vault, cfg)
    op = [o for o in man["ops"] if o["kind"] == "archive_image"][0]
    r = journal.revert_op(vault, cfg, op)
    journal.save(vault, cfg, man)

    assert r["status"] == "skipped"                             # refused, atomic-or-nothing
    assert thumb.exists()                                       # thumbnail NOT deleted
    assert not (vault / "Z_attachments/7A27.png").exists()      # original NOT moved back
    assert "edited later" in note.read_text(encoding="utf-8")   # edits preserved
    assert journal.find_op(journal.load(vault, cfg), op["id"])["state"] != "reverted"


def test_restore_archive_refuses_foreign_file_at_original_path(vault):
    # FIX #2: restore must not clobber a different file the user re-created at the
    # archived image's original path.
    from refactor import journal
    cfg, _res = _archive_one(vault)
    foreign = _png_bytes((200, 100, 50))
    (vault / "Z_attachments/7A27.png").write_bytes(foreign)

    man = journal.load(vault, cfg)
    op = [o for o in man["ops"] if o["kind"] == "archive_image"][0]
    r = journal.revert_op(vault, cfg, op)
    journal.save(vault, cfg, man)

    assert r["status"] == "skipped"
    assert (vault / "Z_attachments/7A27.png").read_bytes() == foreign  # foreign untouched


def test_archive_aborts_and_rolls_back_if_note_changes_midway(vault):
    # FIX #6: if the note changes during the thumbnail/copy work, archive aborts
    # BEFORE the destructive write and rolls back the thumbnail + archive copy +
    # the just-appended manifest op, leaving the vault exactly as it was.
    from core.config import load_config
    from refactor import archive as archive_mod, journal
    from refactor.result import sha256_bytes

    note = vault / "study_notes/fluoxetine.md"
    cfg = load_config()
    ch = sha256_bytes(note.read_bytes())
    real_make = archive_mod.make_thumbnail

    def racing_make(data, max_side):
        # Simulate a concurrent editor touching the note mid-archive.
        note.write_text(note.read_text(encoding="utf-8") + "\nrace\n", encoding="utf-8")
        return real_make(data, max_side)

    with mock.patch.object(archive_mod, "make_thumbnail", side_effect=racing_make):
        res = archive_mod.archive_image(
            vault, cfg, "study_notes",
            "study_notes/fluoxetine.md", "Z_attachments/7A27.png", ch)

    assert res["status"] == "skipped" and "during archiving" in res["message"]
    assert (vault / "Z_attachments/7A27.png").exists()          # original NOT moved
    tdir = vault / "study_notes/_thumbs"
    assert not tdir.exists() or not any(tdir.iterdir())         # thumbnail rolled back
    assert not [o for o in journal.load(vault, cfg)["ops"]
                if o["kind"] == "archive_image"]                # op rolled back
