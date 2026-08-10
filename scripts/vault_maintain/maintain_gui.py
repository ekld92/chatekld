#!/usr/bin/env python3
"""Tk control panel for vault maintenance.

A thin front end over `maintain.sh` — it never reimplements a step. It asks the
script for its step registry (`--list --porcelain`), runs the selected subset
(`--only=… --porcelain`), and renders the machine-readable markers the script
emits as per-step status while streaming the rest of the output into a log
pane. One source of truth for what a step *is*; the GUI only decides which ones
run and shows how they went.

Launched by `maintain.sh --gui` (and by Vault Sync.app). Pure stdlib: tkinter
ships with every python3 on this machine.

Trust boundary (2026-07-29 audit): `maintain.sh` lives INSIDE the resolved
vault — an iCloud-synced, plugin-writable tree — and this panel executes it
with the launcher's full augmented PATH. `_check_script_trust` therefore
refuses a script that is not owned by the current user or is group/world-
writable, which stops the shared-folder, unpacked-archive, and
wrong-cwd-vault cases (vault_paths adopts ANY ancestor of cwd holding
`.obsidian/`). Residual risk, documented not solved: iCloud sync writes as
the user, so a script replaced from another device passes both checks —
the ownership test bounds the attacker set to "can already write as you",
it cannot shrink it further.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

POLL_MS = 80
# Keeps the Text widget bounded: a full LaTeX sweep emits thousands of lines
# and tk's Text degrades (memory + redraw cost) unbounded. 5000 comfortably
# holds the largest observed sweep's tail while trimming pathological runs.
MAX_LOG_LINES = 5000
_SUMMARY_RE = re.compile(r"^\d+ passed, (\d+) failed")

# Marker prefixes that are protocol, not log content (see maintain.sh
# --porcelain). An EXACT prefix set, not "any line starting with ::" — a
# legitimate output line that happens to start with :: must reach the log
# verbatim rather than vanish without trace (2026-07-29 audit).
_PROTOCOL_PREFIXES = ("::step::", "::start::", "::end::")


def _check_script_trust(script: Path) -> str:
    """Refuse a maintain.sh the current user does not own or that others can
    write. Returns "" when trusted, else the human-readable refusal.

    See the module docstring's trust-boundary note for what this does and
    does not protect against. `st_mode & 0o022` is group+other write bits.
    """
    try:
        st = script.stat()
    except OSError as exc:
        return f"Cannot stat {script}: {exc}"
    if st.st_uid != os.getuid():
        return (f"{script} is not owned by the current user (uid {st.st_uid} "
                f"vs {os.getuid()}). Refusing to execute a script someone "
                f"else placed in the vault.")
    if st.st_mode & 0o022:
        return (f"{script} is group/world-writable (mode {st.st_mode & 0o777:o}). "
                f"Anyone who can write it gets code execution the next time "
                f"this panel runs — `chmod go-w` it first.")
    return ""

STATUS_STYLE = {
    "pending": ("○", "muted"),
    "running": ("◐", "running"),
    "ok": ("✅", "ok"),
    "fail": ("❌", "fail"),
}


def is_dark_mode() -> bool:
    try:
        out = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return "dark" in out.stdout.lower()
    except Exception:
        return False


def mono_font() -> str:
    import tkinter.font as tkfont

    available = set(tkfont.families())
    for name in ("SF Mono", "Menlo", "Monaco", "Courier New"):
        if name in available:
            return name
    return "TkFixedFont"


class Step:
    def __init__(self, sid: str, label: str, default_on: bool, hint: str):
        self.id = sid
        self.label = label
        self.default_on = default_on
        self.hint = hint
        self.var = tk.BooleanVar(value=default_on)
        self.status_lbl: ttk.Label | None = None
        self.hint_lbl: ttk.Label | None = None


class MaintenanceGUI:
    def __init__(self, root: tk.Tk, maintain_sh: Path):
        self.root = root
        self.maintain = maintain_sh
        self.vault = maintain_sh.parent
        self.proc: subprocess.Popen | None = None
        self.queue: queue.Queue = queue.Queue()
        self.steps: list[Step] = []
        self.dark = is_dark_mode()

        root.title("Vault Maintenance")
        root.minsize(720, 520)
        root.geometry("900x680")

        self._build_widgets()
        trust_err = _check_script_trust(self.maintain)
        if trust_err:
            self._fatal(trust_err)
        else:
            self._load_steps()
        self.root.after(POLL_MS, self._drain)

    # -- layout -----------------------------------------------------------

    def _build_widgets(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Label(
            outer,
            text=f"Vault: {self.vault}",
            foreground="#888",
        )
        header.pack(anchor="w", pady=(0, 8))

        self.steps_frame = ttk.LabelFrame(outer, text="Checks", padding=10)
        self.steps_frame.pack(fill="x")

        # LaTeX sub-options live in their own indented block, enabled only when
        # that step is selected.
        self.latex_frame = ttk.Frame(self.steps_frame)
        self.filter_var = tk.StringVar()
        self.jobs_var = tk.StringVar(value="4")
        self.nopandoc_var = tk.BooleanVar(value=False)

        ttk.Label(self.latex_frame, text="filter").grid(row=0, column=0, sticky="w", padx=(24, 4))
        self.filter_entry = ttk.Entry(self.latex_frame, textvariable=self.filter_var, width=22)
        self.filter_entry.grid(row=0, column=1, sticky="w")
        ttk.Label(self.latex_frame, text="jobs").grid(row=0, column=2, sticky="w", padx=(12, 4))
        self.jobs_spin = ttk.Spinbox(
            self.latex_frame, from_=1, to=16, width=4, textvariable=self.jobs_var
        )
        self.jobs_spin.grid(row=0, column=3, sticky="w")
        self.nopandoc_check = ttk.Checkbutton(
            self.latex_frame, text="skip PPTX/DOCX", variable=self.nopandoc_var
        )
        self.nopandoc_check.grid(row=0, column=4, sticky="w", padx=(12, 0))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(10, 8))
        self.run_btn = ttk.Button(controls, text="Run", command=self.run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(controls, textvariable=self.status_var).pack(side="right")

        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", expand=True)

        bg, fg = ("#1e1e1e", "#e6e6e6") if self.dark else ("#ffffff", "#1a1a1a")
        self.log = tk.Text(
            log_frame,
            wrap="none",
            height=18,
            font=(mono_font(), 11),
            background=bg,
            foreground=fg,
            insertbackground=fg,
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#999" if not self.dark else "#444",
        )
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        xscroll = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log.tag_configure("err", foreground="#ff6b6b" if self.dark else "#b3261e")
        self.log.tag_configure("warn", foreground="#d9a441")
        self.log.tag_configure("ok", foreground="#3fa34d")
        self.log.tag_configure("head", foreground="#6c8cff")
        self.log.configure(state="disabled")

    # -- step registry ----------------------------------------------------

    def _load_steps(self):
        """Ask maintain.sh for its steps rather than duplicating the list."""
        try:
            out = subprocess.run(
                [str(self.maintain), "--list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.vault),
            )
        except Exception as exc:
            self._fatal(f"Could not run {self.maintain}:\n{exc}")
            return

        if out.returncode != 0:
            self._fatal(
                f"{self.maintain} --list failed (exit {out.returncode}):\n"
                f"{out.stderr.strip() or out.stdout.strip()}"
            )
            return

        for line in out.stdout.splitlines():
            if not line.startswith("::step::"):
                continue
            # "::step::id::label::on::hint" -> ['step', 'id', 'label', 'on', 'hint']
            # (the leading empty field is the text before the first separator)
            parts = line.split("::")[1:]
            if len(parts) < 5:
                continue
            _, sid, label, default, hint = parts[:5]
            self.steps.append(Step(sid, label, default == "on", hint))

        if not self.steps:
            self._fatal("maintain.sh reported no steps.")
            return

        # One shared grid (not a frame per row) so the hint and status columns
        # line up regardless of how long each step's label is.
        for row, step in enumerate(self.steps):
            ttk.Checkbutton(
                self.steps_frame,
                text=step.label,
                variable=step.var,
                command=self._sync_enabled,
            ).grid(row=row * 2, column=0, sticky="w", pady=1)
            step.hint_lbl = ttk.Label(self.steps_frame, text=step.hint, foreground="#888")
            step.hint_lbl.grid(row=row * 2, column=1, sticky="w", padx=(14, 0))
            step.status_lbl = ttk.Label(self.steps_frame, text="○", foreground="#888", width=2)
            step.status_lbl.grid(row=row * 2, column=2, sticky="e", padx=(18, 0))

            if step.id == "latex":
                self.latex_frame.grid(
                    row=row * 2 + 1, column=0, columnspan=3, sticky="w", pady=(0, 4)
                )

        self.steps_frame.columnconfigure(1, weight=1)

        self._sync_enabled()
        self._count_latex_docs()

    def _count_latex_docs(self):
        """Fill in the real document count in the background (a suite scan)."""
        latex = next((s for s in self.steps if s.id == "latex"), None)
        if latex is None:
            return

        def work():
            try:
                out = subprocess.run(
                    [str(self.maintain), "--latex-list"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(self.vault),
                )
                count = sum(
                    1 for line in out.stdout.splitlines() if line.strip().endswith(".tex")
                )
            except Exception:
                return
            if count:
                self.queue.put(("latexcount", count))

        threading.Thread(target=work, daemon=True).start()

    def _sync_enabled(self):
        latex = next((s for s in self.steps if s.id == "latex"), None)
        state = "normal" if (latex and latex.var.get()) else "disabled"
        for widget in (self.filter_entry, self.jobs_spin, self.nopandoc_check):
            widget.configure(state=state)
        any_selected = any(s.var.get() for s in self.steps)
        if self.proc is None:
            self.run_btn.configure(state="normal" if any_selected else "disabled")

    # -- running ----------------------------------------------------------

    def _build_command(self) -> list[str] | None:
        selected = [s.id for s in self.steps if s.var.get()]
        if not selected:
            return None
        cmd = [str(self.maintain), "--porcelain", f"--only={','.join(selected)}"]
        if "latex" in selected:
            pattern = self.filter_var.get().strip()
            if pattern:
                cmd.append(f"--filter={pattern}")
            jobs = self.jobs_var.get().strip()
            if jobs.isdigit() and int(jobs) >= 1:
                cmd.append(f"--jobs={jobs}")
            if self.nopandoc_var.get():
                cmd.append("--no-pandoc")
        return cmd

    def run(self):
        if self.proc is not None:
            return
        # Re-checked per run, not only at launch: the panel can sit open for
        # hours and the script lives in a synced tree — a cheap stat per Run
        # keeps the trust decision current.
        trust_err = _check_script_trust(self.maintain)
        if trust_err:
            self._append(f"[ERROR] {trust_err}\n", "err")
            return
        cmd = self._build_command()
        if cmd is None:
            return

        self._clear_log()
        for step in self.steps:
            self._set_status(step, "pending" if step.var.get() else "skip")
        self._append(f"$ {' '.join(cmd)}\n\n", "head")

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(self.vault),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,  # own process group, so Stop kills children too
            )
        except Exception as exc:
            self._append(f"[ERROR] could not start maintain.sh: {exc}\n", "err")
            self.proc = None
            return

        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Running…")

        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc: subprocess.Popen):
        assert proc.stdout is not None
        for line in proc.stdout:
            self.queue.put(("line", line.rstrip("\n")))
        proc.wait()
        self.queue.put(("done", proc.returncode))

    def stop(self):
        # Capture the process object NOW (2026-07-29 audit 6): the escalation
        # timer below fires 4 s later, and `self.proc` may by then reference a
        # NEW run — the old code re-read the attribute in _force_kill and
        # SIGKILLed whatever run was current, i.e. one the user just started.
        # Everything downstream operates only on this captured handle.
        proc = self.proc
        if proc is None:
            return
        # Idempotent: disabling the button stops repeat clicks from stacking
        # one escalation timer per click.
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping…")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        # 4 s before SIGKILL: latexmk mid-build ignores TERM briefly (it
        # forwards it to its TeX child and waits) — long enough for a clean
        # exit, short enough that the panel doesn't look hung.
        self.root.after(4000, lambda: self._force_kill(proc))

    def _force_kill(self, proc: subprocess.Popen):
        # Operates ONLY on the run whose Stop click scheduled it — never on
        # self.proc, which may already be a newer run (see stop()).
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass

    # -- output -----------------------------------------------------------

    def _drain(self):
        # The reschedule lives in a `finally` (2026-07-29 audit 12): this is
        # the ONLY place the poll loop re-arms itself, so any exception
        # escaping an item handler used to kill the loop permanently — the
        # subprocess kept running while the panel silently froze. One bad
        # item now logs and the loop survives.
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                try:
                    if kind == "line":
                        self._handle_line(payload)
                    elif kind == "done":
                        self._finish(payload)
                    elif kind == "latexcount":
                        self._set_latex_count(payload)
                except Exception as exc:  # noqa: BLE001 — one item must not kill the loop
                    try:
                        self._append(f"[ERROR] GUI failed to render an event: {exc}\n", "err")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        finally:
            self.root.after(POLL_MS, self._drain)

    def _handle_line(self, line: str):
        # Only the EXACT marker prefixes are protocol (2026-07-29 audit): the
        # old `startswith("::")` swallowed any output line that happened to
        # start with :: — it vanished from the log with no trace.
        if line.startswith(_PROTOCOL_PREFIXES):
            parts = line.split("::")[1:]  # see _load_steps for the field layout
            kind = parts[0] if parts else ""
            if kind == "start" and len(parts) >= 2:
                self._set_status_by_id(parts[1], "running")
            elif kind == "end" and len(parts) >= 3:
                self._set_status_by_id(parts[1], "ok" if parts[2] == "ok" else "fail")
            return  # markers are protocol, not log content

        # Tag classification is FIRST-MATCH by severity: the summary regex
        # outranks everything (a clean summary containing the word "failed"
        # must stay green), then error > warning > success > heading — a line
        # carrying both an error and a success marker is an error.
        stripped = line.strip()
        summary = _SUMMARY_RE.match(stripped)
        if summary:
            # "N passed, M failed, of T step(s)." — green only when M is 0.
            # A substring test for "failed" painted every clean summary red.
            tag = "ok" if summary.group(1) == "0" else "err"
        elif "[ERROR]" in line or "-> FAILED" in line or "❌" in line or stripped.startswith(
            "Failed:"
        ):
            tag = "err"
        elif "[WARNING]" in line or "⚠" in line:
            tag = "warn"
        elif "-> OK" in line or "🎉" in line or "All checks passed" in line:
            tag = "ok"
        elif stripped.startswith("===") or stripped.startswith("["):
            tag = "head"
        else:
            tag = ""
        self._append(line + "\n", tag)

    def _finish(self, returncode: int):
        self.proc = None
        self.stop_btn.configure(state="disabled")
        self._sync_enabled()
        if returncode == 0:
            self.status_var.set("Finished — all checks passed")
        elif returncode == 2:
            self.status_var.set("Setup error (exit 2)")
        elif returncode < 0:
            # Popen reports death-by-signal as a NEGATIVE returncode (-SIGTERM
            # after Stop). The old test also treated `> 128` as "Stopped" —
            # that is the SHELL's signal convention, which Popen never uses,
            # so it silently reclassified a real exit code ≥ 129 from a step
            # as a user-initiated stop (2026-07-29 audit).
            self.status_var.set("Stopped")
        else:
            self.status_var.set(f"Finished with failures (exit {returncode})")
        # Any step still marked running was interrupted, not passed.
        for step in self.steps:
            if step.status_lbl is not None and step.status_lbl.cget("text") == "◐":
                self._set_status(step, "fail")
        self.root.bell()

    def _set_latex_count(self, count: int):
        for step in self.steps:
            if step.id == "latex" and step.hint_lbl is not None:
                step.hint_lbl.configure(text=f"{count} documents · SLOW")

    def _set_status_by_id(self, sid: str, status: str):
        for step in self.steps:
            if step.id == sid:
                self._set_status(step, status)

    def _set_status(self, step: Step, status: str):
        if step.status_lbl is None:
            return
        if status == "skip":
            step.status_lbl.configure(text="", foreground="#888")
            return
        glyph, _ = STATUS_STYLE.get(status, ("○", "muted"))
        colour = {
            "pending": "#888",
            "running": "#6c8cff",
            "ok": "#3fa34d",
            "fail": "#b3261e",
        }.get(status, "#888")
        step.status_lbl.configure(text=glyph, foreground=colour)

    def _append(self, text: str, tag: str = ""):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        # Keep the pane bounded: a full LaTeX sweep is thousands of lines.
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _fatal(self, message: str):
        self._append(message + "\n", "err")
        self.status_var.set("Cannot start")
        self.run_btn.configure(state="disabled")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--maintain",
        help="path to maintain.sh (default: resolved from ChatEKLD config / the vault)",
    )
    args = parser.parse_args(argv)

    if args.maintain:
        maintain = Path(args.maintain).expanduser().resolve()
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from vault_paths import VaultPathError, resolve_vault

        try:
            maintain = resolve_vault().path / "maintain.sh"
        except VaultPathError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2

    if not maintain.is_file():
        print(f"[ERROR] maintain.sh not found at {maintain}", file=sys.stderr)
        return 2

    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.0)
    except tk.TclError:
        pass
    MaintenanceGUI(root, maintain)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
