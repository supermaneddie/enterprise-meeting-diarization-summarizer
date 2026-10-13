from __future__ import annotations

import codecs
import json
import shutil
import uuid
from urllib.parse import urlparse
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from time import perf_counter

from pydub import AudioSegment

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file

from .urls import url_for

from flask_sock import Sock
from werkzeug.datastructures import FileStorage

from .formats import allowed_suffixes
from .db import utcnow
from .files import UploadError, collect_audio_files, save_uploads, validate_volume
from .artifacts import (
    apply_speaker_names,
    read_text,
    read_transcript,
    read_timeline,
    save_transcript as save_transcript_file,
    speaker_metadata,
)
from .voiceprints import RECOGNITION_STRATEGIES, VoiceprintError

web = Blueprint("web", __name__)
sock = Sock()


@web.before_request
def log_request_started():
    g.transcription_request_started = perf_counter()
    current_app.logger.debug(
        "Route started: method=%s endpoint=%s job_id=%r content_length=%s",
        request.method, request.endpoint, (request.view_args or {}).get("job_id"),
        request.content_length,
    )


@web.after_request
def log_request_finished(response):
    started = g.get("transcription_request_started")
    current_app.logger.debug(
        "Route finished: method=%s endpoint=%s job_id=%r status=%s elapsed_ms=%.1f",
        request.method, request.endpoint, (request.view_args or {}).get("job_id"),
        response.status_code, (perf_counter() - started) * 1000 if started is not None else 0,
    )
    return response

def _store():
    return current_app.extensions["job_store"]


def _runner():
    return current_app.extensions["job_runner"]


def _settings():
    return current_app.config["SETTINGS"]


def _elapsed(job: dict) -> int | None:
    from datetime import datetime, timezone
    if not job["started_at"]:
        return None
    start = datetime.fromisoformat(job["started_at"])
    end = datetime.fromisoformat(job["finished_at"]) if job["finished_at"] else datetime.now(timezone.utc)
    return max(0, int((end - start).total_seconds()))


def _decorate(job: dict) -> dict:
    item = dict(job)
    item["elapsed_seconds"] = _elapsed(item)
    item["adjusted_files"] = list(item.get("config", {}).get("adjusted_files", []))
    item["diarization_enabled"] = bool(item.get("config", {}).get("diarization_enabled", False))
    item["remove_audio_after_completed"] = bool(item.get("config", {}).get("remove_audio_after_completed", False))
    item["auto_analysis"] = bool(item.get("config", {}).get("auto_analysis", True))
    config = item.get("config", {})
    item["audio_source"] = "url" if config.get("audio_url") else "file"
    item["audio_url"] = config.get("audio_url", "")
    item["realtime"] = bool(config.get("realtime"))
    item["final_transcription"] = config.get("final_transcription", "not_requested")
    item["voiceprint_recognition"] = bool(config.get("voiceprint_recognition", False))
    item["adjust_volume"] = bool(config.get("adjust_volume", config.get("auto_adjust_volume", False)))
    item["volume"] = config.get("volume", "auto")
    item.pop("config", None)
    return item


@web.get("/")
def index():
    pagination = _jobs_page()
    return render_template(
        "index.html",
        supported_suffixes=allowed_suffixes(_settings().asr_supported_format),
        **pagination,
        diarization_available=_settings().diarization_enabled,
        voiceprint_available=(
            _settings().diarization_enabled and _settings().voiceprint_enabled
        ),
    )


@web.get("/realtime")
def realtime_page():
    return render_template("realtime.html")


@sock.route("/realtime/stream", bp=web)
def realtime_stream(socket):
    from .realtime_stream import relay

    # Browser credentials must not authorize cross-origin recording sessions.
    origin = urlparse(request.headers.get("Origin", ""))
    if origin.netloc != request.host or origin.scheme not in {"http", "https"}:
        current_app.logger.debug("Realtime stream rejected: origin mismatch")
        socket.close(reason=1008, message="Same-origin connection required")
        return
    current_app.logger.debug("Realtime stream accepted")
    try:
        relay(socket, _settings())
    finally:
        current_app.logger.debug("Realtime stream closed")


