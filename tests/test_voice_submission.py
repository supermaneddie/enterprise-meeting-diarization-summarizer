import io
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from flask import Flask
from transcription.db import JobStore
from transcription.routes import web
from transcription.settings import Settings


class VoiceSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        settings = replace(
            Settings.load(Path(__file__).resolve().parents[1] / 'config.yml'),
            data_dir=root, diarization_enabled=True,
        )
        self.store = JobStore(root / 'jobs.sqlite')
        self.store.initialize()
        self.runner = Mock()
        self.runner.submit.return_value = True
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='test', SETTINGS=settings)
        self.app.extensions.update(job_store=self.store, job_runner=self.runner)
        self.app.register_blueprint(web)
        self.client = self.app.test_client()

    def test_voice_json_still_returns_immediately_with_analysis_default(self):
        response = self.client.post('/voice', json={'name': 'Meeting', 'url': 'https://example.com/a.mp3'})
        self.assertEqual(response.status_code, 202)
        job = self.store.get(response.get_json()['job_id'])
        self.assertEqual(job['status'], 'queued')
        self.assertTrue(job['config']['auto_analysis'])
        self.assertFalse(job['config']['diarization_enabled'])
        self.assertFalse(job['config']['adjust_volume'])

    def test_voice_form_redirect_and_upload_validation(self):
        response = self.client.post('/voice', data={'name': 'Meeting', 'url': 'https://example.com/a.mp3'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.store.list()[0]['config']['auto_analysis'])
        response = self.client.post('/voice', data={
            'name': 'Bad file', 'file': (io.BytesIO(b'not audio'), 'notes.txt'),
        }, headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.store.list()), 1)
