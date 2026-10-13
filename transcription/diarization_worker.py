"""Standalone pyannote speaker diarization worker (requires FFmpeg and pyannote)."""
from __future__ import annotations

import re
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DiarizationConfig:
    diarization_device: str = "cpu"
    diarization_model: str = "pyannote/speaker-diarization-community-1"
    diarization_hf_token: str = ""
    diarization_min_speakers: int | None = None
    diarization_max_speakers: int | None = None
    diarization_merge_gap_seconds: float = 0.75
    asr_max_chunk_seconds: float = 60.0


class DiarizationError(RuntimeError):
    pass


def _bounded_speaker_turns(
    turns: list[tuple[float, float, str]], max_seconds: float, max_gap: float
) -> list[tuple[float, float, str]]:
    """Merge adjacent turns, then split every result to the ASR duration cap."""
    merged: list[list] = []
    for start, end, speaker in turns:
        if end <= start:
            continue
        previous = merged[-1] if merged else None
        if (
            previous
            and previous[2] == speaker
            and start - previous[1] <= max_gap
            and end - previous[0] <= max_seconds
        ):
            previous[1] = end
        else:
            merged.append([start, end, speaker])

    bounded: list[tuple[float, float, str]] = []
    for start, end, speaker in merged:
        cursor = start
        while end - cursor > max_seconds:
            split_at = cursor + max_seconds
            bounded.append((cursor, split_at, speaker))
            cursor = split_at
        if end > cursor:
            bounded.append((cursor, end, speaker))
    return bounded


