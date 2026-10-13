from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

import yaml

from transcription import create_app
from transcription.settings import Settings
from transcription.signing import persistent_secret_key


class SigningKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.yml"
        self.config.write_text(yaml.safe_dump({
            "authorized_users": ["alice"],
            "server": {"data_dir": str(self.root), "secret_key": ""},
        }))
        environment = patch.dict(os.environ, {
            "TRANSCRIPTION_CONFIG": str(self.config),
            "TRANSCRIPTION_SECRET_KEY": "", "DATA_DIR": str(self.root),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def make_app(self):
        with patch("transcription.JobRunner"), patch("transcription.check_remote_connection"):
            return create_app({"TESTING": True})

    def test_remembered_cookie_is_accepted_after_app_restart(self):
        first_app = self.make_app()
        client = first_app.test_client()
        with patch("transcription.auth.authenticate", return_value=True):
            response = client.post("/login", json={
                "username": "alice", "password": "secret", "remember_me": True,
            })
        self.assertEqual(response.status_code, 200)
        cookie = client.get_cookie(first_app.config["SESSION_COOKIE_NAME"])
        self.assertIsNotNone(cookie.expires)
        restarted_app = self.make_app()
        self.assertEqual(first_app.secret_key, restarted_app.secret_key)
        restarted_client = restarted_app.test_client()
        restarted_client.set_cookie(cookie.key, cookie.value)
        with patch("transcription.auth.authenticate") as authenticate:
            self.assertEqual(restarted_client.get("/").status_code, 200)
            self.assertEqual(restarted_client.get("/login").status_code, 302)
            authenticate.assert_not_called()
        self.assertEqual(restarted_client.post("/logout", json={}).status_code, 200)
        self.assertEqual(restarted_client.get("/").status_code, 401)

    def test_concurrent_workers_share_private_key(self):
        barrier = threading.Barrier(8)

        def start_worker(_):
            barrier.wait()
            return persistent_secret_key(self.root)

        with ThreadPoolExecutor(max_workers=8) as pool:
            keys = list(pool.map(start_worker, range(8)))
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(len(keys[0]), 64)
        self.assertEqual(stat.S_IMODE((self.root / ".session-secret").stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob(".session-secret-*")), [])
        self.assertEqual(persistent_secret_key(self.root), keys[0])

    def test_configured_key_and_environment_override_persist_across_restart(self):
        document = yaml.safe_load(self.config.read_text())
        document["server"]["secret_key"] = "configured-key"
        self.config.write_text(yaml.safe_dump(document))
        self.assertEqual(self.make_app().secret_key, "configured-key")
        self.assertEqual(self.make_app().secret_key, "configured-key")
        with patch.dict(os.environ, {"TRANSCRIPTION_SECRET_KEY": "environment-key"}):
            self.assertEqual(self.make_app().secret_key, "environment-key")
        self.assertFalse((self.root / ".session-secret").exists())

    def test_settings_save_preserves_generated_key(self):
        app = self.make_app()
        app.config["SETTINGS"].save()
        self.assertEqual(Settings.load(self.config).secret_key, app.secret_key)
        self.assertEqual(self.make_app().secret_key, app.secret_key)

    def test_invalid_key_file_is_not_silently_replaced(self):
        path = self.root / ".session-secret"
        path.write_text("")
        with self.assertRaisesRegex(RuntimeError, "Invalid cookie signing key"):
            self.make_app()
        self.assertEqual(path.read_text(), "")
