"""Bounded, same-origin microphone relay to vLLM's realtime API.

Each browser connection is one capture interval. Speech detection terminates
utterances on silence; the original audio is retained independently by the UI.
"""
from __future__ import annotations

import base64
from collections import deque
import json
import logging
from queue import Queue, Empty, Full
import re
import ssl
from threading import Event, Lock, Thread
import time
from urllib.parse import urlsplit, urlunsplit

import webrtcvad
from websockets.sync.client import connect


FRAME_BYTES = 640  # 20 ms, mono PCM16 at 16 kHz
# Keep each upstream session below Qwen's 5 s internal chunk boundary.
# The deployed realtime backend can repeat its last chunk during finalization.
MAX_UTTERANCE_FRAMES = 240  # 4.8 s, INCLUDING pre-roll and trailing silence


def clean_text(raw: str, final: bool = False) -> str:
    # Qwen emits a language header for each internal audio segment.
    if not final and '<asr_text>' not in raw and ('language'.startswith(raw) or raw.startswith('language')):
        return ''
    text = re.sub(r"language\s+[^<\r\n]{1,80}<asr_text>", " ", raw)
    text = re.sub(r"<\|[^>]*\|>|<asr_text>", "", text)
    if not final:
        text = re.sub(r"(?:language(?:\s+[^<\r\n]{0,80})?(?:<[^>]*)?|<[^>]*)$", "", text)
        if '<asr_text>' in raw:
            text = re.sub(r'(?<!\w)(?:l|la|lan|lang|langu|langua|languag)$', '', text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


class SpeechGate:
    """20 ms WebRTC VAD with 200 ms pre-roll and 600 ms trailing silence."""
    def __init__(self, vad=None):
        self.vad = vad or webrtcvad.Vad(1)
        self.buffer = bytearray()
        self.pre = deque(maxlen=10)
        self.active = False
        self.quiet = 0
        self.frames = 0

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= FRAME_BYTES:
            frame = bytes(self.buffer[:FRAME_BYTES])
            del self.buffer[:FRAME_BYTES]
            voiced = self.vad.is_speech(frame, 16000)
            if not self.active:
                self.pre.append(frame)
                if not voiced:
                    continue
                self.active = True
                self.frames = len(self.pre)
                yield b''.join(self.pre)
                self.pre.clear()
            else:
                self.frames += 1
                yield frame
            self.quiet = 0 if voiced else self.quiet + 1
            if self.quiet >= 30 or self.frames >= MAX_UTTERANCE_FRAMES:
                yield None  # End at a pause or before the upstream internal chunk boundary.
                self.active = False
                self.quiet = self.frames = 0

    def finish(self):
        if self.buffer:
            # Preserve a short final speech frame; padding is for VAD only.
            remainder = bytes(self.buffer)
            self.buffer.clear()
            if self.active or self.vad.is_speech(remainder.ljust(FRAME_BYTES, b'\0'), 16000):
                if not self.active:
                    yield b''.join(self.pre)
                    self.pre.clear()
                yield remainder
                self.active = True
        if self.active:
            yield None
            self.active = False


def upstream_url(settings):
    parts = urlsplit(settings.asr_base_url)
    if parts.scheme not in {'http', 'https'} or not parts.netloc:
        raise ValueError('Invalid ASR server URL')
    return urlunsplit(('wss' if parts.scheme == 'https' else 'ws', parts.netloc,
                       parts.path.rstrip('/') + '/realtime', '', ''))


class ModelStream:
    def __init__(self, settings, emit):
        self.settings = settings
        self.emit = emit
        self.socket = None
        self.started = False
        self.raw = ''

    def open(self):
        url = upstream_url(self.settings)
        options = dict(open_timeout=10, close_timeout=2, proxy=None, max_size=2**20)
        if self.settings.asr_api_key:
            options['additional_headers'] = {'Authorization': 'Bearer ' + self.settings.asr_api_key}
        if url.startswith('wss:'):
            options['ssl'] = (ssl.create_default_context() if self.settings.asr_verify_ssl
                              else ssl._create_unverified_context())
        self.socket = connect(url, **options)
        event = json.loads(self.socket.recv(timeout=10))
        if event.get('type') != 'session.created':
            raise RuntimeError('Model did not create a realtime session')
        self.socket.send(json.dumps({'type': 'session.update', 'model': self.settings.asr_model}))

    def append(self, data):
        if not data:
            return
        if self.socket is None:
            self.open()
        if not self.started:
            self.socket.send(json.dumps({'type': 'input_audio_buffer.commit'}))
            self.started = True
            self.raw = ''
        self.socket.send(json.dumps({'type': 'input_audio_buffer.append',
                                     'audio': base64.b64encode(data).decode('ascii')}))
        self.poll()

    def poll(self, timeout=0):
        if self.socket is None:
            return False
        while True:
            try:
                event = json.loads(self.socket.recv(timeout=timeout))
            except TimeoutError:
                return False
            timeout = 0
            kind = event.get('type')
            if kind == 'error':
                raise RuntimeError('Model reported a realtime error')
            if not self.started:
                continue  # Late text cannot create another segment after completion.
            if kind == 'transcription.delta':
                self.raw += event.get('delta', '')
                if len(self.raw) > 100_000:
                    raise RuntimeError('Model response exceeded the segment limit')
                self.emit(type='partial', text=clean_text(self.raw))
            elif kind == 'transcription.done':
                self.emit(type='segment', text=clean_text(event.get('text', self.raw), final=True))
                self.started = False
                self.raw = ''
                return True

    def finish(self, stopped):
        if not self.started:
            self.close()
            return
        self.socket.send(json.dumps({'type': 'input_audio_buffer.commit', 'final': True}))
        deadline = time.monotonic() + min(self.settings.asr_timeout_seconds, 60)
        while not stopped.is_set():
            if self.poll(timeout=0.2):
                self.close()
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('Model finalization timed out')

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.started = False
        self.raw = ''


def relay(browser, settings):
    pending = Queue(maxsize=100)  # At most 100 browser frames, never an unbounded stream.
    stopped = Event()
    sending = Lock()

    def emit(**event):
        with sending:
            browser.send(json.dumps(event, ensure_ascii=False))

    def worker():
        model = ModelStream(settings, emit)
        gate = SpeechGate()
        try:
            model.open()
            emit(type='ready')
            while not stopped.is_set():
                try:
                    data = pending.get(timeout=0.1)
                except Empty:
                    model.poll()
                    continue
                if data is None:
                    for frame in gate.finish():
                        model.finish(stopped) if frame is None else model.append(frame)
                    emit(type='done')
                    return
                for frame in gate.feed(data):
                    if stopped.is_set():
                        return
                    model.finish(stopped) if frame is None else model.append(frame)
                emit(type='ack', bytes=len(data))
        except Exception:
            logging.getLogger(__name__).exception('Realtime model relay failed')
            try:
                emit(type='error', message='Live transcription failed. Audio is retained; save it for final transcription or reconnect.')
            except Exception:
                pass
        finally:
            stopped.set()
            model.close()

    thread = Thread(target=worker, daemon=True, name='realtime-model')
    thread.start()
    last_message = time.monotonic()
    finishing = False
    received = 0
    try:
        while not stopped.is_set():
            message = browser.receive(timeout=0.5)
            if message is None:
                if time.monotonic() - last_message > (75 if finishing else 30):
                    raise TimeoutError('Recording connection timed out')
                continue
            last_message = time.monotonic()
            if finishing:
                raise ValueError('Audio received after finalization')
            if isinstance(message, bytes):
                if not message or len(message) % 2 or len(message) > 8192:
                    raise ValueError('Expected a PCM16 frame of at most 8192 bytes')
                received += len(message)
                if received > min(settings.max_upload_bytes, 128 * 1024 * 1024):
                    raise ValueError('Recording limit reached; save this recording')
                pending.put_nowait(message)
            elif json.loads(message) == {'type': 'finish'}:
                pending.put_nowait(None)
                finishing = True
            else:
                raise ValueError('Unsupported recording command')
    except Full:
        emit(type='error', message='Transcription fell behind. Audio is retained; save or reconnect.')
    except (ValueError, TimeoutError) as exc:
        emit(type='error', message=str(exc))
    finally:
        stopped.set()
        thread.join(timeout=3)