def create_speaker_chunks(
    source: Path,
    output_dir: Path,
    settings: DiarizationConfig,
    check_cancelled,
    log,
) -> list[tuple[Path, str, float]]:
    """Detect speaker turns on the configured device and export them chronologically."""
    if not math.isfinite(settings.asr_max_chunk_seconds) or settings.asr_max_chunk_seconds <= 0:
        raise DiarizationError("Chunk duration must be positive and finite")
    try:
        from pydub import AudioSegment
        import numpy as np
        import torch
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise DiarizationError(
            "Speaker diarization is enabled but pyannote is not installed. "
            "Install requirements-diarization.txt."
        ) from exc

    device_name = settings.diarization_device.strip().lower()
    if not re.fullmatch(r"(?:cpu|cuda(?::\d+)?)", device_name):
        raise DiarizationError(
            "Invalid diarization device. Use 'cpu', 'cuda', or a CUDA index such as 'cuda:0'."
        )
    if device_name.startswith("cuda"):
        if not torch.cuda.is_available():
            raise DiarizationError(
                f"Diarization device is {device_name}, but CUDA is unavailable in PyTorch. "
                "Install a CUDA-enabled PyTorch build and verify the NVIDIA driver."
            )
        requested_index = int(device_name.split(":", 1)[1]) if ":" in device_name else 0
        if requested_index >= torch.cuda.device_count():
            raise DiarizationError(
                f"Diarization requested {device_name}, but PyTorch detects only "
                f"{torch.cuda.device_count()} CUDA device(s)."
            )
        device_name = f"cuda:{requested_index}"

    check_cancelled()
    device = torch.device(device_name)
    if device.type == "cuda":
        log(
            "diarization",
            f"Loading {settings.diarization_model} on {device_name} "
            f"({torch.cuda.get_device_name(device.index or 0)})",
        )
    else:
        log("diarization", f"Loading {settings.diarization_model} on CPU")
    token = settings.diarization_hf_token or None
    try:
        # Supplying a waveform dictionary bypasses pyannote's optional
        # torchcodec decoder, whose binary must exactly match Torch/FFmpeg.
        # Pydub already provides the application's tested FFmpeg decode path.
        log("diarization", f"Decoding {source.name} with FFmpeg for pyannote")
        audio = AudioSegment.from_file(source).set_channels(1).set_frame_rate(16000)
        samples = np.array(audio.get_array_of_samples(), dtype=np.float32, copy=True)
        samples /= float(1 << (8 * audio.sample_width - 1))
        audio_input = {
            "waveform": torch.from_numpy(samples).unsqueeze(0),
            "sample_rate": audio.frame_rate,
        }
        pipeline = Pipeline.from_pretrained(settings.diarization_model, token=token)
        if pipeline is None:
            raise DiarizationError(
                "Could not load the diarization model. Check the model name, accept its "
                "Hugging Face conditions, and provide a valid token."
            )
        pipeline.to(device)
        options = {}
        if settings.diarization_min_speakers is not None:
            options["min_speakers"] = settings.diarization_min_speakers
        if settings.diarization_max_speakers is not None:
            options["max_speakers"] = settings.diarization_max_speakers
        result = pipeline(audio_input, **options)
    except DiarizationError:
        raise
    except Exception as exc:
        raise DiarizationError(f"Speaker diarization failed: {exc}") from exc

    check_cancelled()
    annotation = getattr(result, "exclusive_speaker_diarization", None)
    if annotation is None:
        annotation = getattr(result, "speaker_diarization", result)

    turns: list[tuple[float, float, str]] = []
    if hasattr(annotation, "itertracks"):
        turns = [
            (float(turn.start), float(turn.end), str(speaker))
            for turn, _, speaker in annotation.itertracks(yield_label=True)
        ]
    else:
        turns = [
            (float(turn.start), float(turn.end), str(speaker))
            for turn, speaker in annotation
        ]
    turns.sort(key=lambda item: item[0])
    if not turns:
        raise DiarizationError("No speaker turns were detected in the recording")

    max_seconds = float(settings.asr_max_chunk_seconds)
    max_gap = settings.diarization_merge_gap_seconds
    bounded = _bounded_speaker_turns(turns, max_seconds, max_gap)

    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[Path, str, float]] = []
    max_ms = round(max_seconds * 1000)
    for index, (start, end, speaker) in enumerate(bounded):
        check_cancelled()
        # A small boundary pad helps ASR retain initial/final phonemes.
        start_ms = max(0, round((start - 0.12) * 1000))
        end_ms = min(len(audio), round((end + 0.12) * 1000))
        # Padding must never defeat the configured hard duration limit.
        end_ms = min(end_ms, start_ms + max_ms)
        path = output_dir / f"speaker-turn-{index:05d}.wav"
        audio[start_ms:end_ms].export(path, format="wav")
        # Preserve the detected turn start, before ASR boundary padding.
        chunks.append((path, speaker, start))
    log(
        "diarization",
        f"Detected {len(set(speaker for _, speaker, _ in chunks))} speaker(s), "
        f"{len(chunks)} turn(s)",
    )
    return chunks


def main():
    """Run independently of the application and return WAVs plus a JSON manifest."""
    import argparse
    import json
    import os
    import sys
    import tarfile
    import threading

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch-stdin", action="store_true")
    args = parser.parse_args()
    if args.watch_stdin:
        def watch_connection():
            while os.read(sys.stdin.fileno(), 1):
                pass
            os._exit(1)  # Stop inference when the SSH connection closes.
        threading.Thread(target=watch_connection, daemon=True).start()
    try:
        config = DiarizationConfig(**json.loads(args.request.read_text()))
        chunks = create_speaker_chunks(
            args.source, args.output / "chunks", config, lambda: None,
            lambda stage, message: print(f"{stage}: {message}", flush=True),
        )
        manifest = [{"file": path.name, "speaker": speaker, "start": start}
                    for path, speaker, start in chunks]
        manifest_path = args.output / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        with tarfile.open(args.output / "results.tar.gz", "w:gz") as archive:
            archive.add(manifest_path, arcname="manifest.json")
            for path, _, _ in chunks:
                archive.add(path, arcname=path.name)
    except Exception as exc:
        print(f"Speaker diarization failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
