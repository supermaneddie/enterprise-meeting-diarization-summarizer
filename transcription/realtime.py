"""Validation of microphone WAV recordings before job persistence."""
from pathlib import Path
import wave


def validate_recording(path: Path, max_seconds: float | None = None) -> float:
    try:
        with wave.open(str(path), 'rb') as audio:
            frames = audio.getnframes()
            rate = audio.getframerate()
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                    or not 8000 <= rate <= 96000 or frames <= 0):
                raise ValueError('Expected nonempty mono 16-bit PCM WAV audio')
            duration = frames / rate
            if max_seconds is not None and duration > max_seconds:
                raise ValueError('Audio chunk must be at most 10 seconds')
            # Check truncation without loading a whole recording into memory.
            remaining = frames * 2
            while remaining:
                data = audio.readframes(min(65536, remaining // 2))
                if not data:
                    raise ValueError('Incomplete WAV audio')
                remaining -= len(data)
            return duration
    except (wave.Error, EOFError) as exc:
        raise ValueError('Invalid PCM WAV audio') from exc
