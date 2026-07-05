"""Unit tests for deckgen/checkpoint.py (pure: tmp dir, no app, no server)."""
import os

from deckgen import checkpoint
from deckgen.assemble import SectionOutput
from deckgen.outline import Section


def _key(**over):
    base = dict(
        topic="Heart failure", instructions="cover staging", template_tex="\\documentclass{beamer}",
        provider="lm_studio", model="qwen", max_sections=4, audience="residents",
        citations_enabled=False, slug="hf", out_dir="/decks",
    )
    base.update(over)
    return checkpoint.compute_job_key(**base)


def test_job_key_is_stable_and_input_sensitive():
    assert _key() == _key()  # deterministic
    assert _key(topic="Other") != _key()
    assert _key(max_sections=5) != _key()
    assert _key(citations_enabled=True) != _key()
    assert _key(out_dir="/elsewhere") != _key()


def test_manifest_roundtrip(tmp_path):
    d = str(tmp_path)
    jk = _key()
    sections = [Section(title="Intro", points=["a", "b"]), Section(title="Methods", points=[])]
    m = checkpoint.new_manifest(job_key=jk, topic="T", slug="hf", out_dir="/decks", sections=sections)
    assert checkpoint.completed_count(m) == 0

    checkpoint.set_section(m, 1, SectionOutput(title="Intro", body="\\section{Intro}", placeholder=False))
    checkpoint.save(d, m)

    loaded = checkpoint.load(d, jk)
    assert loaded is not None
    assert checkpoint.completed_count(loaded) == 1
    # Outline survives the roundtrip as Section objects.
    outline = checkpoint.outline_from_list(loaded["outline"])
    assert [s.title for s in outline] == ["Intro", "Methods"]
    assert outline[0].points == ["a", "b"]
    # The saved section is recovered verbatim.
    sec = checkpoint.get_section(loaded, 1)
    assert sec is not None and sec.body == "\\section{Intro}"
    assert checkpoint.get_section(loaded, 2) is None


def test_load_is_tolerant(tmp_path):
    d = str(tmp_path)
    assert checkpoint.load(d, "nope") is None  # missing

    jk = _key()
    # Corrupt JSON.
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{jk}.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert checkpoint.load(d, jk) is None

    # Wrong version.
    import json
    with open(os.path.join(d, f"{jk}.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 999, "job_key": jk}, fh)
    assert checkpoint.load(d, jk) is None


def test_delete_and_prune(tmp_path):
    d = str(tmp_path)
    jk = _key()
    m = checkpoint.new_manifest(job_key=jk, topic="T", slug="s", out_dir="/d", sections=[])
    checkpoint.save(d, m)
    assert checkpoint.load(d, jk) is not None
    checkpoint.delete(d, jk)
    assert checkpoint.load(d, jk) is None
    checkpoint.delete(d, jk)  # idempotent, no raise

    # Prune keeps only the newest max_keep files.
    for i in range(15):
        mi = checkpoint.new_manifest(job_key=f"k{i:02d}", topic="T", slug="s", out_dir="/d", sections=[])
        checkpoint.save(d, mi)
        os.utime(os.path.join(d, f"k{i:02d}.json"), (1000 + i, 1000 + i))  # ascending mtime
    checkpoint.prune(d, max_keep=10)
    remaining = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    assert len(remaining) == 10
    # The 5 oldest (k00..k04) were dropped; the 10 newest survive.
    assert remaining == [f"k{i:02d}.json" for i in range(5, 15)]
