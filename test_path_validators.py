"""Parity ratchet over the hand-rolled path validators (P8.2, 2026-07-30).

`core/paths.py::resolve_under_root` is the single chokepoint for path inputs
that resolve UNDER a known root. The validators here cannot use it — they take
user-chosen ABSOLUTE locations with no under-root anchor, or a vault-relative
string with no filesystem access — so each hand-rolls its own NUL/newline +
realpath + deny-prefix guards. Root `CLAUDE.md` warns that "a future hardening
of `resolve_under_root` must be mirrored into those hand-rolled validators",
and until now nothing enforced that: each had only its own per-function test,
so a guard could be dropped from ONE of six without a red suite.

This file pins the SHARED contract across all of them at once:

* every validator rejects embedded NUL / LF / CR (the control-character
  injection class) even when the rest of the path is otherwise perfectly valid;
* every validator rejects non-string, empty and absurdly-long input;
* the absolute-path family rejects relative input, and the vault-relative one
  rejects absolute input and `..` traversal.

Each group carries a POSITIVE control (a real temp file/dir that IS accepted),
so a rejection assertion cannot be satisfied by a validator that simply returns
None for everything.

Adding a seventh validator of this shape? Add it here.
"""
import os
import tempfile
import unittest

from api.routes import _latexdoc
from api.routes import audit as audit_routes
from api.routes import deck as deck_routes

# Injected into an otherwise-valid path. A validator that lost its explicit
# control-character guard must still refuse these (whether by its own check or
# by the OS layer raising) — the CONTRACT is "returns None", not "which line
# rejected it".
_CONTROL_CHARS = ("\x00", "\n", "\r")


class _PathValidatorParityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, self.tmp)
        self.tex = os.path.join(self.tmp, "deck.tex")
        with open(self.tex, "w", encoding="utf-8") as fh:
            fh.write("\\documentclass{beamer}\\begin{document}\\end{document}")


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class TestAbsolutePathValidatorParity(_PathValidatorParityBase):
    """The absolute-location family: template / out-dir / existing deck (deck +
    docgen twin). Different accept rules, one shared rejection contract."""

    def _validators_with_valid_input(self):
        """(name, callable, a value it MUST accept) for each abs-path validator."""
        return [
            ("deck._resolve_template_path",
             deck_routes._resolve_template_path, self.tex),
            ("deck._resolve_out_dir",
             deck_routes._resolve_out_dir, self.tmp),
            ("deck._resolve_existing_deck",
             deck_routes._resolve_existing_deck, self.tex),
            ("_latexdoc.resolve_template_path",
             _latexdoc.resolve_template_path, self.tex),
            # The docgen twins of out-dir / existing-deck. Omitting them is how
            # the P8.2 relative-path fix reached deck._resolve_out_dir alone
            # while its three siblings kept CWD-anchoring a relative input.
            ("_latexdoc.resolve_out_dir",
             _latexdoc.resolve_out_dir, self.tmp),
            ("_latexdoc.resolve_existing_doc",
             _latexdoc.resolve_existing_doc, self.tex),
        ]

    def test_positive_controls_are_accepted(self):
        """Guards the whole file: without this, every rejection below could be
        satisfied by a validator that returns None unconditionally."""
        for name, fn, good in self._validators_with_valid_input():
            with self.subTest(validator=name):
                self.assertIsNotNone(fn(good), f"{name} rejected a valid path")

    def test_embedded_control_characters_are_rejected_by_every_validator(self):
        """EMBEDDED control chars only.

        A LEADING/TRAILING one is legitimately stripped by
        `coerce_string_max_len` before any validator sees it, which yields the
        clean path — safe, and not what the guard is for. The injection case is
        a control character in the MIDDLE ("/safe/dir\n/etc/passwd"), which
        every one of these must refuse.
        """
        for name, fn, good in self._validators_with_valid_input():
            for ch in _CONTROL_CHARS:
                candidate = good[:4] + ch + good[4:]
                with self.subTest(validator=name, char=repr(ch)):
                    self.assertIsNone(
                        fn(candidate),
                        f"{name} accepted a path containing embedded {ch!r}")

    def test_empty_and_non_string_input_is_rejected(self):
        for name, fn, _good in self._validators_with_valid_input():
            for bad in ("", "   ", None, 123, [], {}, True):
                with self.subTest(validator=name, value=repr(bad)):
                    self.assertIsNone(fn(bad), f"{name} accepted {bad!r}")

    def test_absurdly_long_input_is_rejected(self):
        for name, fn, _good in self._validators_with_valid_input():
            with self.subTest(validator=name):
                self.assertIsNone(fn("/" + "a" * 100_000))

    def test_relative_input_is_rejected(self):
        """These take user-chosen ABSOLUTE locations; a relative path would
        silently anchor to the server's CWD at use time.

        The CWD is moved into the fixture dir for the duration, so the relative
        names below RESOLVE TO REAL FILES. Without that this test proved almost
        nothing: an existence-checking validator (`isfile`/`isdir`) rejects a
        relative path only because nothing of that name happens to sit in the
        CWD, not because it enforces the rule — which is exactly how the P8.2
        fix landed on one validator while its siblings kept CWD-anchoring.
        """
        cwd = os.getcwd()
        os.chdir(self.tmp)                     # `deck.tex` and `.` exist here
        self.addCleanup(os.chdir, cwd)
        for name, fn, _good in self._validators_with_valid_input():
            for bad in (os.path.basename(self.tex), f"./{os.path.basename(self.tex)}",
                        "sub/deck.tex", "..", ".", ""):
                with self.subTest(validator=name, value=bad):
                    self.assertIsNone(fn(bad), f"{name} accepted relative {bad!r}")

    def test_system_locations_are_rejected_by_the_writeback_validators(self):
        """`_resolve_out_dir` / `_resolve_existing_deck` can be written back to,
        so they additionally refuse `_DENY_PREFIXES` roots."""
        for name, fn in (("deck._resolve_out_dir", deck_routes._resolve_out_dir),
                         ("deck._resolve_existing_deck",
                          deck_routes._resolve_existing_deck),
                         ("_latexdoc.resolve_out_dir",
                          _latexdoc.resolve_out_dir),
                         ("_latexdoc.resolve_existing_doc",
                          _latexdoc.resolve_existing_doc)):
            for bad in ("/System", "/usr/bin", "/bin", "/etc"):
                with self.subTest(validator=name, value=bad):
                    self.assertIsNone(fn(bad), f"{name} accepted {bad!r}")


