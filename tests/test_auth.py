"""Optional shared-password authentication.

Auth is off unless a password is configured, so most tests here need a
reloaded configuration that has one. `Settings` is frozen and resolved at
import time, which is why the password goes into the environment before the
application modules are rebuilt.
"""

import importlib
import sys
import time

import pytest

PASSWORD = "correct horse battery staple"


@pytest.fixture
def secured(monkeypatch, tmp_path):
    """An application whose config carries a password and a fixed secret."""
    parent = tmp_path / "media"
    videos = parent / "library"
    thumbs = tmp_path / "thumbnails"
    for directory in (videos, thumbs):
        directory.mkdir(parents=True)

    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "missing-config.json"))
    monkeypatch.setenv("VIDEO_DIR", str(videos))
    monkeypatch.setenv("PARENT_DIR", str(parent))
    monkeypatch.setenv("THUMBNAIL_DIR", str(thumbs))
    monkeypatch.setenv("DB_FILE", str(tmp_path / "db.json"))
    monkeypatch.setenv("ALLOWED_IPS", "127.0.0.1,::1")
    monkeypatch.setenv("TRUSTED_PROXIES", "")
    monkeypatch.setenv("AUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "test-signing-secret")

    import config

    importlib.reload(config)
    for name in ("database", "downloads", "boorus", "utils", "middleware", "auth"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    import database
    import utils

    database.reset_cache()
    database.init_db()
    monkeypatch.setattr(
        utils, "scan_library",
        lambda directory=None: {
            "converted": 0, "added": 0, "pruned": 0, "backfilled": 0, "duplicates": 0,
        },
    )

    main = importlib.reload(sys.modules["main"]) if "main" in sys.modules else \
        importlib.import_module("main")

    from fastapi.testclient import TestClient

    from tests.conftest import with_peer

    with TestClient(with_peer(main.app, "127.0.0.1")) as test_client:
        yield test_client, main

    database.reset_cache()
    importlib.reload(config)


# --- disabled by default ---------------------------------------------------------


def test_auth_is_off_when_no_password_is_configured(client, app_env):
    """The previous behaviour, unchanged, for anyone who does not opt in."""
    test_client, main = client

    assert main.auth.is_enabled() is False
    assert test_client.get("/").status_code == 200


def test_the_login_page_redirects_home_when_auth_is_off(client, app_env):
    test_client, _ = client

    response = test_client.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_logging_in_is_404_when_auth_is_off(client, app_env):
    test_client, _ = client

    assert test_client.post("/login", json={"password": "x"}).status_code == 404


# --- tokens -----------------------------------------------------------------------


def test_a_freshly_issued_token_verifies(secured):
    _, main = secured

    assert main.auth.verify_token(main.auth.issue_token()) is True


def test_an_expired_token_is_rejected(secured):
    _, main = secured

    token = main.auth.issue_token(now=time.time() - main.auth.settings.session_ttl - 10)

    assert main.auth.verify_token(token) is False


def test_a_tampered_expiry_is_rejected(secured):
    """Extending the expiry must invalidate the signature over it."""
    _, main = secured

    expiry, _, signature = main.auth.issue_token().partition(".")
    forged = f"{int(expiry) + 100000}.{signature}"

    assert main.auth.verify_token(forged) is False


@pytest.mark.parametrize("token", ["", None, "garbage", "notanumber.sig", "12345", "."])
def test_malformed_tokens_are_rejected(secured, token):
    _, main = secured

    assert main.auth.verify_token(token) is False


def test_a_token_signed_with_another_secret_is_rejected(secured, monkeypatch):
    _, main = secured
    token = main.auth.issue_token()

    monkeypatch.setattr(main.auth, "_EPHEMERAL_SECRET", b"different")
    monkeypatch.setattr(
        main.auth, "settings",
        main.auth.settings.__class__(**{**main.auth.settings.__dict__, "session_secret": "other"}),
    )

    assert main.auth.verify_token(token) is False


# --- gating -------------------------------------------------------------------------


def test_an_unauthenticated_page_request_redirects_to_login(secured):
    test_client, _ = secured

    response = test_client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_an_unauthenticated_api_call_gets_401_not_html(secured):
    """A fetch() caller needs an error it can report, not a login page."""
    test_client, _ = secured

    response = test_client.post("/api/download", json={"url": "https://example.com/a.mp4"})

    assert response.status_code == 401
    assert response.json()["detail"]


def test_streaming_is_gated_too(secured):
    test_client, _ = secured

    assert test_client.get("/videos/anything", follow_redirects=False).status_code == 303


def test_the_health_check_stays_reachable(secured):
    """The container polls it; requiring a session would fail every check."""
    test_client, _ = secured

    assert test_client.get("/healthz").status_code == 200


def test_the_login_page_is_reachable_without_a_session(secured):
    test_client, _ = secured

    assert test_client.get("/login").status_code == 200


def test_the_web_app_manifest_is_reachable_without_a_session(secured):
    test_client, _ = secured

    response = test_client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/manifest+json"
    assert [icon["sizes"] for icon in response.json()["icons"]] == ["192x192", "512x512"]


# --- signing in ----------------------------------------------------------------------


def test_the_correct_password_sets_a_session_cookie(secured):
    test_client, main = secured

    response = test_client.post("/login", json={"password": PASSWORD})

    assert response.status_code == 200
    assert main.auth.COOKIE_NAME in response.cookies


def test_a_session_grants_access(secured):
    test_client, _ = secured
    test_client.post("/login", json={"password": PASSWORD})

    assert test_client.get("/").status_code == 200


def test_the_wrong_password_is_rejected(secured):
    test_client, _ = secured

    response = test_client.post("/login", json={"password": "wrong"})

    assert response.status_code == 401


def test_the_session_cookie_is_httponly_and_samesite(secured):
    """Script must not be able to read it, and a cross-site POST must not send it."""
    test_client, _ = secured

    response = test_client.post("/login", json={"password": PASSWORD})
    header = response.headers["set-cookie"].lower()

    assert "httponly" in header
    assert "samesite=lax" in header


def test_signing_out_clears_the_session(secured):
    test_client, _ = secured
    test_client.post("/login", json={"password": PASSWORD})

    test_client.post("/logout")

    assert test_client.get("/", follow_redirects=False).status_code == 303


def test_the_requested_path_survives_the_login(secured):
    test_client, _ = secured

    redirect = test_client.get("/play/abc", follow_redirects=False)
    assert "next=" in redirect.headers["location"]

    response = test_client.post("/login", json={"password": PASSWORD, "next": "/play/abc"})
    assert response.json()["next"] == "/play/abc"


@pytest.mark.parametrize(
    "target",
    ["https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)", ""],
)
def test_an_off_site_redirect_target_is_discarded(secured, target):
    """A login form that redirects anywhere is a phishing primitive."""
    test_client, _ = secured

    response = test_client.post("/login", json={"password": PASSWORD, "next": target})

    assert response.json()["next"] == "/"


def test_a_same_site_target_is_kept(secured):
    test_client, _ = secured

    response = test_client.post("/login", json={"password": PASSWORD, "next": "/?sort=title"})

    assert response.json()["next"] == "/?sort=title"


# --- throttling ------------------------------------------------------------------------


def test_repeated_failures_lock_the_address_out(secured):
    test_client, main = secured

    for _ in range(main.auth.MAX_FAILURES):
        assert test_client.post("/login", json={"password": "wrong"}).status_code == 401

    response = test_client.post("/login", json={"password": "wrong"})

    assert response.status_code == 429


def test_the_lockout_applies_even_to_the_correct_password(secured):
    """Otherwise the throttle is trivially bypassed by guessing right once."""
    test_client, main = secured

    for _ in range(main.auth.MAX_FAILURES):
        test_client.post("/login", json={"password": "wrong"})

    assert test_client.post("/login", json={"password": PASSWORD}).status_code == 429


def test_a_successful_login_resets_the_failure_count(secured):
    test_client, main = secured

    for _ in range(main.auth.MAX_FAILURES - 1):
        test_client.post("/login", json={"password": "wrong"})
    assert test_client.post("/login", json={"password": PASSWORD}).status_code == 200

    for _ in range(main.auth.MAX_FAILURES - 1):
        assert test_client.post("/login", json={"password": "wrong"}).status_code == 401


def test_the_lockout_expires(secured):
    _, main = secured
    throttle = main.auth.Throttle(max_failures=2, lockout_seconds=60)

    throttle.record_failure("1.2.3.4", now=1000.0)
    throttle.record_failure("1.2.3.4", now=1000.0)

    assert throttle.locked_for("1.2.3.4", now=1010.0) > 0
    assert throttle.locked_for("1.2.3.4", now=1070.0) == 0


def test_the_lockout_is_per_address(secured):
    _, main = secured
    throttle = main.auth.Throttle(max_failures=1, lockout_seconds=60)

    throttle.record_failure("1.2.3.4", now=1000.0)

    assert throttle.locked_for("1.2.3.4", now=1000.0) > 0
    assert throttle.locked_for("5.6.7.8", now=1000.0) == 0


# --- layering ------------------------------------------------------------------------


def test_the_ip_allowlist_still_runs_first(secured, monkeypatch):
    """A blocked address is turned away without the password layer seeing it."""
    from fastapi.testclient import TestClient

    from tests.conftest import with_peer

    _, main = secured
    with TestClient(with_peer(main.app, "10.99.99.99")) as blocked:
        response = blocked.get("/login", follow_redirects=False)

    assert response.status_code == 403
