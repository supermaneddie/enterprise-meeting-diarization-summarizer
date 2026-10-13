import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydub import AudioSegment

from transcription.pipeline import Cancelled, TranscriptionPipeline
from transcription.settings import Settings


class VadStrategyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'config.yml'
        self.config.write_text('{}')
        self.settings = Settings.load(self.config)
        self.pipeline = TranscriptionPipeline(self.settings, Mock(), Mock(), Event())

    def test_config_default_validation_and_round_trip(self):
        self.assertEqual(self.settings.asr_vad_strategy, 'energy-based')
        for strategy in ('energy-based', 'silero-vad'):
            self.config.write_text(f'asr:\n  vad-strategy: {strategy}\n')
            settings = Settings.load(self.config)
            self.assertEqual(settings.asr_vad_strategy, strategy)
            settings.save()
            self.assertEqual(Settings.load(self.config).asr_vad_strategy, strategy)
        for value in ('unknown', 'null', '[]'):
            self.config.write_text(f'asr:\n  vad-strategy: {value}\n')
            with self.assertRaisesRegex(ValueError, 'asr.vad-strategy'):
                Settings.load(self.config)

    def test_split_dispatch_and_export(self):
        audio = AudioSegment.silent(duration=2500, frame_rate=16000)
        for strategy, helper in (('energy-based', '_energy_ranges'), ('silero-vad', '_silero_ranges')):
            self.pipeline.settings = replace(self.settings, asr_vad_strategy=strategy)
            with patch('transcription.pipeline.AudioSegment.from_file', return_value=audio), \
                 patch.object(self.pipeline, helper, return_value=[(0, 1500), (1500, 2500)]) as split:
                paths = self.pipeline._split_audio(self.root / 'input.mp3', self.root / strategy)
            split.assert_called_once_with(audio, 30000, 60000)
            self.assertEqual([len(AudioSegment.from_wav(path)) for path in paths], [1500, 1000])

    def test_silero_pause_boundaries_preserve_timeline(self):
        vad = SimpleNamespace(
            load_silero_vad=Mock(),
            get_speech_timestamps=Mock(return_value=[
                {'start': 0, 'end': 40000 * 16},
                {'start': 44000 * 16, 'end': 130000 * 16},
            ]),
        )
        tensor = Mock()
        tensor.__truediv__ = Mock(return_value=range(130000 * 16))
        torch = SimpleNamespace(tensor=Mock(return_value=tensor), float32=object())
        audio = AudioSegment.silent(duration=130000, frame_rate=16000)
        with patch.dict(sys.modules, {'silero_vad': vad, 'torch': torch}):
            ranges = self.pipeline._silero_ranges(audio, 30000, 60000)
        self.assertEqual(ranges, [(0, 42000), (42000, 102000), (102000, 130000)])
        vad.load_silero_vad.assert_called_once_with(onnx=True)
        kwargs = vad.get_speech_timestamps.call_args.kwargs
        self.assertFalse(kwargs['return_seconds'])
        self.pipeline.cancel_event.set()
        with self.assertRaises(Cancelled):
            kwargs['progress_tracking_callback'](50)

    def test_missing_silero_is_actionable_and_energy_still_works(self):
        audio = AudioSegment.silent(duration=1000, frame_rate=16000)
        with patch.dict(sys.modules, {'silero_vad': None}):
            with self.assertRaisesRegex(RuntimeError, 'requirements-asr.txt'):
                self.pipeline._silero_ranges(audio, 30000, 60000)
            self.assertEqual(self.pipeline._energy_ranges(audio, 30000, 60000), [(0, 1000)])

    def test_hard_limit_and_full_coverage_without_pauses(self):
        for minimum in (30000, 90000):
            self.assertEqual(
                self.pipeline._ranges_from_markers(130000, [], minimum, 60000),
                [(0, 60000), (60000, 120000), (120000, 130000)],
            )
        self.assertEqual(self.pipeline._ranges_from_markers(0, [], 30000, 60000), [])


if __name__ == '__main__':
    unittest.main()
