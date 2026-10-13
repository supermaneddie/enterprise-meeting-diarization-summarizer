import unittest
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock

from flask import Flask, render_template

from transcription.pipeline import Cancelled, TranscriptionPipeline
from transcription.routes import _decorate, web
from transcription.runner import JobRunner
from transcription.settings import Settings


class UrlAudioTests(unittest.TestCase):
    def test_cleanup_on_success_failure_and_cancellation_preserves_artifacts(self):
        for audio_url, adjust_volume, prepared_audio, remove_audio in product(
            ('https://example.com/audio.mp3', ''), (False, True), (False, True), (False, True)
        ):
            for error in (None, Cancelled('Stopped'), RuntimeError('Failed')):
                with self.subTest(audio_url=audio_url, adjust_volume=adjust_volume, prepared_audio=prepared_audio, remove_audio=remove_audio, error=error), TemporaryDirectory() as root:
                    job_dir = Path(root)
                    for directory in ('uploads', 'audio', 'chunks', 'speaker-samples'):
                        path = job_dir / directory
                        path.mkdir()
                        (path / 'sample.mp3').write_bytes(b'audio')
                    (job_dir / 'audio' / 'sample.volume-adjusted.mp3').write_bytes(b'adjusted')
                    (job_dir / 'results.txt').write_text('Transcript')
                    pipeline = TranscriptionPipeline(Settings.load(Path(__file__).resolve().parents[1] / 'config.yml'), Mock(), Mock(), Event())

                    def updates(state, **kwargs):
                        if adjust_volume and prepared_audio:
                            state['audio_prepared'](
                                [job_dir / 'audio' / 'sample.mp3'],
                                [job_dir / 'audio' / 'sample.volume-adjusted.mp3'],
                                [job_dir / 'audio' / 'sample.volume-adjusted.mp3'],
                            )
                        if error:
                            raise error
                        yield {'transcribe': {'transcript': 'Transcript'}}

                    pipeline.graph = Mock(stream=updates)
                    state = {'job_dir': root, 'audio_url': audio_url, 'adjust_volume': adjust_volume, 'remove_audio_after_completed': remove_audio, 'audio_prepared': Mock()}
                    if error:
                        with self.assertRaises(type(error)):
                            pipeline.run(state)
                    else:
                        self.assertEqual(pipeline.run(state)['transcript'], 'Transcript')
                    retained = not remove_audio or error is not None
                    for directory in ('uploads', 'chunks'):
                        self.assertFalse((job_dir / directory).exists())
                    self.assertEqual((job_dir / 'audio').exists(), retained)
                    self.assertEqual(
                        (job_dir / 'audio' / 'sample.volume-adjusted.mp3').is_file(),
                        retained,
                    )
                    self.assertEqual((job_dir / 'audio' / 'sample.mp3').exists(), retained)
                    self.assertTrue((job_dir / 'speaker-samples' / 'sample.mp3').is_file())
                    self.assertEqual((job_dir / 'results.txt').read_text(), 'Transcript')

    def test_url_retry_downloads_again_despite_persisted_filenames(self):
        job = {'kind': 'voice', 'files': ['original.mp3', 'adjusted.mp3'], 'config': {
            'audio_url': 'https://example.com/audio.mp3', 'adjust_volume': True,
            'processing_files': ['adjusted.mp3'], 'adjusted_files': ['adjusted.mp3'],
        }}
        runner = JobRunner.__new__(JobRunner)
        self.assertEqual(runner._prepare_voice_files('job', job, Path('/unused'), Event(), Mock()), [])

    def test_page_offers_remote_original_and_local_adjusted_audio(self):
        app = Flask('transcription')
        app.secret_key = 'test'
        app.register_blueprint(web)
        url = 'https://example.com/audio.mp3?token=a&download=1'
        for audio_url, files in ((url, []), (url, ['original.mp3', 'adjusted.mp3']), ('', ['original.mp3'])):
            with self.subTest(audio_url=audio_url, files=files), app.test_request_context():
                job = _decorate(dict(
                    id='job', name='Meeting', kind='voice', status='completed',
                    files=files, config={'audio_url': audio_url, 'adjusted_files': ['adjusted.mp3'] if 'adjusted.mp3' in files else []},
                    started_at=None, finished_at=None, brief_description='', error=None,
                ))
                html = render_template('job.html', job=job, speakers=[], voiceprint_enabled=False,
                                       diarization_available=False, voiceprint_available=False,
                                       transcript='', summary='', logs='', logs_offset=0, timeline='',
                                       transcript_identifiers='', timeline_identifiers='')
                self.assertEqual(html.count('<audio '), 2 if audio_url and 'adjusted.mp3' in files else 1)
                if audio_url and 'adjusted.mp3' in files:
                    self.assertIn('src="/jobs/job/audio/adjusted.mp3"', html)
                    self.assertIn('Adjusted volume · used for processing', html)
                    self.assertNotIn('src="/jobs/job/audio/original.mp3"', html)
                expected = url.replace('&', '&amp;') if audio_url else '/jobs/job/audio/original.mp3'
                self.assertIn(f'src="{expected}"', html)
                self.assertIn("if(audioUrl) files=['Audio URL',...adjustedFiles];", html)
                self.assertIn("player.src=audioUrl && filename==='Audio URL' ? audioUrl : audioTemplate.replace", html)


if __name__ == '__main__':
    unittest.main()
