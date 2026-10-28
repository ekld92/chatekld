"""The three UNREGENERABLE decision sidecars fsync their directory entry.

DEFECT PINNED (performance plan, Phase 5): `mapping.json`, `dup_decisions.json`
and `pdf_wanted.json` fsync the FILE before the rename — so the content is
durable — but not the parent directory, so the entry that names the new file
may not be. A power loss right after the rename can leave the old file, or no
file, despite a successful save. The app-wide policy omits parent fsync for
REGENERABLE files on purpose; these three are the place it costs something.

Counted at os.fsync — the level both the pre- and post-change code reach: a
directory fd is a second fsync on a descriptor that is not the temp file's.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from audit.engine import bridge, dup_decisions, pdf_wanted


def _fsync_targets(monkeypatch):
    """Record whether each fsync'd fd is a directory."""
    seen = []
    real_fsync = os.fsync

    def _counting(fd):
        try:
            seen.append(os.fstat(fd).st_mode)
        except OSError:
            seen.append(0)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _counting)
    return seen


def _dir_fsyncs(seen):
    import stat
    return [m for m in seen if stat.S_ISDIR(m)]


def test_mapping_json_fsyncs_its_directory(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Z_attachments").mkdir(parents=True)
    pdf = vault / "Z_attachments" / "a_2020.pdf"
    pdf.write_bytes(b"%PDF")
    mapping = tmp_path / "mapping.json"
    seen = _fsync_targets(monkeypatch)
    bridge.add_match(mapping, pdf, "a2020", vault)
    assert _dir_fsyncs(seen), "mapping.json's directory entry was never fsynced"


def test_dup_decisions_fsyncs_its_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(dup_decisions, "decisions_path",
                        lambda: tmp_path / "dup_decisions.json")
    seen = _fsync_targets(monkeypatch)
    dup_decisions.add_dismissed("pair-1")
    assert _dir_fsyncs(seen), "dup_decisions.json's directory entry was never fsynced"


def test_pdf_wanted_fsyncs_its_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_wanted, "decisions_path",
                        lambda: tmp_path / "pdf_wanted.json")
    seen = _fsync_targets(monkeypatch)
    pdf_wanted.add_dismissed(["smith2020"])
    assert _dir_fsyncs(seen), "pdf_wanted.json's directory entry was never fsynced"


def test_an_unfsyncable_directory_never_fails_the_write(tmp_path, monkeypatch):
    """Best-effort: the write has already landed by then."""
    monkeypatch.setattr(dup_decisions, "decisions_path",
                        lambda: tmp_path / "dup_decisions.json")
    real_open = os.open

    def _refuse_dir(path, flags, *a, **kw):
        if flags == os.O_RDONLY and Path(path).is_dir():
            raise OSError("no directory fds here")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", _refuse_dir)
    assert dup_decisions.add_dismissed("pair-2") is True
    assert "pair-2" in dup_decisions.load_dismissed()


@pytest.mark.parametrize("mod", [dup_decisions, pdf_wanted])
def test_the_content_is_still_written(tmp_path, monkeypatch, mod):
    monkeypatch.setattr(mod, "decisions_path", lambda: tmp_path / "d.json")
    if mod is pdf_wanted:
        mod.add_dismissed(["k1"])
    else:
        mod.add_dismissed("k1")
    assert "k1" in mod.load_dismissed()