@web.post("/realtime/save")
def realtime_save():
    from .realtime import validate_recording

    try:
        name = _validate_job_name(request.form.get("name", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    upload = request.files.get("audio")
    if not upload:
        return jsonify(error="Record some audio before saving"), 400
    job_id = uuid.uuid4().hex
    job_dir = _settings().job_dir(job_id)
    try:
        audio_dir = job_dir / "audio"
        audio_dir.mkdir(parents=True)
        path = audio_dir / "microphone.wav"
        upload.save(path)
        duration = validate_recording(path)
        content = request.form.get("text", "")
        live = request.form.get("live_text", "")
        if max(len(content), len(live)) > 10_000_000:
            raise ValueError("Transcript exceeds the 10,000,000 character limit")
        mp3_path = audio_dir / "microphone.mp3"
        with path.open("rb") as source:
            AudioSegment.from_wav(source).export(mp3_path, format="mp3", bitrate="64k").close()
        path.unlink()
        path = mp3_path
        save_transcript_file(job_dir, content)
        (job_dir / "results-live.txt").write_text(live, encoding="utf-8")
        (job_dir / "results-edited.txt").write_text(content, encoding="utf-8")
        config = _job_config({})
        config.update(
            auto_analysis=False, diarization_enabled=False,
            voiceprint_recognition=False, adjust_volume=False,
            realtime=True, recording_seconds=duration,
            manual_edits=request.form.get("edited") == "true" or content != live,
            final_transcription="not_requested",
            asr_supported_format=_settings().asr_supported_format + ",MP3",
            transcription_incomplete=request.form.get("incomplete") == "true",
        )
        _store().create(job_id, name, "voice", [path.name], config)
        _store().update(
            job_id, status="completed", started_at=utcnow(), finished_at=utcnow(),
            current_step=("Saved — partial transcript" if config["transcription_incomplete"]
                          else "Saved — ready for analysis"),
        )
    except Exception as exc:
        _store().delete(job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        if isinstance(exc, ValueError):
            return jsonify(error=str(exc)), 400
        current_app.logger.exception("Could not save real-time recording")
        return jsonify(error="Could not save recording as MP3. Your recording is still here; please try again."), 500
    current_app.logger.debug(
        "Realtime recording saved: job_id=%s duration_seconds=%.2f incomplete=%s edited=%s",
        job_id, duration, config["transcription_incomplete"], config["manual_edits"],
    )
    if request.form.get("final_transcription") == "true":
        # Saving is already durable; a queue failure must never delete the recording.
        try:
            if not _runner().submit_final_transcription(job_id):
                raise RuntimeError("Could not queue final transcription")
            current_app.logger.debug("Final transcription queued: job_id=%s", job_id)
        except Exception:
            current_app.logger.exception("Could not queue final transcription")
            _store().update(job_id, status="failed", error="Recording saved; final transcription could not be queued. Retry from the job page.")
    return jsonify(job_id=job_id, url=url_for("web.job_detail", job_id=job_id)), 201


@web.get("/voiceprints")
def voiceprints_page():
    return render_template(
        "voiceprints.html",
        people=current_app.extensions["voiceprint_store"].list_people(),
        enabled=_settings().voiceprint_enabled,
        recording_languages=_settings().voiceprint_recording_languages,
        recording_default_language=_settings().voiceprint_recording_default_language,
        strategies=RECOGNITION_STRATEGIES,
        recognition_strategy=_settings().voiceprint_recognition_strategy,
    )


@web.post("/voiceprints/strategy")
def save_voiceprint_strategy():
    strategy = request.form.get("strategy", "")
    if strategy not in RECOGNITION_STRATEGIES:
        return jsonify(error="Invalid voiceprint recognition strategy"), 400
    updated = _settings().with_overrides({"voiceprint_recognition_strategy": strategy})
    updated.save()
    current_app.config["SETTINGS"] = updated
    _runner().settings = updated
    flash("Recognition strategy saved. New jobs will use it.", "success")
    return redirect(url_for("web.voiceprints_page"))


@web.post("/voiceprints/people/<int:person_id>/rename")
def rename_voiceprint_speaker(person_id: int):
    try:
        name = _validate_speaker_name(request.form.get("name", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    try:
        if not current_app.extensions["voiceprint_store"].rename(person_id, name):
            abort(404)
    except VoiceprintError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(message="Speaker renamed")


@web.post("/voiceprints")
def upload_voiceprint():
    if not _settings().voiceprint_enabled:
        return jsonify(error="Voiceprint enrollment is disabled in config.yml"), 409
    try:
        name = _validate_speaker_name(request.form.get("name", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="An audio file is required"), 400
    with TemporaryDirectory(prefix="voiceprint-") as temporary:
        source = Path(temporary) / "input"
        upload.save(source)
        try:
            audio = AudioSegment.from_file(source)
        except Exception:
            return jsonify(error="Could not decode the uploaded audio file"), 400
        if not 3000 <= len(audio) <= 60000:
            return jsonify(error="Provide between 3 and 60 seconds of one speaker's voice"), 400
        if audio.rms == 0:
            return jsonify(error="The audio is silent; please record audible speech"), 400
        normalized = Path(temporary) / "sample.wav"
        audio.set_channels(1).set_frame_rate(16000).export(normalized, format="wav")
        try:
            with _runner().device_pool.acquire(_settings().diarization_device, Event()) as device:
                people = current_app.extensions["voiceprint_engine"].enroll(
                    name, normalized, _settings().voiceprint_model, device
                )
        except (VoiceprintError, ValueError) as exc:
            return jsonify(error=str(exc)), 503
    return jsonify(message=f"Voiceprint added for {name}", indexed_people=people), 201


@web.post("/voiceprints/<int:sample_id>/delete")
def delete_voiceprint(sample_id: int):
    if not current_app.extensions["voiceprint_store"].delete(sample_id):
        abort(404)
    return jsonify(message="Voiceprint deleted")


@web.post("/voice")
def voice():
    payload = request.get_json(silent=True) or request.form
    files = request.files.getlist("files") or request.files.getlist("file")
    files = [file for file in files if file.filename]
    try:
        job = _create_voice_job(payload, files, auto_analysis_default=request.is_json)
    except ValueError as exc:
        if request.is_json or request.accept_mimetypes.best == "application/json":
            return jsonify(error=str(exc)), 400
        flash(str(exc), "error")
        return redirect(url_for("web.index"))
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify(
            job_id=job["id"],
            name=job["name"],
            status=job["status"],
            url=url_for("web.job_detail", job_id=job["id"]),
        ), 202
    return redirect(url_for("web.job_detail", job_id=job["id"]))


def _validate_voice_options(payload):
    volume = validate_volume(payload.get("volume", "auto"))
    requested_diarization = str(
        payload.get("use_diarization", "")
    ).lower() in {"1", "true", "yes", "on"}
    if requested_diarization and not _settings().diarization_enabled:
        raise UploadError(
            "Diarization is disabled in config.yml and cannot be enabled for this job"
        )
    requested_voiceprint = str(
        payload.get("use_voiceprint_recognition", "")
    ).lower() in {"1", "true", "yes", "on"}
    if requested_voiceprint and not requested_diarization:
        raise UploadError("Voiceprint recognition requires speaker diarization")
    if requested_voiceprint and not _settings().voiceprint_enabled:
        raise UploadError("Voiceprint recognition is disabled in config.yml")
    return volume, requested_diarization, requested_voiceprint


def _create_voice_job(payload, files, *, auto_analysis_default=True):
    """Validate, persist and submit a voice job for the UI or API."""
    audio_url = str(payload.get("url") or payload.get("audio_url") or "").strip()
    name = _validate_job_name(payload.get("name", ""))
    brief_description = _validate_brief_description(payload.get("brief_description", ""))
    job_id = uuid.uuid4().hex
    job_dir = _settings().job_dir(job_id)
    current_app.logger.debug(
        "Creating voice job: job_id=%s upload_count=%s has_url=%s",
        job_id, len(files), bool(audio_url),
    )
    try:
        if files and audio_url:
            raise UploadError("Provide either uploaded files or an audio URL, not both")
        if not files and not audio_url:
            raise UploadError("At least one uploaded file or an audio URL is required")
        volume, requested_diarization, requested_voiceprint = _validate_voice_options(payload)
        if audio_url:
            # Remote inputs are downloaded by the LangGraph worker so this
            # request only persists and queues the job.
            job_dir.mkdir(parents=True, exist_ok=True)
            audio = []
        else:
            saved = save_uploads(files, job_dir / "uploads", _settings().asr_supported_format)
            audio = collect_audio_files(
                saved, job_dir / "audio", _settings().max_archive_files,
                _settings().max_extracted_bytes, _settings().asr_supported_format,
            )
            # Uploads are staging inputs only. The validated/extracted media files
            # in audio are the canonical copies retained by the job.
            shutil.rmtree(job_dir / "uploads", ignore_errors=True)
        config = _job_config(payload)
        config["auto_analysis"] = str(
            payload.get("auto_analysis", auto_analysis_default)
        ).lower() in {"1", "true", "yes", "on"}
        config["remove_audio_after_completed"] = str(
            payload.get("remove_audio_after_completed", False)
        ).lower() in {"1", "true", "yes", "on"}
        config["audio_url"] = audio_url
        config["volume"] = volume
        config["diarization_enabled"] = requested_diarization
        config["voiceprint_recognition"] = requested_voiceprint
        config["adjust_volume"] = str(
            payload.get("adjust_volume", "")
        ).lower() in {"1", "true", "yes", "on"}
        # Volume adjustment is deliberately deferred to the background worker
        # so the request can return as soon as the job is persisted and queued.
        config["adjusted_files"] = []
        config["processing_files"] = [path.name for path in audio]
        current_app.logger.debug(
            "Voice job prepared: job_id=%s audio_count=%s diarization=%s voiceprint=%s "
            "auto_analysis=%s adjust_volume=%s",
            job_id, len(audio), requested_diarization, requested_voiceprint,
            config["auto_analysis"], config["adjust_volume"],
        )
        job = _store().create(
            job_id,
            name,
            "voice",
            [path.name for path in audio],
            config,
            brief_description,
        )
        try:
            submitted = _runner().submit(job_id)
        except Exception as exc:
            current_app.logger.debug(
                "Voice job submission failed: job_id=%s error_type=%s", job_id, type(exc).__name__,
            )
            _store().update(
                job_id, status="failed", finished_at=utcnow(),
                current_step="Failed", error=str(exc),
            )
            raise
        current_app.logger.debug("Voice job submission: job_id=%s accepted=%s", job_id, submitted)
        if not submitted:
            _store().update(
                job_id, status="failed", finished_at=utcnow(),
                current_step="Failed", error="Job runner is unavailable",
            )
            _runner().notify_terminal(job_id)
            job = _store().get(job_id)
    except UploadError:
        current_app.logger.debug("Voice job validation failed; cleaning up: job_id=%s", job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    return job

@web.post("/text")
def text():
    payload = request.get_json(silent=True) or request.form
    try:
        name = _validate_job_name(payload.get("name", ""))
        brief_description = _validate_brief_description(payload.get("brief_description", ""))
    except ValueError as exc:
        if request.is_json or request.accept_mimetypes.best == "application/json":
            return jsonify(error=str(exc)), 400
        flash(str(exc), "error")
        return redirect(url_for("web.index"))
    content = str(payload.get("text", "")).strip()
    if not content:
        return jsonify(error="Meeting text is required"), 400
    job_id = uuid.uuid4().hex
    job_dir = _settings().job_dir(job_id)
    job_dir.mkdir(parents=True)
    (job_dir / "input.txt").write_text(content, encoding="utf-8")
    job = _store().create(job_id, name, "text", ["input.txt"], _job_config(), brief_description)
    submitted = _runner().submit(job_id)
    current_app.logger.debug(
        "Text job submission: job_id=%s text_characters=%s accepted=%s", job_id, len(content), submitted,
    )
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify(
            job_id=job_id,
            name=job["name"],
            status=job["status"],
            url=url_for("web.job_detail", job_id=job_id),
        ), 202
    return redirect(url_for("web.job_detail", job_id=job_id))


@web.get("/jobs/<job_id>")
def job_detail(job_id: str):
    job = _store().get(job_id)
    if not job:
        abort(404)
    content = _job_content(job_id)
    return render_template(
        "job.html", job=_decorate(job), **content,
        diarization_available=_settings().diarization_enabled,
        voiceprint_available=_settings().diarization_enabled and _settings().voiceprint_enabled,
    )


@web.get("/jobs/<job_id>/audio/<path:filename>")
def job_audio(job_id: str, filename: str):
    job = _store().get(job_id)
    if not job or job["kind"] != "voice":
        abort(404)
    # Match the database allow-list before constructing a filesystem path.
    if filename not in job["files"] or Path(filename).name != filename:
        abort(404)
    audio_dir = (_settings().job_dir(job_id) / "audio").resolve()
    path = (audio_dir / filename).resolve()
    if path.parent != audio_dir or not path.is_file():
        abort(404)
    return send_file(
        path,
        as_attachment=False,
        conditional=True,
        download_name=filename,
    )


@web.post("/jobs/<job_id>/speakers")
def save_speaker_names(job_id: str):
    job = _store().get(job_id)
    if not job or job["kind"] != "voice":
        abort(404)
    if job["status"] in {"queued", "running", "stopping"}:
        return jsonify(error="Speaker names can be saved after processing finishes"), 409
    path = _settings().job_dir(job_id) / "speakers.json"
    metadata = speaker_metadata(path.parent)
    if not metadata["labels"]:
        abort(404)
    names = {}
    for index, label in enumerate(metadata["labels"]):
        raw_name = request.form.get(f"speaker_name_{index}", "")
        try:
            name = _validate_speaker_name(raw_name, required=False)
        except ValueError:
            if request.accept_mimetypes.best == "application/json":
                return jsonify(error=f"Invalid name for {label}"), 400
            flash(f"Invalid name for {label}", "error")
            return redirect(url_for("web.job_detail", job_id=job_id))
        if name:
            names[label] = name
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "labels": metadata["labels"],
                "names": names,
                "samples": metadata["samples"],
                "candidates": metadata["candidates"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    if request.accept_mimetypes.best == "application/json":
        content = _job_content(job_id)
        return jsonify(
            message="Speaker names saved",
            speakers=content["speakers"],
            transcript=content["transcript"],
            timeline=content["timeline"],
            transcript_identifiers=content["transcript_identifiers"],
            timeline_identifiers=content["timeline_identifiers"],
            summary=content["summary"],
        )
    flash("Speaker names saved.", "success")
    return redirect(url_for("web.job_detail", job_id=job_id))


@web.post("/jobs/<job_id>/speakers/<int:speaker_index>/voiceprint")
def add_speaker_voiceprint(job_id: str, speaker_index: int):
    job = _store().get(job_id)
    if not job or job["kind"] != "voice":
        abort(404)
    if job["status"] in {"queued", "running", "stopping"}:
        return jsonify(error="Voiceprints can be added after processing finishes"), 409
    if not job.get("config", {}).get("diarization_enabled"):
        return jsonify(error="Voiceprints require a diarized job"), 409
    if not _settings().voiceprint_enabled:
        return jsonify(error="Voiceprint enrollment is disabled in config.yml"), 409
    job_dir = _settings().job_dir(job_id)
    metadata = speaker_metadata(job_dir)
    if speaker_index < 0 or speaker_index >= len(metadata["labels"]):
        abort(404)
    payload = request.get_json(silent=True) or request.form
    try:
        name = _validate_speaker_name(payload.get("name", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    label = metadata["labels"][speaker_index]
    filename = metadata["samples"].get(label)
    if not filename:
        return jsonify(error=f"No clear diarization sample is available for {label}"), 409
    sample_dir = (job_dir / "speaker-samples").resolve()
    sample_path = (sample_dir / filename).resolve()
    if sample_path.parent != sample_dir or not sample_path.is_file():
        return jsonify(error="The saved speaker sample is unavailable"), 404
    config = job.get("config", {})
    model = str(config.get("voiceprint_model") or _settings().voiceprint_model)
    device = str(config.get("diarization_device") or _settings().diarization_device)
    try:
        with _runner().device_pool.acquire(device, Event()) as assigned_device:
            people = current_app.extensions["voiceprint_engine"].enroll(
                name, sample_path, model, assigned_device, job_id, label
            )
    except (VoiceprintError, ValueError) as exc:
        return jsonify(error=str(exc)), 503
    return jsonify(
        message=f"Voiceprint added for {name}",
        name=name,
        indexed_people=people,
    )


@web.post("/jobs/<job_id>/final-transcription")
def final_transcription(job_id: str):
    job = _store().get(job_id)
    if not job or not job["config"].get("realtime"):
        abort(404)
    if not _runner().submit_final_transcription(job_id):
        return jsonify(error="Wait for the current task to finish"), 409
    return redirect(url_for("web.job_detail", job_id=job_id))


@web.post("/jobs/<job_id>/transcript")
def save_transcript(job_id: str):
    job = _store().get(job_id)
    if not job:
        abort(404)
    if job["status"] in {"queued", "running", "stopping"} or _runner().is_active(job_id):
        return jsonify(error="Wait for the current processing task to finish"), 409
    payload = request.get_json(silent=True) or request.form
    transcript = str(payload.get("transcript", ""))
    if not transcript.strip() and payload.get("action") != "use_final":
        return jsonify(error="Transcript cannot be empty"), 400
    if len(transcript) > 10_000_000:
        return jsonify(error="Transcript exceeds the 10,000,000 character limit"), 413

    job_dir = _settings().job_dir(job_id)
    action = str(payload.get("action", "save"))
    if action not in {"save", "regenerate", "use_final"}:
        return jsonify(error="Action must be 'save', 'regenerate', or 'use_final'"), 400
    if action == "use_final":
        transcript = read_text(job_dir / "results-final.txt")
        if not transcript.strip():
            return jsonify(error="No final transcript is available"), 409
    save_transcript_file(job_dir, transcript)
    current_app.logger.debug(
        "Transcript saved: job_id=%r action=%s characters=%s", job_id, action, len(transcript),
    )
    if job["config"].get("realtime"):
        config = dict(job["config"], manual_edits=action != "use_final")
        _store().update(job_id, config_json=json.dumps(config))
        if action != "use_final":
            (job_dir / "results-edited.txt").write_text(transcript, encoding="utf-8")
    if action == "regenerate":
        if not _runner().submit_reanalysis(job_id):
            return jsonify(error="The job is already processing"), 409
        response = {"message": "Transcript saved; summary regeneration queued", "status": "queued"}
        status_code = 202
    else:
        _store().update(
            job_id,
            current_step="Transcript edited; summary may be outdated",
            error=None,
        )
        response = {"message": "Transcript saved", "status": job["status"], "transcript": transcript}
        status_code = 200
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify(response), status_code
    flash(response["message"], "success")
    return redirect(url_for("web.job_detail", job_id=job_id))


@web.get("/api/jobs")
def jobs_api():
    if "page" in request.args:
        return jsonify(_jobs_page())
    return jsonify([_decorate(job) for job in _store().list()])


def _jobs_page():
    pagination = _store().page(request.args.get("page", default=1, type=int))
    pagination["jobs"] = [_decorate(job) for job in pagination["jobs"]]
    return pagination


@web.get("/api/jobs/<job_id>")
def job_api(job_id: str):
    job = _store().get(job_id)
    if not job:
        abort(404)
    logs_offset = request.args.get("logs_offset")
    if logs_offset is not None:
        try:
            logs_offset = int(logs_offset)
            if logs_offset < 0:
                raise ValueError
        except ValueError:
            return jsonify(error="logs_offset must be a non-negative integer"), 400
    return jsonify(job=_decorate(job), **_job_content(job_id, logs_offset))


@web.post("/jobs/<job_id>/stop")
def stop_job(job_id: str):
    if not _store().get(job_id):
        abort(404)
    cancelled = _runner().cancel(job_id)
    current_app.logger.debug("Job stop requested: job_id=%r accepted=%s", job_id, cancelled)
    return redirect(url_for("web.job_detail", job_id=job_id))


@web.post("/jobs/<job_id>/brief-description")
def save_brief_description(job_id: str):
    if not _store().get(job_id):
        abort(404)
    payload = request.get_json(silent=True) or request.form
    try:
        description = _validate_brief_description(payload.get("brief_description", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    _store().update(job_id, brief_description=description)
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify(message="Brief description saved. Retry or regenerate analysis to apply it.",
                       brief_description=description)
    flash("Brief description saved. Retry or regenerate analysis to apply it.", "success")
    return redirect(url_for("web.job_detail", job_id=job_id))


def _audio_settings(payload, job):
    options = dict(payload)
    options.setdefault("volume", job["config"].get("volume", "auto"))
    settings = {
        "volume": validate_volume(options["volume"]),
        "adjust_volume": str(options.get("adjust_volume", "")).lower() in {"1", "true", "yes", "on"},
    }
    # The full form marker distinguishes unchecked boxes from legacy volume-only requests.
    if "audio_settings" in options or any(
        key in options for key in ("use_diarization", "use_voiceprint_recognition", "auto_analysis", "remove_audio_after_completed")
    ):
        _, diarization, voiceprint = _validate_voice_options(options)
        settings.update(
            diarization_enabled=diarization,
            voiceprint_recognition=voiceprint,
            remove_audio_after_completed=str(options.get("remove_audio_after_completed", "")).lower() in {"1", "true", "yes", "on"},
            auto_analysis=str(options.get("auto_analysis", "")).lower() in {"1", "true", "yes", "on"},
        )
    return settings


@web.post("/jobs/<job_id>/audio-settings")
def save_audio_settings(job_id: str):
    job = _store().get(job_id)
    if not job:
        abort(404)
    if job["kind"] != "voice":
        return jsonify(error="Audio settings are only available for voice jobs"), 400
    if _runner().is_active(job_id) or job["status"] in {"queued", "running", "stopping"}:
        return jsonify(error="Wait for processing to finish, or use Save settings and Retry"), 409
    payload = request.get_json(silent=True) or request.form
    try:
        audio_settings = _audio_settings(payload, job)
    except UploadError as exc:
        return jsonify(error=str(exc)), 400
    config = dict(job["config"])
    config.update(audio_settings)
    _store().update(job_id, config_json=json.dumps(config))
    message = "Audio settings saved. Retry to apply them to the original recordings."
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify(message=message)
    flash(message, "success")
    return redirect(url_for("web.job_detail", job_id=job_id))


@web.post("/jobs/<job_id>/retry")
def retry_job(job_id: str):
    job = _store().get(job_id)
    if not job:
        abort(404)
    payload = request.get_json(silent=True) or request.form
    volume_settings = None
    if job["kind"] == "voice" and any(
        key in payload for key in (
            "volume", "adjust_volume", "volume_settings", "audio_settings",
            "use_diarization", "use_voiceprint_recognition", "auto_analysis", "remove_audio_after_completed",
        )
    ):
        try:
            audio_settings = _audio_settings(payload, job)
        except UploadError as exc:
            return jsonify(error=str(exc)), 400
        volume_settings = audio_settings
    if not _runner().retry(job_id, volume_settings):
        current_app.logger.debug("Job retry rejected: job_id=%r", job_id)
        return jsonify(error="The job could not be queued for retry"), 409
    current_app.logger.debug(
        "Job retry accepted: job_id=%r audio_settings_updated=%s", job_id, volume_settings is not None,
    )
    if request.accept_mimetypes.best == "application/json":
        return jsonify(message="Job queued for retry"), 202
    return redirect(url_for("web.job_detail", job_id=job_id))


@web.post("/jobs/<job_id>/delete")
def delete_job(job_id: str):
    job = _store().get(job_id)
    if not job:
        abort(404)
    if _runner().is_active(job_id) or job["status"] in {"queued", "running", "stopping"}:
        return jsonify(error="Stop the job before deleting it"), 409
    _store().delete(job_id)
    shutil.rmtree(_settings().job_dir(job_id), ignore_errors=True)
    current_app.logger.debug("Job deleted: job_id=%r", job_id)
    return redirect(url_for("web.index"))


@web.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        updated = _settings().with_overrides(request.form)
        # HTML checkboxes are absent when unchecked.
        from dataclasses import replace
        updated = replace(
            updated,
            asr_verify_ssl="asr_verify_ssl" in request.form,
            analysis_verify_ssl="analysis_verify_ssl" in request.form,
            diarization_enabled="diarization_enabled" in request.form,
            voiceprint_enabled="voiceprint_enabled" in request.form,
        )
        updated.save()
        current_app.config["SETTINGS"] = updated
        _runner().settings = updated
        current_app.logger.debug("Application settings saved")
        flash("Model settings saved to config.yml. New jobs will use them.", "success")
        return redirect(url_for("web.settings_page"))
    return render_template("settings.html", settings=_settings().public_dict())


def _job_config(override=None) -> dict:
    if override is None:
        override = request.form if request.form else (request.get_json(silent=True) or {})
    settings = _settings().with_overrides(override)
    keys = {
        "asr_base_url", "asr_model", "asr_api_key", "asr_verify_ssl", "asr_prompt",
        "asr_concurrency", "asr_timeout_seconds", "asr_chunk_deadline_seconds",
        "asr_max_retries",
        "asr_min_chunk_seconds", "asr_max_chunk_seconds",
        "analysis_base_url", "analysis_model", "analysis_api_key", "analysis_verify_ssl", "analysis_prompt",
        "diarization_enabled", "diarization_model", "diarization_hf_token", "diarization_device",
        "diarization_min_speakers", "diarization_max_speakers", "diarization_merge_gap_seconds",
        "diarization_ssh_identity_file",
        "diarization_ssh_port",
        "voiceprint_enabled", "voiceprint_model", "voiceprint_match_threshold",
        "voiceprint_recognition_strategy",
    }
    return {key: getattr(settings, key) for key in keys}


def _validate_brief_description(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Brief description must be text")
    description = value.strip()
    if len(description) > 4000:
        raise ValueError("Brief description must be 4000 characters or fewer")
    return description


def _validate_job_name(value) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("Job name is required")
    if len(name) > 200:
        raise ValueError("Job name must be 200 characters or fewer")
    if any(ord(character) < 32 for character in name):
        raise ValueError("Job name contains unsupported control characters")
    return name


def _validate_speaker_name(value, required: bool = True) -> str:
    name = str(value or "").strip()
    if required and not name:
        raise ValueError("A speaker name is required before adding a voiceprint")
    if len(name) > 100 or any(ord(character) < 32 for character in name):
        raise ValueError("Speaker names must be 100 characters or fewer and contain no controls")
    return name


def _read_logs(path: Path, offset: int | None = None) -> dict:
    """Return a UTF-8 log delta and its next byte offset; initially show the tail."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            reset = offset is not None and offset > size
            start = max(0, size - 500_000) if offset is None or reset else offset
            handle.seek(start)
            data = handle.read(size - start)
    except FileNotFoundError:
        return {"logs": "", "logs_offset": 0, "logs_reset": bool(offset)}
    # A tail can start inside a multibyte character. Do not display its fragment.
    if offset is None or reset:
        while data and 0x80 <= data[0] <= 0xBF:
            data = data[1:]
            start += 1
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    logs = decoder.decode(data, final=False)
    # Leave any partially written final character for the next poll.
    pending, _ = decoder.getstate()
    return {"logs": logs, "logs_offset": start + len(data) - len(pending), "logs_reset": reset}


def _job_content(job_id: str, logs_offset: int | None = None) -> dict:
    settings = _settings()
    job_dir = settings.job_dir(job_id)
    metadata = speaker_metadata(job_dir)
    transcript = read_transcript(job_dir)
    identifier_names = {
        label: f"{name} - {label}" if name != label else label
        for label, name in metadata["names"].items()
    }
    transcript_identifiers = apply_speaker_names(
        read_text(job_dir / "results.txt"), identifier_names
    )
    summary = apply_speaker_names(read_text(job_dir / "summary.txt"), metadata["names"])
    speakers = []
    for label in metadata["labels"]:
        speaker = {"label": label, "name": metadata["names"].get(label, "")}
        if metadata["candidates"].get(label):
            speaker["candidates"] = metadata["candidates"][label][:5]
        if metadata["samples"].get(label):
            speaker["voiceprint_sample"] = True
        speakers.append(speaker)
    return {
        "transcript": transcript,
        "timeline": read_timeline(job_dir, transcript, metadata["names"], settings.timeline_pattern),
        "transcript_identifiers": transcript_identifiers,
        "timeline_identifiers": read_timeline(
            job_dir, transcript_identifiers, identifier_names, settings.timeline_pattern
        ),
        "summary": summary,
        "final_transcript": read_text(job_dir / "results-final.txt"),
        "live_transcript": read_text(job_dir / "results-live.txt"),
        "edited_transcript": read_text(job_dir / "results-edited.txt"),
        **_read_logs(job_dir / "langgraph.log", logs_offset),
        "speakers": speakers,
        "voiceprint_enabled": _settings().voiceprint_enabled,
    }


@web.errorhandler(413)
def too_large(_error):
    return jsonify(error="Upload exceeds MAX_UPLOAD_MB"), 413
