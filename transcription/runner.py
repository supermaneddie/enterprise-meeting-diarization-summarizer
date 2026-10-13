from __future__ import annotations

import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from threading import Event, Lock

from .settings import Settings
from .db import JobStore, utcnow
from .pipeline import Cancelled, TranscriptionPipeline
from .devices import DevicePool
from .artifacts import read_transcript, atomic_write_text
from .files import create_volume_adjusted_copies
from .voiceprints import VoiceprintEngine


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        voiceprints: VoiceprintEngine | None = None,
        on_stopped=None,
    ):
        self.on_stopped = on_stopped
        self.settings = settings
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=settings.worker_count, thread_name_prefix="transcription-job")
        self.events: dict[str, Event] = {}
        self.pending_retries: set[str] = set()
        self.retry_volume_settings: dict[str, dict] = {}
        self.lock = Lock()
        self.shutting_down = False
        self.device_pool = DevicePool()
        self.voiceprints = voiceprints

    def submit(self, job_id: str) -> bool:
        event = Event()
        with self.lock:
            if self.shutting_down or job_id in self.events:
                return False
            self.events[job_id] = event
        self.pool.submit(self._run, job_id, event)
        return True

    def submit_final_transcription(self, job_id: str) -> bool:
        with self.lock:
            job = self.store.get(job_id)
            if (self.shutting_down or job_id in self.events or not job
                    or not job["config"].get("realtime")
                    or job["status"] in {"queued", "running", "stopping"}):
                return False
            event = Event()
            self.events[job_id] = event
            config = dict(job["config"], final_transcription="queued")
            self.store.update(job_id, status="queued", started_at=None, finished_at=None,
                              current_step="Queued for final transcription", error=None,
                              cancel_requested=0, config_json=json.dumps(config))
        try:
            self.pool.submit(self._run_final_transcription, job_id, event)
        except Exception:
            with self.lock:
                self.events.pop(job_id, None)
            self.store.update(job_id, status="failed", error="Final transcription could not be queued")
            raise
        return True

    def _run_final_transcription(self, job_id: str, event: Event) -> None:
        job = self.store.get(job_id)
        job_dir = self.settings.job_dir(job_id)
        initial_text = read_transcript(job_dir)

        def log(source, message):
            with (job_dir / "langgraph.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{utcnow()} [{source}] {message}\n")

        def progress(message):
            self.store.update(job_id, current_step="Final pass: " + message)

        def finish(status, step, error=None):
            latest = self.store.get(job_id)
            config = dict(latest["config"], final_transcription=status)
            self.store.update(job_id, status=status, current_step=step, error=error,
                              finished_at=utcnow(), config_json=json.dumps(config))

        try:
            if event.is_set():
                raise Cancelled("Final transcription stopped")
            self.store.update(job_id, status="running", started_at=utcnow(),
                              current_step="Final transcription",
                              config_json=json.dumps(dict(job["config"], final_transcription="running")))
            settings = Settings(**{
                **self.settings.__dict__,
                **{key: value for key, value in job["config"].items() if key in self.settings.__dict__},
            })
            settings = replace(settings, diarization_enabled=False,
                               asr_ignore_duration_seconds=0)
            # The graph writes only into staging. Neither failure nor cancellation
            # can truncate the saved live transcript or manual corrections.
            with TemporaryDirectory(prefix="final-pass-", dir=job_dir) as temporary:
                pipeline = TranscriptionPipeline(settings, log, progress, event, self.device_pool)
                result = pipeline.run({
                    "job_id": job_id, "kind": "voice", "job_dir": temporary,
                    "audio_files": [str(job_dir / "audio" / name) for name in job["files"]],
                    "brief_description": job.get("brief_description", ""),
                    "auto_analysis": False, "adjust_volume": False,
                    "source_text": "", "transcript": "", "audio_url": "",
                })
                if event.is_set():
                    raise Cancelled("Final transcription stopped")
                text = result["transcript"]
                atomic_write_text(job_dir / "results-final.txt", text)
                latest = self.store.get(job_id)
                if (not latest["config"].get("manual_edits", True)
                        and read_transcript(job_dir) == initial_text and text.strip()):
                    atomic_write_text(job_dir / "results.txt", text)
                    (job_dir / "summary.txt").unlink(missing_ok=True)
                    step = "Final transcript ready"
                else:
                    step = ("Final transcript ready for review; saved transcript preserved" if text.strip()
                            else "Final pass returned no speech; saved transcript preserved")
                finish("completed", step)
                log("status", step)
        except Cancelled:
            finish("stopped", "Final transcription stopped; saved transcript preserved")
        except Exception:
            log("error", traceback.format_exc())
            finish("failed", "Final transcription failed; saved transcript preserved",
                   "Final transcription failed. The saved audio and transcript are intact; retry the final pass.")
        finally:
            self._finish_run(job_id)

    def submit_reanalysis(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if not job or job["status"] in {"queued", "running", "stopping"}:
            return False
        event = Event()
        with self.lock:
            if job_id in self.events:
                return False
            self.events[job_id] = event
        self.store.update(
            job_id,
            status="queued",
            started_at=None,
            finished_at=None,
            current_step="Queued for summary regeneration",
            error=None,
            cancel_requested=0,
        )
        self.pool.submit(self._run_analysis, job_id, event)
        return True

    def cancel(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if not job or job["status"] not in {"queued", "running", "stopping"}:
            return False
        self.store.request_cancel(job_id)
        with self.lock:
            self.pending_retries.discard(job_id)
            self.retry_volume_settings.pop(job_id, None)
            event = self.events.get(job_id)
        if event:
            event.set()
        return True

    def retry(self, job_id: str, volume_settings: dict | None = None) -> bool:
        job = self.store.get(job_id)
        if not job:
            return False
        with self.lock:
            if volume_settings is not None:
                self.retry_volume_settings[job_id] = volume_settings
            event = self.events.get(job_id)
            if event:
                self.pending_retries.add(job_id)
                self.store.update(
                    job_id,
                    status="stopping",
                    current_step="Stopping before retry…",
                    cancel_requested=1,
                )
                event.set()
                return True
        return self._queue_retry(job_id)

    def _queue_retry(self, job_id: str) -> bool:
        with self.lock:
            volume_settings = self.retry_volume_settings.pop(job_id, None)
        if volume_settings is not None:
            # Apply after the previous worker stops so its audio callbacks cannot
            # overwrite the new settings. Always adjust from original recordings.
            job = self.store.get(job_id)
            config = job["config"]
            adjusted = set(config.get("adjusted_files", []))
            originals = [name for name in job["files"] if name not in adjusted]
            config.update(volume_settings)
            config.update(adjusted_files=[], processing_files=originals)
            self.store.update(job_id, config_json=json.dumps(config), files_json=json.dumps(originals))
        self.store.update(
            job_id,
            status="queued",
            started_at=None,
            finished_at=None,
            current_step="Queued for retry",
            error=None,
            cancel_requested=0,
        )
        return self.submit(job_id)

    def notify_terminal(self, job_id: str) -> None:
        """Invoke the callback with the persisted terminal job, including config."""
        job = self.store.get(job_id)
        if job and job["status"] in {"completed", "failed", "stopped"}:
            try:
                if self.on_stopped is not None:
                    self.on_stopped(job)
            except Exception:
                logging.getLogger(__name__).exception("Job callback failed for %s", job_id)

    def _finish_run(self, job_id: str) -> None:
        # Keep the job active while the callback runs.
        self.notify_terminal(job_id)
        with self.lock:
            self.events.pop(job_id, None)
            should_retry = job_id in self.pending_retries and not self.shutting_down
            self.pending_retries.discard(job_id)
        if should_retry:
            self._queue_retry(job_id)

    def is_active(self, job_id: str) -> bool:
        with self.lock:
            return job_id in self.events

    def _run(self, job_id: str, event: Event) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        job_dir = self.settings.job_dir(job_id)
        log_path = job_dir / "langgraph.log"

        def log(source: str, message: str) -> None:
            stamp = datetime.now(timezone.utc).isoformat()
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} [{source}] {message}\n")
                handle.flush()

        def progress(step: str) -> None:
            self.store.update(job_id, current_step=step)
            log("status", step)

        try:
            if event.is_set() or self.store.get(job_id)["cancel_requested"]:
                raise Cancelled("Job stopped before it started")
            self.store.update(job_id, status="running", started_at=utcnow(), current_step="Starting graph")
            log("langgraph", "Job started")
            # A full retry invalidates any analysis of the previous transcript,
            # including when this run ends after transcription.
            (job_dir / "summary.txt").unlink(missing_ok=True)
            configured = Settings(**{
                **self.settings.__dict__,
                **{key: value for key, value in job["config"].items() if key in self.settings.__dict__},
            })
            processing_files = self._prepare_voice_files(job_id, job, job_dir, event, progress)

            def audio_prepared(original, adjusted, processing) -> None:
                adjusted_files = [path.name for path in adjusted]
                processing_names = [path.name for path in processing]
                files = list(dict.fromkeys(
                    [*(path.name for path in original), *adjusted_files]
                ))
                config = {
                    **job["config"],
                    "adjusted_files": adjusted_files,
                    "processing_files": processing_names,
                }
                self.store.update(
                    job_id,
                    files_json=json.dumps(files),
                    config_json=json.dumps(config),
                )
                job["files"] = files
                job["config"] = config

            pipeline = TranscriptionPipeline(configured, log, progress, event, self.device_pool)
            pipeline.voiceprints = self.voiceprints
            pipeline.run({
                "job_id": job_id,
                "kind": job["kind"],
                "brief_description": job.get("brief_description", ""),
                "custom_prompt": job["config"].get("custom_prompt", ""),
                "auto_analysis": job["config"].get("auto_analysis", True),
                "remove_audio_after_completed": job["config"].get("remove_audio_after_completed", False),
                "job_dir": str(job_dir),
                "audio_files": [str(job_dir / "audio" / name) for name in processing_files],
                "audio_url": job["config"].get("audio_url", ""),
                "adjust_volume": bool(job["config"].get("adjust_volume", job["config"].get("auto_adjust_volume", False))),
                "volume": job["config"].get("volume", "auto"),
                "voiceprint_recognition": bool(
                    job["config"].get("voiceprint_recognition")
                ),
                "audio_prepared": audio_prepared,
                "source_text": "",
                "transcript": "",
                "summary": "",
            })
            if job["config"].get("remove_audio_after_completed"):
                # Keep playback metadata consistent, including partial cleanup failures.
                retained = [name for name in job["files"] if (job_dir / "audio" / name).is_file()]
                config = dict(job["config"])
                for key in ("adjusted_files", "processing_files"):
                    config[key] = [name for name in config.get(key, []) if name in retained]
                self.store.update(job_id, files_json=json.dumps(retained), config_json=json.dumps(config))
            self.store.update(job_id, status="completed", finished_at=utcnow(), current_step="Complete")
            log("status", "Job completed")
        except Cancelled as exc:
            self.store.update(job_id, status="stopped", finished_at=utcnow(), current_step="Stopped", error=str(exc))
            log("status", str(exc))
        except Exception as exc:
            self.store.update(job_id, status="failed", finished_at=utcnow(), current_step="Failed", error=str(exc))
            log("error", traceback.format_exc())
        finally:
            self._finish_run(job_id)

    def _prepare_voice_files(
        self,
        job_id: str,
        job: dict,
        job_dir: Path,
        event: Event,
        progress,
    ) -> list[str]:
        """Create requested volume-adjusted copies inside the background job."""
        processing_files = job["config"].get("processing_files") or job["files"]
        if job["config"].get("audio_url"):
            # Persisted URL filenames describe the last
            # run and must not prevent a fresh download on retry.
            return []
        if job["kind"] != "voice" or not job["config"].get("adjust_volume", job["config"].get("auto_adjust_volume", False)):
            return processing_files
        if (
            job["config"].get("volume", "auto") == "auto"
            and job["config"].get("diarization_enabled", self.settings.diarization_enabled)
        ):
            # Diarize originals, including on retry, then level each speaker chunk.
            adjusted_names = set(job["config"].get("adjusted_files", []))
            return [name for name in job["files"] if name not in adjusted_names]
        if job["config"].get("adjusted_files"):
            return processing_files

        progress("Adjusting audio volume")
        adjusted: list[Path] = []
        try:
            for filename in processing_files:
                if event.is_set() or self.store.get(job_id)["cancel_requested"]:
                    raise Cancelled("Job stopped while adjusting audio volume")
                source = job_dir / "audio" / filename
                adjusted.extend(create_volume_adjusted_copies(
                    [source], volume=job["config"].get("volume", "auto")
                ))
            if event.is_set() or self.store.get(job_id)["cancel_requested"]:
                raise Cancelled("Job stopped while adjusting audio volume")
        except Exception:
            for path in adjusted:
                path.unlink(missing_ok=True)
            raise

        adjusted_files = [path.name for path in adjusted]
        config = {
            **job["config"],
            "adjusted_files": adjusted_files,
            "processing_files": adjusted_files,
        }
        files = list(dict.fromkeys([*job["files"], *adjusted_files]))
        self.store.update(
            job_id,
            files_json=json.dumps(files),
            config_json=json.dumps(config),
        )
        job["files"] = files
        job["config"] = config
        return adjusted_files

    def _run_analysis(self, job_id: str, event: Event) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        job_dir = self.settings.job_dir(job_id)
        log_path = job_dir / "langgraph.log"

        def log(source: str, message: str) -> None:
            stamp = datetime.now(timezone.utc).isoformat()
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} [{source}] {message}\n")
                handle.flush()

        def progress(step: str) -> None:
            self.store.update(job_id, current_step=step)
            log("status", step)

        try:
            if event.is_set():
                raise Cancelled("Summary regeneration stopped before it started")
            transcript = read_transcript(job_dir)
            if not transcript.strip():
                raise ValueError("The edited transcript is empty")
            self.store.update(
                job_id,
                status="running",
                started_at=utcnow(),
                current_step="Regenerating summary",
            )
            log("langgraph", "Summary regeneration started from edited transcript")
            configured = Settings(**{
                **self.settings.__dict__,
                **{key: value for key, value in job["config"].items() if key in self.settings.__dict__},
            })
            pipeline = TranscriptionPipeline(configured, log, progress, event, self.device_pool)
            pipeline.run_analysis_only({
                "job_id": job_id,
                "kind": job["kind"],
                "brief_description": job.get("brief_description", ""),
                "custom_prompt": job["config"].get("custom_prompt", ""),
                "job_dir": str(job_dir),
                "audio_files": [],
                "source_text": transcript,
                "transcript": transcript,
                "summary": "",
            })
            self.store.update(
                job_id,
                status="completed",
                finished_at=utcnow(),
                current_step="Summary regenerated",
            )
            log("status", "Summary regeneration completed")
        except Cancelled as exc:
            self.store.update(
                job_id,
                status="stopped",
                finished_at=utcnow(),
                current_step="Stopped",
                error=str(exc),
            )
            log("status", str(exc))
        except Exception as exc:
            self.store.update(
                job_id,
                status="failed",
                finished_at=utcnow(),
                current_step="Summary regeneration failed",
                error=str(exc),
            )
            log("error", traceback.format_exc())
        finally:
            self._finish_run(job_id)

    def shutdown(self) -> None:
        with self.lock:
            self.shutting_down = True
            self.pending_retries.clear()
            for event in self.events.values():
                event.set()
        self.pool.shutdown(wait=False, cancel_futures=False)
