import json
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from transcription.pipeline import Cancelled, TranscriptionPipeline
from transcription.settings import Settings


class ChunkTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config = self.root / 'config.yml'
        config.write_text('{}')
        self.settings = replace(Settings.load(config), asr_concurrency=1, asr_max_retries=0)
        self.pipeline = TranscriptionPipeline(self.settings, Mock(), Mock(), Event())
        self.client = Mock()
        self.client.audio.transcriptions.create.return_value = 'hello'
        self.pipeline._client = Mock(return_value=self.client)

    def chunks(self, *durations):
        paths = []
        for i, duration in enumerate(durations):
            path = self.root / f'{i}.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\0\0' * round(duration * 16000))
            paths.append(path)
        return paths

    def test_config(self):
        self.assertEqual(self.settings.asr_ignore_duration_seconds, 1)
        for value in (0, .25, 2):
            self.settings.config_path.write_text(f'asr:\n  ignore_duration_seconds: {value}\n')
            settings = Settings.load(self.settings.config_path)
            settings.save()
            self.assertEqual(Settings.load(settings.config_path).asr_ignore_duration_seconds, value)
        for value in ('-1', '.nan', '.inf'):
            self.settings.config_path.write_text(f'asr:\n  ignore_duration_seconds: {value}\n')
            with self.assertRaises(ValueError):
                Settings.load(self.settings.config_path)

    def test_boundary_and_disabled_filter(self):
        callback = Mock()
        self.assertEqual(self.pipeline._transcribe_batch(self.chunks(0, .999, 1), callback), ['', '', 'hello'])
        self.client.audio.transcriptions.create.assert_called_once()
        self.assertEqual([c.args for c in callback.call_args_list], [(0, ''), (1, ''), (2, 'hello')])
        self.pipeline.settings = replace(self.settings, asr_ignore_duration_seconds=0)
        self.assertEqual(self.pipeline._transcribe_batch(self.chunks(.1)), ['hello'])

    def test_retry_failure_continues(self):
        self.pipeline.settings = replace(self.settings, asr_max_retries=1)
        self.client.audio.transcriptions.create.side_effect = [TimeoutError(), TimeoutError(), 'next']
        self.assertEqual(self.pipeline._transcribe_batch(self.chunks(1, 1)), ['[Transcription Failed]', 'next'])
        self.assertEqual(self.client.audio.transcriptions.create.call_count, 3)

    def test_hard_deadline_continues_before_stalled_request_returns(self):
        release = Event()
        finished = Event()
        def stalled(**kwargs):
            release.wait(5)
            finished.set()
            return 'late'
        self.client.audio.transcriptions.create.side_effect = stalled
        next_client = Mock()
        next_client.audio.transcriptions.create.return_value = 'next'
        self.pipeline._client = Mock(side_effect=[self.client, next_client])
        self.pipeline.settings = replace(self.settings, asr_chunk_deadline_seconds=.05)
        callback = Mock()
        try:
            self.assertEqual(self.pipeline._transcribe_batch(self.chunks(1, 1), callback), ['[Transcription Failed]', 'next'])
            self.assertFalse(finished.is_set())
            self.assertEqual(callback.call_count, 2)
        finally:
            release.set()
            finished.wait(2)

    def test_cancellation(self):
        self.client.audio.transcriptions.create.side_effect = Cancelled('Stopped')
        with self.assertRaises(Cancelled):
            self.pipeline._transcribe_batch(self.chunks(1))

    def test_diarized_skip_omits_speaker_and_timeline(self):
        self.pipeline.settings = replace(self.settings, diarization_enabled=True, voiceprint_enabled=False)
        chunks = self.chunks(.1, 1)
        self.pipeline._preserve_clearest_speaker_samples = Mock()
        with patch('transcription.pipeline.create_speaker_chunks', return_value=[(chunks[0], 's', 0), (chunks[1], 's', 2)]):
            result = self.pipeline._transcribe_on_device({'job_dir': str(self.root), 'audio_files': ['meeting.mp3']})
        self.assertEqual(result['transcript'], '[Speaker 1] hello')
        timeline = json.loads((self.root / 'timeline.json').read_text())
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]['start'], 2)

    def test_all_skipped(self):
        self.pipeline._split_audio = Mock(return_value=self.chunks(.1))
        result = self.pipeline._transcribe_on_device({'job_dir': str(self.root), 'audio_files': ['meeting.mp3']})
        self.assertEqual(result['transcript'], '')
        self.pipeline._client.assert_not_called()
