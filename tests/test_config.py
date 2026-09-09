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


# --- Behind an HTTPS proxy (CloudFront / ALB) --------------------------------
#
# The proxy terminates TLS and speaks plain HTTP to the app, so Flask has to be
# told. Each of these fails silently if it isn't.

def test_cookies_are_hardened_even_on_plain_http(monkeypatch):
    app_pkg = build(monkeypatch, SECRET_KEY="k")
    cfg = app_pkg.create_app().config
    assert cfg["SESSION_COOKIE_HTTPONLY"] is True
    assert cfg["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_without_the_flag_nothing_is_assumed(monkeypatch):
    """Trusting X-Forwarded-* with no proxy in front would let a client lie."""
    app_pkg = build(monkeypatch, SECRET_KEY="k")
    application = app_pkg.create_app()
    assert not application.config.get("SESSION_COOKIE_SECURE")
    assert application.config.get("PREFERRED_URL_SCHEME", "http") == "http"


def test_behind_proxy_marks_cookies_secure(monkeypatch):
    monkeypatch.setenv("BEHIND_HTTPS_PROXY", "1")
    app_pkg = build(monkeypatch, SECRET_KEY="k")
    monkeypatch.setenv("BEHIND_HTTPS_PROXY", "1")
    cfg = app_pkg.create_app().config
    assert cfg["SESSION_COOKIE_SECURE"] is True
    assert cfg["REMEMBER_COOKIE_SECURE"] is True
    assert cfg["PREFERRED_URL_SCHEME"] == "https"


def _app_with(monkeypatch, **env):
    """Build the app and capture the URL the reset email would carry."""
    import importlib, sys, types
    monkeypatch.setenv("SECRET_KEY", "k")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    # app.email imports boto3, which isn't needed to test URL generation.
    if "boto3" not in sys.modules:
        stub = types.ModuleType("boto3")
        stub.client = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "boto3", stub)
        exc = types.ModuleType("botocore.exceptions")
        exc.ClientError = type("ClientError", (Exception,), {})
        monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
        monkeypatch.setitem(sys.modules, "botocore.exceptions", exc)

    import app as app_pkg
    importlib.reload(app_pkg)
    application = app_pkg.create_app()
    application.config["WTF_CSRF_ENABLED"] = False

    import app.email as email_mod
    importlib.reload(email_mod)
    captured = {}
    monkeypatch.setattr(email_mod, "send_reset_email",
                        lambda to, url, name="": captured.update(url=url))
    return application, captured


def _request_reset(application, captured, sqlite_db, **headers):
    sqlite_db.create_user("m@yeemscoffee.com", "M", "pw12345678")
    application.test_client().post(
        "/forgot-password", data={"email": "m@yeemscoffee.com"}, headers=headers)
    return captured.get("url")


def test_reset_links_use_https_and_the_public_hostname(monkeypatch, sqlite_db):
    """
    Exercised through the real WSGI stack, because ProxyFix is middleware —
    a test_request_context would skip it and prove nothing.

    The reset link is emailed, so getting this wrong sends managers to an
    http:// URL on the raw origin hostname — the exact thing HTTPS was for.
    """
    application, captured = _app_with(monkeypatch, BEHIND_HTTPS_PROXY="1")
    link = _request_reset(application, captured, sqlite_db,
                          **{"X-Forwarded-Proto": "https",
                             "X-Forwarded-Host": "lossanalysis.yeemscoffee.com"})

    assert link, "no reset email was sent"
    assert link.startswith("https://"), f"emailed an insecure link: {link}"
    assert "lossanalysis.yeemscoffee.com" in link, f"wrong hostname: {link}"


def test_forwarded_headers_are_ignored_without_the_flag(monkeypatch, sqlite_db):
    """A client setting X-Forwarded-* itself must not be believed."""
    application, captured = _app_with(monkeypatch)      # no BEHIND_HTTPS_PROXY
    link = _request_reset(application, captured, sqlite_db,
                          **{"X-Forwarded-Proto": "https",
                             "X-Forwarded-Host": "attacker.example.com"})

    assert link, "no reset email was sent"
    assert "attacker.example.com" not in link, f"trusted a spoofed host: {link}"