class TestAuditPathValidatorParity(_PathValidatorParityBase):
    """The audit pair: a vault-RELATIVE rel and an absolute settings path."""

    def test_rel_positive_control(self):
        self.assertIsNotNone(audit_routes._normalise_rel_path("biblio/paper.pdf"))
        # B3: accented / apostrophed vault paths must keep working (denylist,
        # not an ASCII allowlist).
        self.assertIsNotNone(
            audit_routes._normalise_rel_path("biblio/étude_dépression (2).pdf"))

    def test_rel_rejects_control_chars_traversal_and_absolute(self):
        fn = audit_routes._normalise_rel_path
        for ch in _CONTROL_CHARS:
            self.assertIsNone(fn(f"biblio/pa{ch}per.pdf"), repr(ch))  # embedded
        for bad in ("../secrets.pdf", "biblio/../../etc/passwd", "..",
                    "/abs/paper.pdf", "biblio\\paper.pdf",
                    "", "   ", None, 123, []):
            with self.subTest(value=repr(bad)):
                self.assertIsNone(fn(bad), f"accepted {bad!r}")

    def test_abs_positive_control_and_rejections(self):
        fn = audit_routes._normalise_abs_path
        self.assertIsNotNone(fn(self.tmp))
        for ch in _CONTROL_CHARS:
            # Embedded (see the abs-family note on stripped edges).
            self.assertIsNone(fn(self.tmp[:4] + ch + self.tmp[4:]), repr(ch))
        for bad in ("relative/path", "", "   ", None, 123, []):
            with self.subTest(value=repr(bad)):
                self.assertIsNone(fn(bad), f"accepted {bad!r}")


if __name__ == "__main__":
    unittest.main()
