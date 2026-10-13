"""Configured media extensions and the always-supported archive containers."""
import re

DEFAULT_SUPPORTED_FORMAT = "WAV,MP3,FLAC,OGG,M4A,AAC,MP4,MPEG,MPGA,WEBM"
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz")


def media_suffixes(value: str = DEFAULT_SUPPORTED_FORMAT) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("asr.supported_format must be a comma-separated string")
    formats = tuple(dict.fromkeys(part.strip().lower().removeprefix(".") for part in value.split(",")))
    if not formats or any(not re.fullmatch(r"[a-z0-9]+", part) or "." + part in ARCHIVE_SUFFIXES for part in formats):
        raise ValueError("asr.supported_format must contain media extensions separated by commas")
    return tuple("." + part for part in formats)


def allowed_suffixes(value: str = DEFAULT_SUPPORTED_FORMAT) -> tuple[str, ...]:
    return media_suffixes(value) + ARCHIVE_SUFFIXES
