import os
from flask import Flask
from flask_login import LoginManager

login_manager = LoginManager()


def create_app():
    app = Flask(__name__)

    # Secret key — must be overridden in production via SECRET_KEY env var
    app.config["SECRET_KEY"] = os.environ.get(
        "SECRET_KEY", "dev-secret-key-change-in-production"
    )
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

    # Flask-Login setup
    login_manager.init_app(app)
    login_manager.login_view         = "main.login"
    login_manager.login_message      = "Please sign in to access Loss Analysis."
    login_manager.login_message_category = "info"

    # Lazy import to avoid circular imports
    from .db import get_user_by_id, init_db, seed_admin_if_needed

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return get_user_by_id(int(user_id))
        except Exception:
            return None

    # Initialise DB tables and seed first admin if configured
    with app.app_context():
        try:
            init_db()
            seed_admin_if_needed()
        except Exception as e:
            app.logger.warning(f"DB init warning: {e}")

    from .routes import bp
    app.register_blueprint(bp)

    return app
