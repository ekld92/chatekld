"""Flask application factory and process entry point.

This module is the app glue: :func:`create_app` builds the Flask app, runs the
one-time service/database initialisation, and registers every route blueprint.
The module-level ``app = create_app()`` is what PyWebView / WSGI imports.

``CONFIG_FILE`` and ``FEEDBACK_FILE`` are deliberately re-exported here (see the
``# noqa: F401`` on the import below) so the reset test can monkeypatch them at
``app.CONFIG_FILE`` / ``app.FEEDBACK_FILE`` — the ``/api/reset`` handler in
``api/routes/config.py`` resolves them via ``getattr(sys.modules["app"], ...)``
so a patch on this module takes effect there without an import-time binding.
"""
import os
import sys
import logging
import logging.handlers
from pathlib import Path
from flask import Blueprint, Flask, render_template

from core.config import load_config
# CONFIG_FILE and FEEDBACK_FILE are re-imported here intentionally: smoke_test
# patches them at `app.CONFIG_FILE` / `app.FEEDBACK_FILE` via mock.patch.
from core.constants import (  # noqa: F401
    BASE_DIR,
    CONFIG_FILE,
    DEFAULT_REQUEST_MAX_BYTES,
    FEEDBACK_FILE,
    LOG_FILE,
    PDF_LIMIT_SIZE_MB,
    UPLOAD_REQUEST_OVERHEAD_BYTES,
    CHAT_ATTACHMENT_LIMIT_SIZE_MB,
)
from core.database import init_db
from core.llm.usage import configure_default_usage_tracker, log_unpriced_curated_models
from api.security import (
    register_origin_guard,
    register_request_limits,
    register_security_headers,
)
from services.vision import vision_manager, glm_ocr_manager
from rag.vault import obsidian_manager

# API Routes (Blueprints)
from api.routes.status import status_bp
from api.routes.config import config_bp
from api.routes.paper import paper_bp
from api.routes.vault import vault_bp
from api.routes.online_ocr import online_ocr_bp
from api.routes.about import about_bp
from api.routes.usage import usage_bp
from api.routes.audit import audit_bp
from api.routes.accession import accession_bp
from api.routes.pending import pending_bp
from api.routes.deck import deck_bp
from api.routes.docgen import docgen_bp
from api.routes.history import history_bp
from api.routes.plainchat import plainchat_bp
from api.routes.chat_attachment import chat_attachment_bp
from api.routes.prompts import prompts_bp
from api.routes.open import open_bp
from api.routes.notesync import notesync_bp
from api.routes.tagging import tagging_bp
from api.routes.zotcreate import zotcreate_bp
from api.routes.zotrecord import zotrecord_bp
from api.routes.zotlink import zotlink_bp
from api.routes.library_workflow import library_workflow_bp
from api.routes.widget import widget_bp

def _load_optional_maintenance_blueprint() -> Blueprint | None:
    """Load the private Maintenance capability or prove it was stripped.

    The public artifact intentionally removes both the route module and the
    private ``maintenance`` package. Those two exact absences mean “capability
    unavailable.” A missing transitive dependency, a renamed symbol, or any
    other import-time defect is a broken private build and must fail boot; if
    swallowed, the UI merely hides Maintenance and turns a release regression
    into a false-success public-strip state.
    """
    try:
        from api.routes.maintenance import maintenance_bp as loaded_blueprint
    except ModuleNotFoundError as exc:
        if exc.name not in {"api.routes.maintenance", "maintenance"}:
            raise
        return None
    return loaded_blueprint


# Maintenance is private-only (STRIP STEP 4). Its exact absence yields 404s,
# which is the capability probe used to keep the public tab hidden.
maintenance_bp = _load_optional_maintenance_blueprint()

logger = logging.getLogger(__name__)

