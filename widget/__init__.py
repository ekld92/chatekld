"""Widget snapshot subsystem (Lot 2, 2026-08-06).

Turns the audit's cached in-memory ``Inventory`` into a small, durable,
regenerable JSON snapshot (``BASE_DIR/widget.json``) that external desktop
widgets (SwiftBar / Übersicht — Lot 3) can read whether or not the app is
running. Written ONLY on scan completion (an ``audit_manager`` result
listener) and on the manual ``POST /api/widget/refresh`` — no scheduler, no
auto-scan; the audit's manual-only invariant is untouched.

``snapshot.py`` is the pure payload builder (injected inputs, deterministic
under a seed); ``store.py`` owns the two BASE_DIR files (snapshot + the
anti-repeat history sidecar). The Flask surface lives in
``api/routes/widget.py``.
"""
