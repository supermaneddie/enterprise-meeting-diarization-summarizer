import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask

from transcription.artifacts import read_timeline
from transcription.routes import _job_content
from transcription.settings import Settings


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'config.yml'
        self.config.write_text('data_dir: .\n')
        self.settings = Settings.load(self.config)
        self.job_dir = self.settings.job_dir('job')
        self.job_dir.mkdir(parents=True)
        self.transcript = '## audio.mp3\n\n[Speaker 1] Hello.\n'
        (self.job_dir / 'results.txt').write_text(self.transcript)
        (self.job_dir / 'speakers.json').write_text(json.dumps({
            'labels': ['Speaker 1'], 'names': {'Speaker 1': 'Alice'},
        }))
        (self.job_dir / 'timeline.json').write_text(json.dumps([
            {'source': 'audio.mp3', 'start': 3662.125, 'text': '[Speaker 1] Hello.'},
        ]))

    def test_default_and_alignment(self):
        self.assertEqual(
            read_timeline(self.job_dir, self.transcript, {}),
            '## audio.mp3\n\n[01:01:02.125] [Speaker 1] Hello.\n',
        )
        self.assertEqual(read_timeline(self.job_dir, self.transcript + 'edited', {}), '')

    def test_configured_pattern_reaches_portal_and_survives_save(self):
        pattern = '{hours:02d}:{minutes:02d}:{seconds:02d}'
        self.config.write_text('timeline_pattern: ' + json.dumps(pattern) + '\n')
        settings = Settings.load(self.config)
        settings.save()
        settings = Settings.load(self.config)
        self.assertEqual(settings.timeline_pattern, pattern)
        app = Flask(__name__)
        app.config['SETTINGS'] = settings
        with app.app_context():
            content = _job_content('job')
        expected = '## audio.mp3\n\n[01:01:02] [Alice] Hello.\n'
        self.assertEqual(content['timeline'], expected)
        self.assertEqual(content['timeline_identifiers'], expected.replace('Alice', 'Alice - Speaker 1'))

    def test_invalid_patterns_make_timeline_unavailable(self):
        for pattern in ('{unknown}', '{', '{}', '{hours.missing}'):
            with self.subTest(pattern=pattern):
                self.assertEqual(read_timeline(self.job_dir, self.transcript, {}, pattern), '')
