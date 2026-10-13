from __future__ import annotations

import json
import re
from pathlib import Path


DEFAULT_TIMELINE_PATTERN = "{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def save_transcript(job_dir: Path, content: str) -> None:
    atomic_write_text(job_dir / "results.txt", content)


def read_text(path: Path, tail: int | None = None) -> str:
    if not path.is_file():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    return content[-tail:] if tail else content


def speaker_metadata(job_dir: Path) -> dict:
    path = job_dir / "speakers.json"
    if not path.is_file():
        return {"labels": [], "names": {}, "samples": {}, "candidates": {}}
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        labels = [str(label) for label in content.get("labels", [])]
        names = {
            str(label): str(name)
            for label, name in content.get("names", {}).items()
            if str(label) in labels and str(name).strip()
        }
        samples = {
            str(label): Path(str(filename)).name
            for label, filename in content.get("samples", {}).items()
            if str(label) in labels and Path(str(filename)).name == str(filename)
        }
        candidates = {
            str(label): [
                {
                    "name": str(candidate.get("name", "")),
                    "score": float(candidate.get("score", 0)),
                    "voiceprints": int(candidate.get("voiceprints", 0)),
                }
                for candidate in items[:5]
                if isinstance(candidate, dict) and str(candidate.get("name", "")).strip()
            ]
            for label, items in content.get("candidates", {}).items()
            if str(label) in labels and isinstance(items, list)
        }
        return {
            "labels": labels,
            "names": names,
            "samples": samples,
            "candidates": candidates,
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"labels": [], "names": {}, "samples": {}, "candidates": {}}


def apply_speaker_names(content: str, names: dict[str, str]) -> str:
    for label in sorted(names, key=len, reverse=True):
        content = re.sub(
            rf"\b{re.escape(label)}\b", lambda _match, name=names[label]: name, content
        )
    return content


def read_transcript(job_dir: Path) -> str:
    metadata = speaker_metadata(job_dir)
    return apply_speaker_names(read_text(job_dir / "results.txt"), metadata["names"])


def read_timeline(
    job_dir: Path,
    transcript: str,
    names: dict[str, str],
    timeline_pattern: str = DEFAULT_TIMELINE_PATTERN,
) -> str:
    """Add patterned start times only when the transcript matches its chunks."""
    try:
        chunks = json.loads(read_text(job_dir / "timeline.json") or "[]")
        if not chunks:
            return ""
        remaining = transcript
        rendered = []
        for chunk in chunks:
            text = apply_speaker_names(chunk["text"], names)
            # Preserve source headings and whitespace between chunks. Any other
            # change invalidates alignment rather than attaching a misleading time.
            prefix, found, remaining_after = remaining.partition(text)
            allowed_prefix = prefix.strip()
            heading = apply_speaker_names(f"## {chunk['source']}", names)
            if not found or allowed_prefix not in {"", heading}:
                return ""
            milliseconds = max(0, round(float(chunk["start"]) * 1000))
            seconds, milliseconds = divmod(milliseconds, 1000)
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            timestamp = timeline_pattern.format(
                hours=hours, minutes=minutes,
                seconds=seconds, milliseconds=milliseconds,
            )
            rendered.append(f"{prefix}[{timestamp}] {text}")
            remaining = remaining_after
        if remaining.strip():
            return ""
        return "".join(rendered) + remaining
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return ""
