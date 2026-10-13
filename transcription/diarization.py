from __future__ import annotations

import json
import math
import re
import shlex
import shutil
import subprocess
import tarfile
import time
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from uuid import uuid4

from .devices import parse_remote_device
from .diarization_worker import (
    DiarizationConfig,
    DiarizationError,
    _bounded_speaker_turns,  # Backwards-compatible import for callers/tests.
    create_speaker_chunks as create_local_speaker_chunks,
)
if TYPE_CHECKING:
    from .settings import Settings


# Trust new hosts without prompting, but reject changed host keys.
_SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]


def _ssh_commands(settings, host):
    try:
        port = int(getattr(settings, "diarization_ssh_port", 22))
        if not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise DiarizationError("SSH port must be between 1 and 65535") from exc
    identity = getattr(settings, "diarization_ssh_identity_file", "").strip()
    identity_options = []
    if identity:
        identity_path = Path(identity).expanduser().resolve()
        if not identity_path.is_file():
            raise DiarizationError(f"SSH identity file does not exist: {identity_path}")
        identity_options = ["-i", str(identity_path)]
    return (
        ["ssh", "-T", *_SSH_OPTIONS, *identity_options, "-p", str(port), host],
        ["scp", "-q", *_SSH_OPTIONS, *identity_options, "-P", str(port)],
    )


def check_remote_connection(settings: Settings, log) -> None:
    """Verify noninteractive SSH without transferring files or loading a model."""
    remote = parse_remote_device(settings.diarization_device)
    if remote is None:
        return
    _, host = remote
    log("diarization", f"Checking passwordless SSH connection to {host}")
    try:
        ssh, _ = _ssh_commands(settings, host)
        _run_command(ssh + ["true"], lambda: None, log, timeout=30)
    except DiarizationError as exc:
        raise DiarizationError(
            f"Passwordless SSH check failed for {host}: {exc}. "
            "Check SSH access, the configured identity file/agent, and the trusted host key "
            "for the account running this application."
        ) from exc
    log("diarization", f"Passwordless SSH connection to {host} succeeded")


def _run_command(command, check_cancelled, log, *, timeout=None):
    """Keep stdin open for the remote worker; terminate transport on cancellation."""
    with TemporaryDirectory(prefix="diarization-command-") as temporary:
        output = Path(temporary) / "output"
        with output.open("w+") as stream:
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                           stdout=stream, stderr=subprocess.STDOUT)
            except OSError as exc:
                raise DiarizationError(f"Could not start {command[0]}: {exc}") from exc
            try:
                deadline = time.monotonic() + timeout if timeout else None
                while True:
                    check_cancelled()
                    try:
                        code = process.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise DiarizationError(f"{command[0]} timed out")
                stream.seek(0)
                message = stream.read()[-16000:].strip()
                if message:
                    log("diarization", message)
                if code:
                    raise DiarizationError(f"{command[0]} failed (exit {code}): {message}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if process.stdin:
                    process.stdin.close()


def _read_results(archive_path: Path, output_dir: Path, check_cancelled):
    """Read only validated regular files; never extract archive paths or links."""
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)) or any(
                not member.isfile() or not re.fullmatch(
                    r"manifest\.json|speaker-turn-\d+\.wav", member.name
                ) for member in members
            ):
                raise ValueError("Unexpected archive entry")
            manifest_member = archive.getmember("manifest.json")
            if manifest_member.size > 16 * 1024 * 1024:
                raise ValueError("Manifest is too large")
            with archive.extractfile(manifest_member) as stream:
                manifest = json.load(stream)
            if not isinstance(manifest, list) or not manifest:
                raise ValueError("Empty or invalid manifest")
            seen = set()
            for item in manifest:
                filename, speaker, start = item["file"], item["speaker"], item["start"]
                if (not isinstance(filename, str)
                    or not re.fullmatch(r"speaker-turn-\d+\.wav", filename)
                    or filename in seen or filename not in names
                    or not isinstance(speaker, str)
                    or not isinstance(start, (int, float))
                    or not math.isfinite(start) or start < 0):
                    raise ValueError("Invalid speaker turn")
                seen.add(filename)
            output_dir.mkdir(parents=True, exist_ok=True)
            chunks = []
            for item in manifest:
                check_cancelled()
                path = output_dir / item["file"]
                with archive.extractfile(item["file"]) as source, path.open("wb") as target:
                    shutil.copyfileobj(source, target)
                chunks.append((path, item["speaker"], float(item["start"])))
            return chunks
    except (tarfile.TarError, ValueError, KeyError, TypeError, OSError) as exc:
        raise DiarizationError(f"Invalid remote diarization results: {exc}") from exc


def create_speaker_chunks(source: Path, output_dir: Path, settings: Settings,
                          check_cancelled, log) -> list[tuple[Path, str, float]]:
    remote = parse_remote_device(settings.diarization_device)
    if remote is None:
        return create_local_speaker_chunks(source, output_dir, settings, check_cancelled, log)
    device, host = remote
    config = {field.name: getattr(settings, field.name) for field in fields(DiarizationConfig)}
    config["diarization_device"] = device
    remote_dir = f"/tmp/transcription-diarization-{uuid4().hex}"
    remote_source = "source" + source.suffix.lower()
    ssh, scp = _ssh_commands(settings, host)
    check_cancelled()
    log("diarization", f"Running {device} diarization on {host}")
    try:
        _run_command(ssh + [f"umask 077; mkdir {shlex.quote(remote_dir)}"],
                     check_cancelled, log, timeout=30)
        with TemporaryDirectory(prefix="diarization-remote-") as temporary:
            staging = Path(temporary)
            request = staging / "request.json"
            request.write_text(json.dumps(config))
            request.chmod(0o600)
            worker = Path(__file__).with_name("diarization_worker.py")
            _run_command(scp + [str(worker), str(request), f"{host}:{remote_dir}/"],
                         check_cancelled, log)
            _run_command(scp + [str(source.resolve()), f"{host}:{remote_dir}/{remote_source}"],
                         check_cancelled, log)
            command = (f"cd {shlex.quote(remote_dir)} && "
                       "python3 -u diarization_worker.py --request request.json "
                       f"--source {shlex.quote(remote_source)} --output output --watch-stdin")
            _run_command(ssh + [command], check_cancelled, log)
            results = staging / "results.tar.gz"
            _run_command(scp + [f"{host}:{remote_dir}/output/results.tar.gz", str(results)],
                         check_cancelled, log)
            return _read_results(results, output_dir, check_cancelled)
    finally:
        # Cleanup must also run after cancellation, without masking the original error.
        try:
            _run_command(ssh + [f"rm -rf -- {shlex.quote(remote_dir)}"],
                         lambda: None, log, timeout=30)
        except Exception as exc:
            log("diarization", f"Could not clean up {host}:{remote_dir}: {exc}")
