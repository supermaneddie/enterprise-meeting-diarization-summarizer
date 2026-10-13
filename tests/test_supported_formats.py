import io
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from pydub import AudioSegment
from werkzeug.datastructures import FileStorage

from transcription.files import (
    UploadError, allowed_filename, collect_audio_files, create_volume_adjusted_copies,
    download_url, save_uploads,
)
from transcription.formats import DEFAULT_SUPPORTED_FORMAT, allowed_suffixes
from transcription.mcp_tools import inspect_audio_file, list_audio_files
from transcription.settings import Settings


class SupportedFormatTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_settings_default_custom_and_round_trip(self):
        config = self.root / 'config.yml'
        config.write_text('{}')
        self.assertEqual(Settings.load(config).asr_supported_format, DEFAULT_SUPPORTED_FORMAT)
        config.write_text('asr:\n  supported_format: " wav, .FLAC,wav "\n')
        settings = Settings.load(config)
        self.assertEqual(settings.asr_supported_format, 'WAV,FLAC')
        settings.save()
        self.assertEqual(Settings.load(config).asr_supported_format, 'WAV,FLAC')
        for invalid in ('null', '[]', '""', '"WAV,,MP3"', '"../wav"'):
            with self.subTest(invalid=invalid):
                config.write_text(f'asr:\n  supported_format: {invalid}\n')
                with self.assertRaisesRegex(ValueError, 'asr.supported_format'):
                    Settings.load(config)

    def test_uploads_obey_config_and_preserve_archive_support(self):
        for suffix in allowed_suffixes():
            self.assertTrue(allowed_filename('recording' + suffix.upper()))
        for suffix in ('.wav', '.zip', '.tar', '.tar.gz'):
            saved = save_uploads([FileStorage(io.BytesIO(b'media'), filename='meeting' + suffix)],
                                 self.root, 'WAV')
            self.assertEqual(saved[0].suffix, Path(suffix).suffix or suffix)
        with self.assertRaises(UploadError):
            save_uploads([FileStorage(io.BytesIO(b'media'), filename='meeting.mp3')], self.root, 'WAV')
        self.assertFalse(allowed_filename('meeting.wav.exe'))

    def archive(self, kind, entries):
        path = self.root / ('recordings.' + kind)
        if kind == 'zip':
            with zipfile.ZipFile(path, 'w') as archive:
                for name, content in entries:
                    archive.writestr(name, content)
        else:
            with tarfile.open(path, 'w:gz' if kind == 'tar.gz' else 'w') as archive:
                for name, content in entries:
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
        return path

    def test_archive_filtering_and_limits(self):
        for kind in ('zip', 'tar', 'tar.gz'):
            with self.subTest(kind=kind):
                archive = self.archive(kind, [('nested/a.WAV', b'wav'), ('b.flac', b'flac'),
                                               ('c.mp3', b'mp3'), ('notes.txt', b'notes')])
                output = collect_audio_files([archive], self.root / kind, 10, 100, 'WAV,FLAC')
                self.assertEqual([p.name for p in output], ['a.WAV', 'b.flac'])
                with self.assertRaisesRegex(UploadError, 'too many'):
                    collect_audio_files([archive], self.root / 'limited', 1, 100, 'WAV,FLAC')
                with self.assertRaisesRegex(UploadError, 'size limit'):
                    collect_audio_files([archive], self.root / 'small', 10, 2, 'WAV')
                unsafe = self.archive(kind, [('../escape.wav', b'wav')])
                with self.assertRaisesRegex(UploadError, 'Unsafe archive member'):
                    collect_audio_files([unsafe], self.root / 'unsafe', 10, 100, 'WAV')

    def test_url_filename_and_mime_respect_config(self):
        for url, headers, expected in (
            ('https://example.com/audio.WAV', {}, 'audio.WAV'),
            ('https://example.com/token', {'content-disposition': 'attachment; filename="audio.flac"'}, 'audio.flac'),
            ('https://example.com/token', {'content-type': 'audio/wav'}, 'download.wav'),
            ('https://example.com/token', {'content-type': 'video/webm'}, 'download.webm'),
        ):
            with self.subTest(url=url, headers=headers):
                response = httpx.Response(200, headers=headers, content=b'media', request=httpx.Request('GET', url))
                with patch('transcription.files.httpx.stream') as stream:
                    stream.return_value.__enter__.return_value = response
                    saved = download_url(url, self.root / expected, 100)
                self.assertEqual(saved.name, expected)
        response = httpx.Response(200, headers={'content-type': 'audio/mpeg'}, content=b'media',
                                  request=httpx.Request('GET', 'https://example.com/audio.mp3'))
        with patch('transcription.files.httpx.stream') as stream:
            stream.return_value.__enter__.return_value = response
            with self.assertRaises(UploadError):
                download_url(str(response.url), self.root / 'rejected', 100, supported_format='WAV')

    def test_real_wav_inspection_and_volume_output(self):
        source = self.root / 'meeting.WAV'
        AudioSegment.silent(duration=250).export(source, format='wav').close()
        output = collect_audio_files([source], self.root / 'audio', 10, 100000, 'WAV')
        self.assertEqual(inspect_audio_file(str(output[0]), 'WAV')['duration_seconds'], 0.25)
        self.assertEqual(len(list_audio_files(str(self.root / 'audio'), 'WAV')), 1)
        adjusted = create_volume_adjusted_copies(output)
        self.assertEqual(adjusted[0].name, 'meeting.volume-adjusted.mp3')
        self.assertEqual(inspect_audio_file(str(adjusted[0]))['duration_seconds'], 0.25)