def create_app():
    """Build, initialise, and return the Flask app.

    Initialisation order matters and is intentional:

    1. Resolve the template/static folders. In a frozen PyInstaller build the
       assets live under ``sys._MEIPASS`` (the unpacked bundle dir), not next to
       this source file, so Flask is pointed at those absolute paths.
    2. ``init_db()`` creates the SQLite schema; ``configure_default_usage_tracker``
       wires the token/USD usage log under ``BASE_DIR``.
    3. Hydrate the long-lived service singletons (OCR/vision managers, the
       Obsidian vault manager) from the saved ``config.json`` so the configured
       models/vault are active before the first request — restoring the vault
       path here also lets prewarm start without waiting for the UI to POST.
    4. Register every route blueprint, then the bare ``/`` index route.

    The audit blueprint is registered like any other but never auto-scans:
    nothing here calls ``audit_manager.start_scan()`` (pinned by
    ``test_audit.py::TestFlaskAppBootDoesNotScan``).
    """
    # Handle PyInstaller bundle paths
    if getattr(sys, 'frozen', False):
        template_folder = os.path.join(sys._MEIPASS, 'templates')
        static_folder = os.path.join(sys._MEIPASS, 'static')
        app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
    else:
        app = Flask(__name__)

    # Enforced CSP and response hardening. Hashes are derived once from the
    # trusted app-shell source so the existing exact inline handlers work
    # without granting arbitrary inline-script execution.
    template_path = Path(app.template_folder or "templates") / "index.html"
    if not template_path.is_absolute():
        template_path = Path(app.root_path) / template_path
    register_security_headers(
        app, template_path.read_text(encoding="utf-8"))

    register_request_limits(
        app,
        default_max_bytes=DEFAULT_REQUEST_MAX_BYTES,
        upload_max_bytes=(
            PDF_LIMIT_SIZE_MB * 1024 * 1024 + UPLOAD_REQUEST_OVERHEAD_BYTES
        ),
        # Plain Chat attachments get their OWN, much smaller envelope — see
        # register_request_limits. Sharing /api/upload's would mean buffering up
        # to 500 MB for a route that refuses anything over 64 MB.
        extra_upload_limits={
            "/api/chat-attachment": (
                CHAT_ATTACHMENT_LIMIT_SIZE_MB * 1024 * 1024
                + UPLOAD_REQUEST_OVERHEAD_BYTES
            ),
        },
    )

    # Item 4.1 — single before_request hook replaces the 67 inline
    # origin_is_local() checks that lived in every route handler.
    # New blueprints are protected automatically; the hook is the single
    # location to patch in tests (api.security.origin_is_local).
    register_origin_guard(app)
    from api.audit_context import register_context_guard
    register_context_guard(app)

    # --- Initialization ---
    init_db()
    configure_default_usage_tracker(BASE_DIR)
    # Track 5.5 startup advisory: name any curated model whose usage would
    # silently cost out at $0 (missing PRICING_TABLE entry). Warning only —
    # never blocks boot.
    log_unpriced_curated_models()

    cfg = load_config()
    
    # Initialize services with saved preferences
    ocr_model = cfg.get("ocr_model", "glm-ocr:latest")
    if ocr_model:
        glm_ocr_manager.set_model(ocr_model)
    glm_ocr_manager.set_provider(cfg.get("ocr_provider", "ollama"))
        
    vision_model = cfg.get("vision_model", "qwen3-vl:4b")
    if vision_model:
        vision_manager.set_model(vision_model)
    vision_manager.set_provider(cfg.get("vision_provider", "ollama"))
        
    vault_path = cfg.get("obsidian_vault_path", "")
    if vault_path:
        obsidian_manager.restore_vault_path(vault_path)

    # --- Register Blueprints ---
    app.register_blueprint(status_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(paper_bp)
    app.register_blueprint(vault_bp)
    app.register_blueprint(online_ocr_bp)
    app.register_blueprint(about_bp)
    app.register_blueprint(usage_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(accession_bp)
    app.register_blueprint(pending_bp)
    app.register_blueprint(deck_bp)
    app.register_blueprint(docgen_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(plainchat_bp)
    app.register_blueprint(chat_attachment_bp)
    app.register_blueprint(prompts_bp)
    app.register_blueprint(open_bp)
    app.register_blueprint(tagging_bp)
    app.register_blueprint(notesync_bp)
    app.register_blueprint(zotcreate_bp)
    app.register_blueprint(zotrecord_bp)
    app.register_blueprint(zotlink_bp)
    app.register_blueprint(library_workflow_bp)
    app.register_blueprint(widget_bp)
    if maintenance_bp is not None:  # private-only build (see the import above)
        app.register_blueprint(maintenance_bp)

    # --- Main Index Route ---
    @app.route("/")
    def index():
        return render_template("index.html")

    return app

app = create_app()

if __name__ == "__main__":
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            # Rotate so the dev log can't grow unbounded (mirrors launch.py).
            logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=10_000_000, backupCount=3),
            logging.StreamHandler(sys.stdout)
        ]
    )
    # Write-time key redaction on every handler (mirrors launch.py — see the
    # RedactingFilter docstring: httpx logs Gemini URLs with the key inline).
    from core.llm.redact import RedactingFilter
    for _h in logging.getLogger().handlers:
        _h.addFilter(RedactingFilter())
    # Pin the loopback interface explicitly. Flask already defaults to
    # 127.0.0.1, but the whole local-origin CSRF model (api/security.py)
    # assumes the server is unreachable off loopback — make it impossible
    # for a future accidental host="0.0.0.0" to silently expose this
    # dev entry point on the local network (mirrors launch.py:run_flask).
    app.run(host="127.0.0.1", port=5000, debug=False)
