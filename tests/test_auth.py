import importlib.util
import re
import time
import unittest
from urllib.parse import urljoin, urlsplit, parse_qs
import yaml
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from transcription import create_app
from transcription.auth import authenticate
from transcription.settings import Settings


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = replace(
            Settings.load(Path(__file__).resolve().parents[1] / "config.yml"),
            data_dir=Path(temporary.name), diarization_enabled=False,
            authorized_users=("alice",),
        )
        with patch("transcription.Settings.load", return_value=self.settings), \
                patch("transcription.JobRunner"), \
                patch("transcription.check_remote_connection"):
            self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()

    def login(self, remember_me=False):
        with patch("transcription.auth.authenticate", return_value=True):
            return self.client.post("/login", json={
                "username": "alice", "password": "secret", "remember_me": remember_me,
            })

    def test_all_registered_routes_require_authentication(self):
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint == "auth.login":
                continue
            path = str(rule)
            for argument in rule.arguments:
                path = re.sub(r"<(?:(?:[^:>]+):)?" + argument + r">", "missing", path)
            for method in rule.methods - {"HEAD"}:
                with self.subTest(path=path, method=method):
                    response = self.client.open(path, method=method)
                    self.assertEqual(response.status_code, 401)
        self.app.extensions["job_runner"].submit.assert_not_called()

    def test_browser_redirect_and_public_login(self):
        response = self.client.get("/jobs/missing", headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login?next=", response.location)
        page = self.client.get(urljoin("/jobs/missing", response.location))
        self.assertEqual(page.status_code, 200)
        self.assertIn('autocomplete="current-password"', page.text)
        self.assertNotIn('/static/', page.text)

    def test_proxy_mount_login_round_trip_and_resources(self):
        for mount in ("", "/transcription", "/tools/transcription"):
            for path in ("/", "/jobs/missing?tab=logs"):
                with self.subTest(mount=mount, path=path):
                    self.client.post("/logout", json={})
                    original = "https://example.test" + mount + path
                    response = self.client.get(path, headers={"Accept": "text/html"})
                    login_url = urlsplit(urljoin(original, response.location))
                    self.assertEqual(login_url.path, mount + "/login")
                    self.assertEqual(parse_qs(login_url.query)["next"], [path])
                    self.client.get("/login")
                    with self.client.session_transaction() as session:
                        token = session["login_csrf"]
                    with patch("transcription.auth.authenticate", return_value=True):
                        response = self.client.post("/login?" + login_url.query, data={
                            "username": "alice", "password": "secret", "csrf_token": token,
                        })
                    self.assertEqual(urljoin(login_url.geturl(), response.location), original)
            for path in ("/", "/settings", "/voiceprints", "/realtime"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                for link in re.findall(r'(?:href|src|action|data-stream|data-save|data-worklet)="([^"]+)"', response.text):
                    if link.startswith("#"):
                        continue
                    self.assertFalse(link.startswith("/"), link)
                    resolved = urljoin("https://example.test" + mount + path, link)
                    self.assertTrue(resolved.startswith("https://example.test" + mount + "/"), resolved)

    def test_unlisted_users_rejected_before_ldap_login(self):
        with patch("transcription.auth.authenticate", return_value=True) as bind:
            for username in ("bob", "Alice", " alice "):
                with self.subTest(username=username):
                    response = self.client.post("/login", json={"username": username, "password": "secret"})
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(self.client.get("/").status_code, 401)
            bind.assert_not_called()

    def test_empty_allowlist_denies_login(self):
        self.app.config["SETTINGS"] = replace(self.settings, authorized_users=())
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 401)

    def test_removed_user_loses_access_with_existing_session(self):
        self.assertEqual(self.login().status_code, 200)
        self.app.config["SETTINGS"] = replace(self.settings, authorized_users=("bob",))
        self.assertEqual(self.client.get("/").status_code, 403)
        with self.client.session_transaction() as session:
            self.assertNotIn("username", session)
        self.assertEqual(self.client.get("/api/jobs/missing").status_code, 401)

    def test_login_access_logout_and_no_password_in_cookie(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), {"username": "alice", "_permanent": False})
        self.assertNotIn("Expires=", response.headers["Set-Cookie"])
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.client.get("/static/app.css") as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post("/logout", json={}).status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 401)

    def test_browser_accepting_signed_exchanges_redirects_to_login(self):
        headers = {"Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8,application/signed-exchange;v=b3;q=1"
        )}
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                response = self.client.open("/", method=method, headers=headers)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.startswith("./login?next="))
        self.assertEqual(self.client.get("/api/jobs/missing", headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/realtime/stream", headers={
            **headers, "Upgrade": "websocket", "Connection": "Upgrade",
        }).status_code, 401)

    def test_json_preference_or_html_rejection_returns_401(self):
        for accept in ("application/json", "text/html;q=0.5,application/json",
                       "text/html;q=0,*/*;q=0.8"):
            with self.subTest(accept=accept):
                response = self.client.get("/", headers={"Accept": accept})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json["login_url"], "./login")

    def test_bad_login_and_malformed_payloads(self):
        with patch("transcription.auth.authenticate", return_value=False):
            for payload in ({"username": "alice", "password": "bad"}, [], None,
                            {"username": [], "password": {}}, {}):
                response = self.client.post("/login", json=payload, content_type="application/json")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(self.client.get("/").status_code, 401)

    def test_form_login_csrf_and_safe_redirect(self):
        self.assertEqual(self.client.post("/login", data={}).status_code, 400)
        self.assertEqual(self.client.post("/login", data={"csrf_token": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post("/login", data={"csrf_token": "中文"}).status_code, 400)
        for target in ("https://evil.example", "//evil.example", "/\\evil.example", "//[", "/../outside", "/%2e%2e/outside", "/%5cevil"):
            self.client.post("/logout", json={})
            self.client.get("/login")
            with self.client.session_transaction() as session:
                token = session["login_csrf"]
            with patch("transcription.auth.authenticate", return_value=True):
                response = self.client.post("/login", query_string={"next": target}, data={
                    "username": "alice", "password": "secret", "csrf_token": token,
                })
            self.assertEqual(response.location, "./")

    def test_https_proxy_form_login_and_mutations(self):
        headers = {"Host": "portal.example:8443", "X-Forwarded-Proto": "https",
                   "Origin": "https://portal.example:8443"}
        for trusted in (False, True):
            with self.subTest(trusted=trusted):
                settings = replace(self.settings, trust_proxy_proto=trusted)
                with patch("transcription.Settings.load", return_value=settings), \
                        patch("transcription.JobRunner"), \
                        patch("transcription.check_remote_connection"):
                    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
                client = app.test_client()
                client.get("/login", headers=headers)
                with client.session_transaction(base_url="http://portal.example:8443") as session:
                    token = session["login_csrf"]
                with patch("transcription.auth.authenticate", return_value=True) as bind:
                    response = client.post("/login?next=/", headers=headers, data={
                        "username": "alice", "password": "secret", "csrf_token": token,
                    })
                    self.assertEqual(response.status_code, 302 if trusted else 403)
                    if not trusted:
                        bind.assert_not_called()
                        continue
                self.assertEqual(response.location, "./")
                for origin in ("https://evil.example", "http://portal.example:8443", "null"):
                    response = client.post("/logout", json={}, headers={
                        **headers, "Origin": origin,
                        "X-Forwarded-Host": "evil.example",
                    })
                    self.assertEqual(response.status_code, 403)
                self.assertEqual(client.get("/", headers=headers).status_code, 200)
                self.assertEqual(client.post("/logout", json={}, headers=headers).status_code, 200)

    def test_cross_origin_login_rejected(self):
        with patch("transcription.auth.authenticate") as bind:
            response = self.client.post("/login", json={"username": "alice", "password": "secret"},
                                        headers={"Origin": "https://evil.example"})
            self.assertEqual(response.status_code, 403)
            bind.assert_not_called()

    def test_websocket_handshake_rejected_before_upgrade(self):
        response = self.client.get("/realtime/stream", headers={
            "Upgrade": "websocket", "Connection": "Upgrade", "Accept": "text/html",
        })
        self.assertEqual(response.status_code, 401)

    def test_expired_and_tampered_sessions_rejected(self):
        serializer = self.app.session_interface.get_signing_serializer(self.app)
        with patch("time.time", return_value=1):
            expired = serializer.dumps({"username": "alice", "_permanent": True})
        for cookie in (expired, serializer.dumps({"username": "alice"}) + "tampered"):
            self.client.set_cookie(self.app.config["SESSION_COOKIE_NAME"], cookie)
            self.assertEqual(self.client.get("/").status_code, 401)

    def test_settings_round_trip_and_secret_redaction(self):
        settings = replace(self.settings, config_path=self.settings.data_dir / "config.yml",
                           auto_login_expiry=14, trust_proxy_proto=True,
                           ldap_server="ldaps://directory.example", ldap_port=636,
                           ldap_base_dn="dc=example", ldap_user_dn="cn={username},{base_dn}")
        settings.save()
        loaded = Settings.load(settings.config_path)
        for field in ("ldap_server", "ldap_port", "ldap_base_dn", "ldap_user_dn", "authorized_users",
                      "auto_login_expiry", "trust_proxy_proto"):
            self.assertEqual(getattr(loaded, field), getattr(settings, field))
        self.assertNotEqual(loaded.public_dict()["secret_key"], loaded.secret_key)

    def test_remember_me_cookie_survives_new_browser_client_and_expires(self):
        now = int(time.time())
        with patch("time.time", return_value=now):
            response = self.login(remember_me=True)
        self.assertEqual(response.status_code, 200)
        cookie = self.client.get_cookie(self.app.config["SESSION_COOKIE_NAME"])
        self.assertIsNotNone(cookie.expires)
        remaining = (cookie.expires - datetime.now(timezone.utc)).total_seconds()
        self.assertAlmostEqual(remaining, 7 * 86400, delta=5)
        returning_client = self.app.test_client()
        returning_client.set_cookie(cookie.key, cookie.value)
        with patch("transcription.auth.authenticate") as bind:
            self.assertEqual(returning_client.get("/").status_code, 200)
            response = returning_client.get("/login?next=/jobs")
            self.assertEqual(response.location, "./jobs")
            self.assertNotIn("Set-Cookie", response.headers)
            self.assertEqual(returning_client.get("/login?next=//evil.example").location, "./")
            bind.assert_not_called()
        with patch("time.time", return_value=now + 7 * 86400 + 1):
            self.assertEqual(returning_client.get("/").status_code, 401)
            self.assertEqual(returning_client.get("/login").status_code, 200)
        self.client.post("/logout", json={})
        self.assertIsNone(self.client.get_cookie(cookie.key))
        self.assertEqual(self.client.get("/").status_code, 401)

    def test_form_remember_me_uses_configured_expiry(self):
        from transcription.auth import init_auth
        from flask import Flask
        app = Flask("transcription")
        app.config.update(SECRET_KEY="test-secret", TESTING=True,
                          SETTINGS=replace(self.settings, auto_login_expiry=14))
        init_auth(app)
        client = app.test_client()
        page = client.get("/login")
        self.assertIn('type="checkbox" name="remember_me"', page.text)
        with client.session_transaction() as session:
            token = session["login_csrf"]
        with patch("transcription.auth.authenticate", return_value=True):
            response = client.post("/login", data={
                "username": "alice", "password": "secret", "csrf_token": token,
                "remember_me": "on",
            })
        self.assertEqual(response.status_code, 302)
        cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"])
        remaining = (cookie.expires - datetime.now(timezone.utc)).total_seconds()
        self.assertAlmostEqual(remaining, 14 * 86400, delta=5)
        with patch("time.time", return_value=time.time() + 14 * 86400 + 1):
            self.assertEqual(client.get("/private").status_code, 401)

    def test_auto_login_expiry_validation_and_default(self):
        path = self.settings.data_dir / "expiry.yml"
        path.write_text("{}")
        self.assertEqual(Settings.load(path).auto_login_expiry, 7)
        for value in (None, True, 0, -1, 1.5, "7", [], {}, 365001):
            path.write_text(yaml.safe_dump({"auto_login_expiry": value}))
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "auto_login_expiry"):
                Settings.load(path)

    def test_empty_credentials_rejected_without_ldap(self):
        for username, password in (("", "secret"), ("alice", ""), ("  ", "secret"), ("a\x00b", "secret")):
            self.assertFalse(authenticate(username, password))

    def test_invalid_ldap_configuration_rejected(self):
        path = self.settings.data_dir / "invalid.yml"
        for ldap in ({"port": 0}, {"server": "https://example.com"},
                     {"user_dn": "cn=fixed"}, {"user_dn": "{username},{unknown}"}):
            path.write_text(yaml.safe_dump({"ldap": ldap}))
            with self.subTest(ldap=ldap), self.assertRaises(ValueError):
                Settings.load(path)

    def test_authorized_users_configuration_validation(self):
        path = self.settings.data_dir / "allowlist.yml"
        for value in (None, "alice", {}, [None], [123], [""], [" alice"]):
            path.write_text(yaml.safe_dump({"authorized_users": value}))
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings.load(path)
        for document in ({}, {"authorized_users": []}):
            path.write_text(yaml.safe_dump(document))
            self.assertEqual(Settings.load(path).authorized_users, ())
        path.write_text(yaml.safe_dump({"authorized_users": ["alice", "bob", "alice"]}))
        self.assertEqual(Settings.load(path).authorized_users, ("alice", "bob"))

    @unittest.skipUnless(importlib.util.find_spec("ldap3"), "ldap3 is not installed")
    def test_ldap_bind_escapes_dn_and_always_closes_connection(self):
        from ldap3.core.exceptions import LDAPException
        for result in (True, False, LDAPException("unavailable")):
            connection = Mock()
            if isinstance(result, Exception):
                connection.bind.side_effect = result
            else:
                connection.bind.return_value = result
            with self.app.app_context(), patch("ldap3.Server") as server, \
                    patch("ldap3.Connection", return_value=connection) as factory:
                self.assertEqual(authenticate("alice,admin", "secret"), result is True)
                self.assertEqual(factory.call_args.kwargs["user"],
                                 "uid=alice\\,admin,ou=People,dc=abc,dc=com")
                self.assertFalse(factory.call_args.kwargs["auto_referrals"])
                self.assertEqual(server.call_args.kwargs["connect_timeout"], 5)
                connection.unbind.assert_called_once()
