"""App factory configuration: secret key handling and upload limits."""
import importlib

import pytest


def build(monkeypatch, **env):
    """Reimport the app package with a specific environment."""
    for key in ("SECRET_KEY", "DATABASE_URL", "MAX_UPLOAD_MB"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import app as app_pkg
    importlib.reload(app_pkg)
    return app_pkg


def test_production_without_secret_key_refuses_to_start(monkeypatch, tmp_path):
    """A published fallback key would let anyone forge a session cookie."""
    app_pkg = build(monkeypatch, DATABASE_URL="postgresql://x/y")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        app_pkg.create_app()


def test_development_without_secret_key_still_starts(monkeypatch):
    app_pkg = build(monkeypatch)          # no DATABASE_URL => local SQLite
    application = app_pkg.create_app()
    assert application.config["SECRET_KEY"] == app_pkg.DEV_SECRET


def test_supplied_secret_key_is_used(monkeypatch):
    app_pkg = build(monkeypatch, SECRET_KEY="a-real-key")
    assert app_pkg.create_app().config["SECRET_KEY"] == "a-real-key"


def test_upload_limit_matches_the_nginx_limit(monkeypatch):
    """Flask must not reject a file nginx was willing to accept."""
    from pathlib import Path
    conf = Path(__file__).resolve().parent.parent / ".platform/nginx/conf.d/custom.conf"
    nginx_mb = int(conf.read_text().split("client_max_body_size")[1].split("M")[0].strip())

    app_pkg = build(monkeypatch, SECRET_KEY="k")
    flask_mb = app_pkg.create_app().config["MAX_CONTENT_LENGTH"] // 1024 // 1024
    assert flask_mb == nginx_mb, f"nginx allows {nginx_mb}MB but Flask allows {flask_mb}MB"


def test_upload_limit_is_overridable(monkeypatch):
    app_pkg = build(monkeypatch, SECRET_KEY="k", MAX_UPLOAD_MB="10")
    assert app_pkg.create_app().config["MAX_CONTENT_LENGTH"] == 10 * 1024 * 1024
