import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from types import SimpleNamespace

from flask import Flask

from transcription.db import JobStore
from transcription.routes import web


class AudioSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(Path(temporary.name) / 'jobs.sqlite')
        self.store.initialize()
        self.store.create('job', 'Meeting', 'voice', ['original.mp3'], {
            'volume': 'auto', 'adjust_volume': True, 'auto_analysis': False,
            'adjusted_files': ['adjusted.mp3'],
        })
        self.store.update('job', status='completed')
        self.runner = Mock()
        self.runner.is_active.return_value = False
        app = Flask(__name__)
        self.settings = SimpleNamespace(
            diarization_enabled=True, voiceprint_enabled=True,
            job_dir=lambda job_id: Path(temporary.name) / job_id,
            timeline_pattern='{time}',
        )
        app.config.update(TESTING=True, SECRET_KEY='test', SETTINGS=self.settings)
        app.template_folder = str(Path(__file__).resolve().parents[1] / 'transcription' / 'templates')
        app.extensions.update(job_store=self.store, job_runner=self.runner)
        app.register_blueprint(web)
        self.client = app.test_client()

    def test_save_persists_settings_without_restarting_or_changing_results(self):
        before = self.store.get('job')
        response = self.client.post('/jobs/job/audio-settings', data={'volume': '-20%'})
        self.assertEqual(response.status_code, 302)
        after = self.store.get('job')
        expected = dict(before['config'], volume='-20%', adjust_volume=False)
        self.assertEqual(after, dict(before, config=expected))
        self.runner.retry.assert_not_called()
        self.runner.submit.assert_not_called()

    def test_invalid_settings_do_not_modify_job(self):
        before = self.store.get('job')
        response = self.client.post('/jobs/job/audio-settings', data={'volume': 'invalid'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.get('job'), before)

    def test_save_rejects_active_jobs(self):
        for status in ('queued', 'running', 'stopping'):
            with self.subTest(status=status):
                self.store.update('job', status=status)
                response = self.client.post('/jobs/job/audio-settings', data={'volume': '+20%'})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.store.get('job')['config']['volume'], 'auto')

    def test_save_and_retry_passes_settings_to_existing_retry_flow(self):
        response = self.client.post('/jobs/job/retry', data={
            'volume_settings': 'true', 'volume': '+30%', 'adjust_volume': 'true',
        })
        self.assertEqual(response.status_code, 302)
        self.runner.retry.assert_called_once_with('job', {'volume': '+30%', 'adjust_volume': True})

    def test_save_missing_or_non_voice_job(self):
        self.assertEqual(self.client.post('/jobs/missing/audio-settings').status_code, 404)
        self.store.create('text', 'Text', 'text', [], {})
        self.assertEqual(self.client.post('/jobs/text/audio-settings').status_code, 400)


    def test_full_settings_save_and_retry(self):
        payload = {
            'audio_settings': 'true', 'volume': '+20%', 'adjust_volume': 'true',
            'use_diarization': 'true', 'use_voiceprint_recognition': 'true',
            'auto_analysis': 'true',
        }
        expected = dict(volume='+20%', adjust_volume=True, diarization_enabled=True,
                        voiceprint_recognition=True, auto_analysis=True, remove_audio_after_completed=False)
        self.assertEqual(self.client.post('/jobs/job/audio-settings', data=payload).status_code, 302)
        config = self.store.get('job')['config']
        for key, value in expected.items():
            self.assertEqual(config[key], value)
        self.assertEqual(config['adjusted_files'], ['adjusted.mp3'])
        self.assertEqual(self.client.post('/jobs/job/retry', data=payload).status_code, 302)
        self.runner.retry.assert_called_once_with('job', expected)

    def test_unchecked_options_are_saved_as_false(self):
        self.client.post('/jobs/job/audio-settings', data={
            'audio_settings': 'true', 'use_diarization': 'true',
            'use_voiceprint_recognition': 'true', 'auto_analysis': 'true',
        })
        self.client.post('/jobs/job/audio-settings', data={'audio_settings': 'true'})
        config = self.store.get('job')['config']
        for key in ('diarization_enabled', 'voiceprint_recognition', 'auto_analysis'):
            self.assertFalse(config[key])

    def test_invalid_voice_options_do_not_save_or_retry(self):
        for endpoint in ('audio-settings', 'retry'):
            for diarization_available, voiceprint_available, payload in (
                (True, True, {'use_voiceprint_recognition': 'true'}),
                (False, True, {'use_diarization': 'true'}),
                (True, False, {'use_diarization': 'true', 'use_voiceprint_recognition': 'true'}),
            ):
                with self.subTest(endpoint=endpoint, payload=payload):
                    self.settings.diarization_enabled = diarization_available
                    self.settings.voiceprint_enabled = voiceprint_available
                    before = self.store.get('job')
                    response = self.client.post('/jobs/job/' + endpoint, data=payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.store.get('job'), before)
        self.runner.retry.assert_not_called()

    def test_detail_renders_saved_options_associated_with_retry_form(self):
        self.client.post('/jobs/job/audio-settings', data={
            'audio_settings': 'true', 'use_diarization': 'true',
            'use_voiceprint_recognition': 'true', 'auto_analysis': 'true',
        })
        response = self.client.get('/jobs/job')
        self.assertEqual(response.status_code, 200)
        from html.parser import HTMLParser

        inputs = {}
        class Inputs(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'input':
                    inputs[attrs.get('name')] = attrs
        Inputs().feed(response.get_data(as_text=True))
        for name in ('use_diarization', 'use_voiceprint_recognition', 'auto_analysis'):
            self.assertEqual(inputs[name]['form'], 'retry-form')
            self.assertIn('checked', inputs[name])
            self.assertNotIn('disabled', inputs[name])
