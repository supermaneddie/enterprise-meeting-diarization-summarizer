from __future__ import annotations

from email.message import Message
import math
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import unquote, urlparse

import httpx
from pydub import AudioSegment
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from .formats import DEFAULT_SUPPORTED_FORMAT, allowed_suffixes, media_suffixes


class UploadError(ValueError):
    pass


VOLUME_OPTIONS = ("auto", *(f"+{value}%" for value in range(10, 101, 10)),
                  *(f"-{value}%" for value in range(10, 101, 10)))


def validate_volume(value: object) -> str:
    volume = str(value).strip().lower()
    if volume not in VOLUME_OPTIONS:
        raise UploadError("Volume must be Auto or a percentage from +10% to +100% or -10% to -100% in steps of 10%")
    return volume


def allowed_filename(name: str, supported_format: str = DEFAULT_SUPPORTED_FORMAT) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in allowed_suffixes(supported_format))


def save_uploads(files: list[FileStorage], upload_dir: Path, supported_format: str = DEFAULT_SUPPORTED_FORMAT) -> list[Path]:
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for index, storage in enumerate(files):
        if not storage.filename or not allowed_filename(storage.filename, supported_format):
            raise UploadError(f"Unsupported file: {storage.filename or '(unnamed)'}")
        name = secure_filename(storage.filename)
        if not allowed_filename(name, supported_format):
            suffix = next(s for s in sorted(allowed_suffixes(supported_format), key=len, reverse=True)
                          if storage.filename.lower().endswith(s))
            name = f"upload-{index}{suffix}"
        target = _unique_path(upload_dir / name)
        storage.save(target)
        saved.append(target)
    if not saved:
        raise UploadError("At least one supported media file or archive is required")
    return saved


def download_url(
    url: str,
    upload_dir: Path,
    max_bytes: int,
    progress: Callable[[int], None] | None = None,
    supported_format: str = DEFAULT_SUPPORTED_FORMAT,
) -> Path:
    """Stream one supported remote input into the upload staging directory."""
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UploadError("Audio URL must be a valid HTTP or HTTPS URL")

    upload_dir.mkdir(parents=True, exist_ok=True)
    target: Path | None = None
    try:
        with httpx.stream(
            "GET", value, follow_redirects=True, timeout=httpx.Timeout(30.0)
        ) as response:
            response.raise_for_status()
            filename = _download_filename(response, parsed, supported_format)
            if not allowed_filename(filename, supported_format):
                raise UploadError(
                    "Audio URL must point to a supported media file or archive"
                )
            content_length = response.headers.get("content-length")
            total_bytes: int | None = None
            if content_length:
                try:
                    total_bytes = int(content_length)
                    if total_bytes > max_bytes:
                        raise UploadError("URL download exceeds MAX_UPLOAD_MB")
                except ValueError:
                    total_bytes = None
            safe_name = secure_filename(filename)
            if not allowed_filename(safe_name, supported_format):
                suffix = next(
                    suffix
                    for suffix in sorted(allowed_suffixes(supported_format), key=len, reverse=True)
                    if filename.lower().endswith(suffix)
                )
                safe_name = f"download{suffix}"
            target = _unique_path(upload_dir / safe_name)
            downloaded = 0
            last_percentage: int | None = None

            def report_percentage(force: int | None = None) -> None:
                nonlocal last_percentage
                if progress is None:
                    return
                if force is None:
                    if not total_bytes or total_bytes <= 0:
                        return
                    percentage = min(100, int(downloaded * 100 / total_bytes))
                else:
                    percentage = force
                if percentage != last_percentage:
                    progress(percentage)
                    last_percentage = percentage

            report_percentage(0)
            with target.open("wb") as output:
                for block in response.iter_bytes(1024 * 1024):
                    downloaded += len(block)
                    if downloaded > max_bytes:
                        raise UploadError("URL download exceeds MAX_UPLOAD_MB")
                    output.write(block)
                    report_percentage()
            report_percentage(100)
    except UploadError:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    except (httpx.HTTPError, OSError) as exc:
        if target is not None:
            target.unlink(missing_ok=True)
        raise UploadError(f"Could not download audio URL: {exc}") from exc
    except Exception:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    if target is None:
        raise UploadError("Could not download audio URL")
    return target


def _download_filename(response: httpx.Response, original_url, supported_format: str = DEFAULT_SUPPORTED_FORMAT) -> str:
    disposition = response.headers.get("content-disposition", "")
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        filename = message.get_filename()
        if filename and allowed_filename(filename, supported_format):
            return Path(filename).name

    response_name = Path(unquote(response.url.path)).name
    original_name = Path(unquote(original_url.path)).name
    for filename in (response_name, original_name):
        if filename and allowed_filename(filename, supported_format):
            return filename
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    mime_formats = {
        "audio/mpeg": ("mp3", "mpga", "mpeg"), "audio/mp3": ("mp3",),
        "audio/wav": ("wav",), "audio/x-wav": ("wav",), "audio/wave": ("wav",),
        "audio/flac": ("flac",), "audio/x-flac": ("flac",),
        "audio/ogg": ("ogg",), "application/ogg": ("ogg",),
        "audio/mp4": ("m4a", "mp4"), "audio/x-m4a": ("m4a",),
        "audio/aac": ("aac",), "video/mp4": ("mp4",),
        "video/mpeg": ("mpeg",), "video/webm": ("webm",), "audio/webm": ("webm",),
        "application/zip": ("zip",), "application/x-tar": ("tar",),
        "application/gzip": ("tar.gz",),
    }
    for extension in mime_formats.get(content_type, ()):
        filename = f"download.{extension}"
        if allowed_filename(filename, supported_format):
            return filename
    return "download"


