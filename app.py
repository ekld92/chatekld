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

logger = logging.getLogger(__name__)

def create_app():
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
    app.run(port=5000, debug=False)
