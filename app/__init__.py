from flask import Flask

def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "yeems-loss-analysis-2026"
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

    from .routes import bp
    app.register_blueprint(bp)

    return app
