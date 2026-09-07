import os
from flask import Flask, flash, redirect, request, url_for
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

login_manager = LoginManager()
csrf          = CSRFProtect()

DEV_SECRET = "dev-secret-key-change-in-production"

# Keep in step with client_max_body_size in .platform/nginx/conf.d/custom.conf,
# so a file nginx accepts isn't then rejected by Flask.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "64"))


def create_app():
    app = Flask(__name__)

    # Session cookies are signed with this. Falling back to a published
    # constant in production would let anyone forge a login, so refuse to
    # start instead — DATABASE_URL being set is what marks a real deployment.
    secret = os.environ.get("SECRET_KEY", "")
    if not secret:
        if os.environ.get("DATABASE_URL"):
            raise RuntimeError(
                "SECRET_KEY is not set. Set it as an environment property "
                "before starting the app — without it session cookies are "
                "forgeable. Generate one with: "
                'python -c "import secrets; print(secrets.token_hex(32))"'
            )
        secret = DEV_SECRET  # local SQLite development only
    app.config["SECRET_KEY"] = secret

    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

    # Reject cross-site POSTs to the admin and auth endpoints.
    csrf.init_app(app)

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

    @app.errorhandler(413)
    def upload_too_large(e):
        flash(f"That file is larger than the {MAX_UPLOAD_MB} MB upload limit. "
              "Try splitting the report into smaller date ranges.", "error")
        return redirect(url_for("main.index")), 413

    return app
