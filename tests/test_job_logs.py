import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from flask import Flask

from transcription.db import JobStore
from transcription.routes import _read_logs, web


class JobLogsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'langgraph.log'
        store = JobStore(self.root / 'jobs.sqlite')
        store.initialize()
        store.create('job', 'Meeting', 'text', [], {})
        app = Flask('transcription')
        app.config.update(TESTING=True, SECRET_KEY='test', SETTINGS=SimpleNamespace(
            job_dir=lambda job_id: self.root, timeline_pattern='{seconds}', voiceprint_enabled=False, diarization_enabled=False,
        ))
        app.extensions['job_store'] = store
        app.register_blueprint(web)
        self.client = app.test_client()

    def test_initial_page_and_incremental_api_use_byte_offsets(self):
        initial = 'Starting 中文 😀\n'
        self.path.write_text(initial, encoding='utf-8')
        page = self.client.get('/jobs/job')
        self.assertEqual(page.status_code, 200)
        offset = len(initial.encode('utf-8'))
        self.assertIn(f'let logsOffset={offset};', page.text)
        self.assertEqual(self.client.get('/api/jobs/job').json['logs'], initial)
        with self.path.open('a', encoding='utf-8') as handle:
            handle.write('Next event\n')
        delta = self.client.get(f'/api/jobs/job?logs_offset={offset}').json
        self.assertEqual(delta['logs'], 'Next event\n')
        self.assertFalse(delta['logs_reset'])
        idle = self.client.get(f"/api/jobs/job?logs_offset={delta['logs_offset']}").json
        self.assertEqual(idle['logs'], '')
        self.assertEqual(idle['logs_offset'], delta['logs_offset'])

    def test_missing_and_truncated_logs_reset_cursor(self):
        self.assertEqual(_read_logs(self.path, 0), {
            'logs': '', 'logs_offset': 0, 'logs_reset': False,
        })
        self.assertTrue(_read_logs(self.path, 50)['logs_reset'])
        self.path.write_text('new\n')
        self.assertEqual(_read_logs(self.path, 50), {
            'logs': 'new\n', 'logs_offset': 4, 'logs_reset': True,
        })

    def test_partial_utf8_character_is_deferred_until_complete(self):
        encoded = '😀'.encode('utf-8')
        self.path.write_bytes(b'event ' + encoded[:2])
        first = _read_logs(self.path, 0)
        self.assertEqual(first['logs'], 'event ')
        self.assertEqual(first['logs_offset'], 6)
        with self.path.open('ab') as handle:
            handle.write(encoded[2:] + b'\n')
        self.assertEqual(_read_logs(self.path, first['logs_offset'])['logs'], '😀\n')

    def test_initial_tail_does_not_split_utf8_or_limit_explicit_delta(self):
        self.path.write_bytes('😀'.encode('utf-8') + b'x' * 499_999)
        tail = _read_logs(self.path)
        self.assertEqual(tail['logs'], 'x' * 499_999)
        self.assertEqual(tail['logs_offset'], 500_003)
        self.assertEqual(_read_logs(self.path, 0)['logs'], '😀' + 'x' * 499_999)

    def test_invalid_offsets_and_missing_job(self):
        for offset in ('-1', 'abc', '1.5', ''):
            with self.subTest(offset=offset):
                self.assertEqual(self.client.get(f'/api/jobs/job?logs_offset={offset}').status_code, 400)
        self.assertEqual(self.client.get('/api/jobs/missing?logs_offset=0').status_code, 404)
