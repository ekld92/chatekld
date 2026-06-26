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
from flask import Flask, render_template

from core.config import load_config
# CONFIG_FILE and FEEDBACK_FILE are re-imported here intentionally: smoke_test
# patches them at `app.CONFIG_FILE` / `app.FEEDBACK_FILE` via mock.patch.
from core.constants import BASE_DIR, CONFIG_FILE, FEEDBACK_FILE  # noqa: F401
from core.database import init_db
from core.llm.usage import configure_default_usage_tracker
from services.vision import vision_manager, glm_ocr_manager
from rag.vault import obsidian_manager

# API Routes (Blueprints)
from api.routes.status import status_bp
from api.routes.config import config_bp
from api.routes.paper import paper_bp
from api.routes.vault import vault_bp
from api.routes.about import about_bp
from api.routes.usage import usage_bp
from api.routes.audit import audit_bp
from api.routes.deck import deck_bp
from api.routes.refactor import refactor_bp
from api.routes.plainchat import plainchat_bp

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

    # --- Initialization ---
    init_db()
    configure_default_usage_tracker(BASE_DIR)

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
    app.register_blueprint(about_bp)
    app.register_blueprint(usage_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(deck_bp)
    app.register_blueprint(refactor_bp)
    app.register_blueprint(plainchat_bp)

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
            logging.FileHandler("chatekld.log"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    # Pin the loopback interface explicitly. Flask already defaults to
    # 127.0.0.1, but the whole local-origin CSRF model (api/security.py)
    # assumes the server is unreachable off loopback — make it impossible
    # for a future accidental host="0.0.0.0" to silently expose this
    # dev entry point on the local network (mirrors launch.py:run_flask).
    app.run(host="127.0.0.1", port=5000, debug=False)
