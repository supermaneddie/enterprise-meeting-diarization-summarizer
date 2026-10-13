from __future__ import annotations

import hashlib
from pathlib import Path

from pydub import AudioSegment

from .formats import DEFAULT_SUPPORTED_FORMAT, media_suffixes


def inspect_audio_file(path: str, supported_format: str = DEFAULT_SUPPORTED_FORMAT) -> dict:
    """Read safe metadata for a media file used by the meeting graph."""
    source = Path(path).resolve(strict=True)
    if source.suffix.lower() not in media_suffixes(supported_format):
        raise ValueError("Unsupported media format")
    audio = AudioSegment.from_file(source)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "name": source.name,
        "bytes": source.stat().st_size,
        "duration_seconds": round(len(audio) / 1000, 3),
        "channels": audio.channels,
        "sample_rate": audio.frame_rate,
        "sha256": digest.hexdigest(),
    }


def list_audio_files(directory: str, supported_format: str = DEFAULT_SUPPORTED_FORMAT) -> list[dict]:
    """List supported media inputs and their metadata for a job directory."""
    root = Path(directory).resolve(strict=True)
    return [inspect_audio_file(str(path), supported_format) for path in sorted(root.iterdir())
            if path.is_file() and path.suffix.lower() in media_suffixes(supported_format)]

