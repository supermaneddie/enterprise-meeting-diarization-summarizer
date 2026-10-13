from urllib.parse import urljoin
import io
import json
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from flask import Flask
from pydub import AudioSegment

from transcription.db import JobStore
from transcription.settings import Settings
from transcription.routes import web
from transcription.runner import JobRunner
from transcription.realtime_stream import clean_text, SpeechGate, ModelStream, upstream_url, FRAME_BYTES, MAX_UTTERANCE_FRAMES


def wav(seconds=1, channels=1):
    data = io.BytesIO()
    with wave.open(data, 'wb') as audio:
        audio.setnchannels(channels); audio.setsampwidth(2); audio.setframerate(16000)
        audio.writeframes(b'\0\0' * int(seconds * 16000) * channels)
    data.seek(0)
    return data


class RealtimeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.settings = replace(Settings.load(Path(__file__).resolve().parents[1] / 'config.yml'),
                                data_dir=Path(temporary.name))
        self.settings.ensure_directories()
        self.store = JobStore(self.settings.database_path); self.store.initialize()
        self.runner = Mock(); self.runner.is_active.return_value = False
        app = Flask('transcription')
        app.config.update(TESTING=True, SECRET_KEY='test', SETTINGS=self.settings)
        app.extensions.update(job_store=self.store, job_runner=self.runner)
        app.register_blueprint(web)
        self.client = app.test_client()

    def save(self, **fields):
        data = {'name': 'Meeting', 'text': 'Live text', 'live_text': 'Live text',
                'audio': (wav(), 'audio.wav')}
        data.update(fields)
        return self.client.post('/realtime/save', data=data)

    def test_pages_and_stream_route(self):
        home = self.client.get('/').text
        self.assertLess(home.index('Start real-time transcript'), home.index('Analyze recordings'))
        page = self.client.get('/realtime')
        self.assertEqual(page.status_code, 200)
        self.assertIn('/realtime/stream', page.text)
        self.assertIn('id="record-final" type="checkbox" checked', page.text)
        self.assertEqual(self.client.post('/realtime/transcribe').status_code, 404)

    def test_save_preserves_all_artifacts_without_reprocessing(self):
        audio = wav().getvalue()
        response = self.save(text='Manual correction', audio=(io.BytesIO(audio), 'untrusted.wav'))
        self.assertEqual(response.status_code, 201)
        job_id = response.json['job_id']; job = self.store.get(job_id)
        self.assertEqual((job['kind'], job['status']), ('voice', 'completed'))
        self.assertTrue(job['config']['manual_edits'])
        directory = self.settings.job_dir(job_id)
        self.assertEqual((directory / 'results.txt').read_text(), 'Manual correction')
        self.assertEqual((directory / 'results-edited.txt').read_text(), 'Manual correction')
        self.assertEqual((directory / 'results-live.txt').read_text(), 'Live text')
        self.assertEqual(job['files'], ['microphone.mp3'])
        self.assertFalse((directory / 'audio/microphone.wav').exists())
        player = self.client.get(f'/jobs/{job_id}/audio/microphone.mp3')
        self.assertEqual(player.status_code, 200)
        self.assertEqual(player.mimetype, 'audio/mpeg')
        decoded = AudioSegment.from_file(io.BytesIO(player.data), format='mp3')
        self.assertAlmostEqual(decoded.duration_seconds, 1, places=2)
        self.assertEqual(decoded.channels, 1)
        player.close()
        self.assertEqual(self.client.get(urljoin('/realtime/save', response.json['url'])).status_code, 200)
        self.runner.submit.assert_not_called()
        self.runner.submit_final_transcription.assert_not_called()

    def test_save_queues_final_only_when_requested(self):
        response = self.save(final_transcription='true')
        self.runner.submit_final_transcription.assert_called_once_with(response.json['job_id'])

    def test_queue_failure_keeps_saved_recording(self):
        self.runner.submit_final_transcription.side_effect = RuntimeError('shutdown')
        with self.assertLogs('transcription', level='ERROR'):
            response = self.save(final_transcription='true')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.store.get(response.json['job_id'])['status'], 'failed')
        self.assertTrue((self.settings.job_dir(response.json['job_id']) / 'audio/microphone.mp3').exists())

    def test_encoding_failure_does_not_leave_job_or_audio(self):
        with patch('transcription.routes.AudioSegment.from_wav', side_effect=RuntimeError('encoder failed')):
            with self.assertLogs('transcription', level='ERROR'):
                response = self.save()
        self.assertEqual(response.status_code, 500)
        self.assertIn('MP3', response.json['error'])
        self.assertEqual(self.store.list(), [])
        self.assertEqual(list(self.settings.job_dir('dummy').parent.iterdir()), [])

    def test_partial_audio_only_can_be_saved(self):
        response = self.save(text='', live_text='', incomplete='true')
        job = self.store.get(response.json['job_id'])
        self.assertTrue(job['config']['transcription_incomplete'])
        self.assertIn('partial', job['current_step'])

    def test_invalid_save_does_not_leave_job_or_audio(self):
        for name, audio in (('', wav()), ('x' * 201, wav()), ('Meeting', io.BytesIO(b'broken')),
                            ('Stereo', wav(channels=2)), ('Truncated', io.BytesIO(wav().getvalue()[:-10]))):
            response = self.save(name=name, audio=(audio, 'audio.wav'))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(list(self.settings.job_dir('dummy').parent.iterdir()), [])

    def test_adopt_final_preserves_archived_corrections(self):
        job_id = self.save(text='Correction').json['job_id']
        directory = self.settings.job_dir(job_id)
        (directory / 'results-final.txt').write_text('Final text')
        response = self.client.post(f'/jobs/{job_id}/transcript', json={'action': 'use_final', 'transcript': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['transcript'], 'Final text')
        self.assertEqual((directory / 'results-edited.txt').read_text(), 'Correction')
        self.assertFalse(self.store.get(job_id)['config']['manual_edits'])

    def test_final_adoption_rejected_during_processing(self):
        job_id = self.save().json['job_id']
        self.store.update(job_id, status='running')
        response = self.client.post(f'/jobs/{job_id}/transcript', json={'action': 'use_final'})
        self.assertEqual(response.status_code, 409)

    def run_final(self, job_id, result='Final text', failure=None):
        runner = JobRunner(self.settings, self.store)
        self.addCleanup(runner.shutdown)
        event = Event(); runner.events[job_id] = event
        def process(state):
            self.assertNotEqual(Path(state['job_dir']), self.settings.job_dir(job_id))
            self.assertFalse(state['auto_analysis'])
            (Path(state['job_dir']) / 'results.txt').write_text('Partial staged output')
            if failure: raise failure
            return {'transcript': result}
        with patch('transcription.runner.TranscriptionPipeline') as pipeline:
            pipeline.return_value.run.side_effect = process
            runner._run_final_transcription(job_id, event)
        return self.store.get(job_id)

    def test_final_auto_adopts_only_unedited_transcript(self):
        for edited in (False, True):
            with self.subTest(edited=edited):
                original = 'Correction' if edited else 'Live text'
                job_id = self.save(text=original).json['job_id']
                job = self.run_final(job_id)
                directory = self.settings.job_dir(job_id)
                self.assertEqual(job['status'], 'completed')
                self.assertEqual((directory / 'results-final.txt').read_text(), 'Final text')
                self.assertEqual((directory / 'results.txt').read_text(), original if edited else 'Final text')
                self.assertEqual((directory / 'results-live.txt').read_text(), 'Live text')

    def test_duplicate_final_submission_is_rejected(self):
        job_id = self.save().json['job_id']
        runner = JobRunner(self.settings, self.store)
        self.addCleanup(runner.shutdown)
        with patch.object(runner.pool, 'submit') as submit:
            self.assertTrue(runner.submit_final_transcription(job_id))
            self.assertFalse(runner.submit_final_transcription(job_id))
            submit.assert_called_once()
        self.assertEqual(self.store.get(job_id)['status'], 'queued')
        runner.events.clear()

    def test_final_failure_and_cancel_preserve_saved_text(self):
        from transcription.pipeline import Cancelled
        for failure, status in ((RuntimeError('ASR offline'), 'failed'), (Cancelled('stop'), 'stopped')):
            job_id = self.save().json['job_id']
            job = self.run_final(job_id, failure=failure)
            self.assertEqual(job['status'], status)
            directory = self.settings.job_dir(job_id)
            self.assertEqual((directory / 'results.txt').read_text(), 'Live text')
            self.assertEqual(list(directory.glob('final-pass-*')), [])


class StreamingTests(unittest.TestCase):
    def test_cleanup_preserves_deliberate_repetition_and_partial_words(self):
        sentence = '今日去公園散步。'
        raw = 'language Chinese<asr_text>' + sentence * 2
        self.assertEqual(clean_text(raw), sentence * 2)
        self.assertEqual(clean_text(raw, final=True), sentence * 2)
        self.assertEqual(clean_text(raw + '今日'), sentence * 2 + '今日')

    def test_long_speech_is_split_without_replaying_or_losing_audio(self):
        vad = Mock()
        vad.is_speech.side_effect = [False] * 9 + [True] * 600
        gate = SpeechGate(vad)
        # Unique frames expose any accidental replay at session boundaries.
        pcm = b''.join(index.to_bytes(2, 'little') * (FRAME_BYTES // 2)
                       for index in range(609)) + b'\x01\x02'
        segments = []
        current = bytearray()
        def collect(output):
            for frame in output:
                if frame is None:
                    segments.append(bytes(current))
                    current.clear()
                else:
                    current.extend(frame)
        for offset in range(0, len(pcm), 3200):
            collect(gate.feed(pcm[offset:offset + 3200]))
        collect(gate.finish())
        self.assertEqual(b''.join(segments), pcm)
        self.assertEqual(len(segments), 3)
        self.assertTrue(all(len(segment) <= MAX_UTTERANCE_FRAMES * FRAME_BYTES
                            for segment in segments))
        self.assertLess(MAX_UTTERANCE_FRAMES * FRAME_BYTES, 5 * 32000)

    def test_completed_stream_ignores_late_text_and_closes_at_boundary(self):
        emitted = []
        model = ModelStream(Mock(), lambda **event: emitted.append(event))
        socket = model.socket = Mock()
        model.started = True
        socket.recv.side_effect = [
            json.dumps({'type': 'transcription.done', 'text': '今日去公園散步。'}),
            json.dumps({'type': 'transcription.delta', 'delta': '今日去公園散步。'}),
            json.dumps({'type': 'transcription.done', 'text': '今日去公園散步。'}),
            TimeoutError(),
        ]
        self.assertTrue(model.poll())
        self.assertFalse(model.poll())
        model.finish(Event())
        self.assertEqual(emitted, [{'type': 'segment', 'text': '今日去公園散步。'}])
        socket.close.assert_called_once()
        self.assertIsNone(model.socket)

    def test_clean_split_headers_and_multiple_segments(self):
        for length in range(1, len('language English<asr_text>')):
            self.assertEqual(clean_text('language English<asr_text>'[:length]), '')
        self.assertEqual(clean_text('language English<asr_'), '')
        self.assertEqual(clean_text('language English<asr_text>Hello'), 'Hello')
        self.assertEqual(clean_text('language English<asr_text>Hello language Chinese<asr_text>你好', True), 'Hello 你好')
        self.assertEqual(clean_text('language English<asr_text>Study language', True), 'Study language')

    def test_silence_is_not_sent(self):
        gate = SpeechGate()
        self.assertEqual(list(gate.feed(bytes(32000 * 6))), [])
        self.assertEqual(list(gate.finish()), [])

    def test_speech_includes_preroll_tail_and_flushes_short_frame(self):
        vad = Mock(); vad.is_speech.side_effect = [False] * 12 + [True] * 2 + [False] * 30
        gate = SpeechGate(vad)
        output = list(gate.feed(bytes(640 * 44)))
        self.assertEqual(len(output[0]), 6400)
        self.assertIsNone(output[-1])
        self.assertFalse(gate.active)
        vad.is_speech.side_effect = None; vad.is_speech.return_value = True
        self.assertEqual(list(gate.feed(bytes(100))), [])
        self.assertEqual(list(gate.finish()), [b'', bytes(100), None])

    @patch('transcription.realtime_stream.connect')
    def test_model_protocol_and_text_cleanup(self, connect):
        from types import SimpleNamespace
        settings = SimpleNamespace(asr_base_url='https://model/v1', asr_verify_ssl=True,
                                   asr_api_key='secret', asr_model='Qwen/ASR', asr_timeout_seconds=10)
        self.assertEqual(upstream_url(settings), 'wss://model/v1/realtime')
        socket = connect.return_value
        socket.recv.side_effect = [json.dumps({'type': 'session.created'}), TimeoutError(),
            json.dumps({'type': 'transcription.delta', 'delta': 'language English<asr_text>Hello'}),
            json.dumps({'type': 'transcription.done', 'text': 'language English<asr_text>Hello'})]
        emitted = []
        model = ModelStream(settings, lambda **event: emitted.append(event))
        model.open(); model.append(bytes(640)); model.finish(Event())
        sent = [json.loads(call.args[0]) for call in socket.send.call_args_list]
        self.assertEqual([item['type'] for item in sent], ['session.update', 'input_audio_buffer.commit', 'input_audio_buffer.append', 'input_audio_buffer.commit'])
        self.assertTrue(sent[-1]['final'])
        self.assertEqual(emitted[-1], {'type': 'segment', 'text': 'Hello'})
        self.assertEqual(connect.call_args.kwargs['additional_headers']['Authorization'], 'Bearer secret')
        socket.close.assert_called_once()