def collect_audio_files(saved: list[Path], audio_dir: Path, max_files: int, max_bytes: int,
                        supported_format: str = DEFAULT_SUPPORTED_FORMAT) -> list[Path]:
    suffixes = media_suffixes(supported_format)
    audio_dir.mkdir(parents=True, exist_ok=True)
    output: list[Path] = []
    total = 0

    def add_file(source, display_name: str, size: int) -> None:
        nonlocal total
        if size < 0 or total + size > max_bytes:
            raise UploadError("Archive exceeds the extracted-size limit")
        if len(output) >= max_files:
            raise UploadError("Archive contains too many media files")
        target = _unique_path(audio_dir / secure_filename(Path(display_name).name))
        written = 0
        try:
            with source as src, target.open("wb") as dst:
                while block := src.read(1024 * 1024):
                    written += len(block)
                    if total + written > max_bytes:
                        raise UploadError("Archive exceeds the extracted-size limit")
                    dst.write(block)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        total += written
        output.append(target)

    for path in saved:
        lower = path.name.lower()
        if lower.endswith(suffixes):
            add_file(path.open("rb"), path.name, path.stat().st_size)
        elif lower.endswith(".zip"):
            try:
                with zipfile.ZipFile(path) as archive:
                    members = [m for m in archive.infolist() if not m.is_dir() and m.filename.lower().endswith(suffixes)]
                    for member in members:
                        _validate_member(member.filename)
                        add_file(archive.open(member), member.filename, member.file_size)
            except (zipfile.BadZipFile, RuntimeError) as exc:
                raise UploadError(f"Invalid ZIP archive: {path.name}") from exc
        elif lower.endswith((".tar", ".tar.gz")):
            try:
                with tarfile.open(path, mode="r:*") as archive:
                    for member in archive.getmembers():
                        if member.isfile() and member.name.lower().endswith(suffixes):
                            _validate_member(member.name)
                            extracted = archive.extractfile(member)
                            if extracted is not None:
                                add_file(extracted, member.name, member.size)
            except (tarfile.TarError, OSError) as exc:
                raise UploadError(f"Invalid TAR archive: {path.name}") from exc
        else:
            raise UploadError(f"Unsupported file: {path.name}")
    if not output:
        raise UploadError("No supported media files were found")
    return output


def create_volume_adjusted_copies(
    audio_files: list[Path], target_peak_dbfs: float = -1.0, *, volume: str = "auto"
) -> list[Path]:
    """Create normalized or percentage-scaled copies beside the originals."""
    volume = validate_volume(volume)
    adjusted: list[Path] = []
    for source in audio_files:
        target = _unique_path(
            source.with_name(f"{source.stem}.volume-adjusted.mp3")
        )
        temporary = target.with_name(f"{target.stem}.tmp{target.suffix}")
        try:
            audio = AudioSegment.from_file(source)
            if volume == "auto":
                if audio.rms and audio.max_dBFS != float("-inf"):
                    audio = audio.apply_gain(target_peak_dbfs - audio.max_dBFS)
            else:
                factor = 1 + int(volume[:-1]) / 100
                gain = 20 * math.log10(factor) if factor > 0 else float("-inf")
                audio = audio.apply_gain(gain)
            audio.export(temporary, format="mp3")
            temporary.replace(target)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise UploadError(
                f"Could not adjust volume for {source.name}. Ensure FFmpeg is installed: {exc}"
            ) from exc
        adjusted.append(target)
    return adjusted


def create_speaker_adjusted_copy(
    source: Path,
    chunks: list[tuple[Path, str, float]],
    check_cancelled: Callable[[], None],
    log,
) -> Path:
    """Level diarized WAV chunks and rebuild playback without moving timestamps.

    Aim for -20 dBFS RMS, with at most 20 dB boost and a -1 dBFS
    peak ceiling. Boundary padding overlaps are included only once.
    """
    audio = AudioSegment.from_file(source)
    target = _unique_path(source.with_name(f"{source.stem}.volume-adjusted.mp3"))
    temporary = target.with_name(f"{target.stem}.tmp{target.suffix}")
    pieces = []
    cursor = 0
    try:
        for index, (path, _speaker, start) in enumerate(chunks):
            check_cancelled()
            chunk = AudioSegment.from_wav(path)
            start_ms = max(0, round((start - 0.12) * 1000))
            end_ms = min(len(audio), start_ms + len(chunk))
            # Chunks include 120 ms ASR padding; do not duplicate overlaps.
            if index + 1 < len(chunks):
                next_start = max(0, round((chunks[index + 1][2] - 0.12) * 1000))
                end_ms = min(end_ms, next_start)
            gain = 0.0
            if chunk.rms:
                gain = min(-20.0 - chunk.dBFS, 20.0, -1.0 - chunk.max_dBFS)
                # Also protect the original-channel playback samples from clipping.
                region = audio[start_ms:end_ms]
                if region.rms:
                    gain = min(gain, -1.0 - region.max_dBFS)
            chunk.apply_gain(gain).export(path, format="wav").close()
            log("volume", f"{path.name}: applied {gain:+.1f} dB (target -20 dBFS RMS)")
            if start_ms > cursor:
                pieces.append(audio[cursor:start_ms])
            if end_ms > start_ms:
                pieces.append(audio[start_ms:end_ms].apply_gain(gain))
            cursor = max(cursor, end_ms, start_ms)
        pieces.append(audio[cursor:])
        check_cancelled()
        # Join raw frames once, avoiding repeated copies of a long recording.
        joined = audio._spawn(b"".join(piece.raw_data for piece in pieces))
        joined.export(temporary, format="mp3")
        check_cancelled()
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _validate_member(name: str) -> None:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise UploadError(f"Unsafe archive member: {name}")


def _unique_path(path: Path) -> Path:
    candidate = path
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        index += 1
    return candidate
