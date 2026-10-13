from __future__ import annotations

import os
import math
from string import Formatter
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import yaml

from .artifacts import DEFAULT_TIMELINE_PATTERN
from .formats import DEFAULT_SUPPORTED_FORMAT, media_suffixes


@dataclass(frozen=True)
class Settings:
    config_path: Path
    host: str
    port: int
    data_dir: Path
    secret_key: str
    max_upload_bytes: int
    max_extracted_bytes: int
    max_archive_files: int
    worker_count: int
    asr_base_url: str
    asr_model: str
    asr_api_key: str
    asr_verify_ssl: bool
    asr_prompt: str
    asr_concurrency: int
    asr_timeout_seconds: int
    asr_chunk_deadline_seconds: int
    asr_max_retries: int
    asr_min_chunk_seconds: int
    asr_max_chunk_seconds: int
    diarization_enabled: bool
    diarization_model: str
    diarization_hf_token: str
    diarization_device: str
    diarization_min_speakers: int | None
    diarization_max_speakers: int | None
    diarization_merge_gap_seconds: float
    voiceprint_enabled: bool
    voiceprint_model: str
    voiceprint_match_threshold: float
    analysis_base_url: str
    analysis_model: str
    analysis_api_key: str
    analysis_verify_ssl: bool
    analysis_prompt: str
    voiceprint_recording_languages: tuple[str, ...] = ("English", "Chinese")
    voiceprint_recording_default_language: str = "Chinese"
    voiceprint_recognition_strategy: str = "centroid"
    diarization_ssh_identity_file: str = ""
    diarization_ssh_port: int = 22

    timeline_pattern: str = DEFAULT_TIMELINE_PATTERN
    asr_ignore_duration_seconds: float = 1.0
    asr_vad_strategy: str = "energy-based"
    asr_supported_format: str = DEFAULT_SUPPORTED_FORMAT
    ldap_server: str = "ldap://ldap.abc.com"
    ldap_port: int = 389
    ldap_base_dn: str = "ou=People,dc=abc,dc=com"
    ldap_user_dn: str = "uid={username},{base_dn}"
    authorized_users: tuple[str, ...] = ()
    auto_login_expiry: int = 7
    trust_proxy_proto: bool = False

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        config_path = Path(path or os.getenv("TRANSCRIPTION_CONFIG", "config.yml")).resolve()
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise RuntimeError(f"Configuration file not found: {config_path}") from exc
        server = raw.get("server", {})
        ldap = raw.get("ldap", {})
        authorized_users = raw.get("authorized_users", [])
        auto_login_expiry = raw.get("auto_login_expiry", 7)
        if type(auto_login_expiry) is not int or not 1 <= auto_login_expiry <= 365000:
            raise ValueError("auto_login_expiry must be a positive integer number of days (at most 365000)")
        if not isinstance(authorized_users, list) or any(
            not isinstance(username, str) or not username.strip()
            or username != username.strip() or "\x00" in username
            for username in authorized_users
        ):
            raise ValueError("authorized_users must be a list of nonempty usernames without surrounding whitespace")
        ldap_server = str(ldap.get("server", "ldap://ldap.abc.com"))
        ldap_port = int(ldap.get("port", 389))
        ldap_user_dn = str(ldap.get("user_dn", "uid={username},{base_dn}"))
        ldap_url = urlsplit(ldap_server)
        if ldap_url.scheme not in {"ldap", "ldaps"} or not ldap_url.hostname:
            raise ValueError("ldap.server must be an ldap:// or ldaps:// URL")
        if not 1 <= ldap_port <= 65535:
            raise ValueError("ldap.port must be between 1 and 65535")
        fields = [field for _, field, _, _ in Formatter().parse(ldap_user_dn) if field is not None]
        if "username" not in fields or set(fields) - {"username", "base_dn"}:
            raise ValueError("ldap.user_dn must contain {username} and may use {base_dn}")
        ldap_user_dn.format(username="validation", base_dn="dc=validation")
        asr = raw.get("asr", {})
        supported_format = ",".join(suffix[1:].upper() for suffix in media_suffixes(
            asr.get("supported_format", DEFAULT_SUPPORTED_FORMAT)
        ))
        vad_strategy = asr.get("vad-strategy", "energy-based")
        if vad_strategy not in ("energy-based", "silero-vad"):
            raise ValueError("asr.vad-strategy must be energy-based or silero-vad")
        diarization = raw.get("diarization", {})
        voiceprint = raw.get("voiceprint", {})
        strategy = voiceprint.get("recognition_strategy", "centroid")
        if strategy not in ("centroid", "best_match", "template_update"):
            raise ValueError("Invalid voiceprint.recognition_strategy")
        recording_languages = voiceprint.get("recording_languages", ["English", "Chinese"])
        if (
            not isinstance(recording_languages, list) or not recording_languages
            or any(language not in ("English", "Chinese") for language in recording_languages)
        ):
            raise ValueError("voiceprint.recording_languages must be a nonempty list of English and/or Chinese")
        recording_languages = tuple(dict.fromkeys(recording_languages))
        recording_default = voiceprint.get(
            "recording_default_language",
            "Chinese" if "Chinese" in recording_languages else recording_languages[0],
        )
        if recording_default not in recording_languages:
            raise ValueError("voiceprint.recording_default_language must be in recording_languages")
        analysis = raw.get("analysis", {})
        data_dir = Path(os.getenv("DATA_DIR", server.get("data_dir", ".")))
        if not data_dir.is_absolute():
            data_dir = config_path.parent / data_dir
        ignore_duration = float(asr.get("ignore_duration_seconds", 1.0))
        if not math.isfinite(ignore_duration) or ignore_duration < 0:
            raise ValueError("asr.ignore_duration_seconds must be a finite nonnegative number")
        return cls(
            auto_login_expiry=auto_login_expiry,
            authorized_users=tuple(dict.fromkeys(authorized_users)),
            ldap_server=ldap_server,
            ldap_port=ldap_port,
            ldap_base_dn=str(ldap.get("base_dn", "ou=People,dc=abc,dc=com")),
            ldap_user_dn=ldap_user_dn,
            asr_vad_strategy=vad_strategy,
            asr_supported_format=supported_format,
            asr_ignore_duration_seconds=ignore_duration,
            timeline_pattern=str(raw.get("timeline_pattern", DEFAULT_TIMELINE_PATTERN)),
            config_path=config_path,
            host=str(server.get("host", "0.0.0.0")),
            port=int(server.get("port", 5000)),
            data_dir=data_dir.resolve(),
            secret_key=os.getenv("TRANSCRIPTION_SECRET_KEY") or server.get("secret_key") or "",
            max_upload_bytes=int(server.get("max_upload_mb", 2048)) * 1024 * 1024,
            max_extracted_bytes=int(server.get("max_extracted_mb", 4096)) * 1024 * 1024,
            max_archive_files=int(server.get("max_archive_files", 1000)),
            worker_count=int(server.get("worker_count", 2)),
            trust_proxy_proto=bool(server.get("trust_proxy_proto", False)),
            asr_base_url=str(asr.get("url", "")).rstrip("/"),
            asr_model=str(asr.get("model", "")),
            asr_api_key=str(asr.get("api_key", "N/A")),
            asr_verify_ssl=bool(asr.get("verify_ssl", False)),
            asr_prompt=str(asr.get("prompt", "")),
            asr_concurrency=max(1, int(asr.get("concurrency", 2))),
            asr_timeout_seconds=max(1, int(asr.get("timeout_seconds", 180))),
            asr_chunk_deadline_seconds=max(
                1, int(asr.get("chunk_deadline_seconds", 30))
            ),
            asr_max_retries=max(0, int(asr.get("max_retries", 2))),
            asr_min_chunk_seconds=max(1, int(asr.get("min_chunk_seconds", 30))),
            asr_max_chunk_seconds=max(1, int(asr.get("max_chunk_seconds", 60))),
            diarization_enabled=bool(diarization.get("enabled", False)),
            diarization_model=str(
                diarization.get("model", "pyannote/speaker-diarization-community-1")
            ),
            diarization_hf_token=str(diarization.get("huggingface_token", "") or ""),
            diarization_device=str(diarization.get("device", "cpu")),
            diarization_ssh_identity_file=str(diarization.get("ssh_identity_file", "") or ""),
            diarization_ssh_port=int(diarization.get("ssh_port", 22)),
            diarization_min_speakers=(
                int(diarization["min_speakers"])
                if diarization.get("min_speakers") is not None else None
            ),
            diarization_max_speakers=(
                int(diarization["max_speakers"])
                if diarization.get("max_speakers") is not None else None
            ),
            diarization_merge_gap_seconds=float(diarization.get("merge_gap_seconds", 0.75)),
            voiceprint_enabled=bool(voiceprint.get("enabled", True)),
            voiceprint_model=str(
                voiceprint.get("model", "speechbrain/spkrec-ecapa-voxceleb")
            ),
            voiceprint_match_threshold=float(voiceprint.get("match_threshold", 0.4)),
            voiceprint_recognition_strategy=strategy,
            voiceprint_recording_languages=recording_languages,
            voiceprint_recording_default_language=recording_default,
            analysis_base_url=str(analysis.get("url", "")).rstrip("/"),
            analysis_model=str(analysis.get("model", "")),
            analysis_api_key=str(analysis.get("api_key", "N/A")),
            analysis_verify_ssl=bool(analysis.get("verify_ssl", False)),
            analysis_prompt=str(analysis.get("prompt", "")),
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "results"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def ensure_directories(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict:
        values = asdict(self)
        values["secret_key"] = "••••••••"
        values["config_path"] = str(self.config_path)
        values["data_dir"] = str(self.data_dir)
        values["asr_api_key"] = "" if self.asr_api_key in {"", "N/A"} else "••••••••"
        values["analysis_api_key"] = "" if self.analysis_api_key in {"", "N/A"} else "••••••••"
        values["diarization_hf_token"] = (
            "" if not self.diarization_hf_token else "••••••••"
        )
        return values

    def with_overrides(self, raw: dict) -> "Settings":
        allowed_text = {
            "asr_base_url", "asr_model", "asr_prompt", "analysis_base_url",
            "analysis_model", "analysis_prompt", "diarization_model", "diarization_device",
            "voiceprint_model", "diarization_ssh_identity_file",
        }
        changes = {key: str(raw[key]).strip() for key in allowed_text if key in raw}
        if "diarization_ssh_port" in raw:
            port = int(raw["diarization_ssh_port"])
            if not 1 <= port <= 65535:
                raise ValueError("SSH port must be between 1 and 65535")
            changes["diarization_ssh_port"] = port
        if "voiceprint_recognition_strategy" in raw:
            strategy = str(raw["voiceprint_recognition_strategy"])
            if strategy not in ("centroid", "best_match", "template_update"):
                raise ValueError("Invalid voiceprint recognition strategy")
            changes["voiceprint_recognition_strategy"] = strategy
        for key in (
            "asr_verify_ssl", "analysis_verify_ssl", "diarization_enabled",
            "voiceprint_enabled",
        ):
            if key in raw:
                changes[key] = str(raw[key]).lower() in {"1", "true", "yes", "on"}
        for key in ("asr_api_key", "analysis_api_key"):
            value = str(raw.get(key, "")).strip()
            if value and value != "••••••••":
                changes[key] = value
        token = str(raw.get("diarization_hf_token", "")).strip()
        if token and token != "••••••••":
            changes["diarization_hf_token"] = token
        for key in ("diarization_min_speakers", "diarization_max_speakers"):
            if key in raw:
                value = str(raw[key]).strip()
                changes[key] = int(value) if value else None
        for key in (
            "asr_concurrency",
            "asr_timeout_seconds",
            "asr_chunk_deadline_seconds",
            "asr_max_retries",
        ):
            if key in raw:
                value = int(str(raw[key]).strip())
                if key == "asr_max_retries":
                    changes[key] = max(0, value)
                else:
                    changes[key] = max(1, value)
        if "voiceprint_match_threshold" in raw:
            threshold = float(str(raw["voiceprint_match_threshold"]).strip())
            changes["voiceprint_match_threshold"] = min(1.0, max(-1.0, threshold))
        return replace(self, **changes)

    def save(self) -> None:
        try:
            stored_data_dir = str(self.data_dir.relative_to(self.config_path.parent)) or "."
        except ValueError:
            stored_data_dir = str(self.data_dir)
        document = {
            "auto_login_expiry": self.auto_login_expiry,
            "authorized_users": list(self.authorized_users),
            "ldap": {key: getattr(self, "ldap_" + key) for key in
                     ("server", "port", "base_dn", "user_dn")},
            "timeline_pattern": self.timeline_pattern,
            "server": {
                "host": self.host,
                "port": self.port,
                "secret_key": self.secret_key,
                "data_dir": stored_data_dir,
                "max_upload_mb": self.max_upload_bytes // (1024 * 1024),
                "max_extracted_mb": self.max_extracted_bytes // (1024 * 1024),
                "max_archive_files": self.max_archive_files,
                "worker_count": self.worker_count,
                "trust_proxy_proto": self.trust_proxy_proto,
            },
            "asr": {
                "supported_format": self.asr_supported_format,
                "url": self.asr_base_url,
                "model": self.asr_model,
                "api_key": self.asr_api_key,
                "verify_ssl": self.asr_verify_ssl,
                "prompt": self.asr_prompt,
                "concurrency": self.asr_concurrency,
                "timeout_seconds": self.asr_timeout_seconds,
                "chunk_deadline_seconds": self.asr_chunk_deadline_seconds,
                "max_retries": self.asr_max_retries,
                "vad-strategy": self.asr_vad_strategy,
                "ignore_duration_seconds": self.asr_ignore_duration_seconds,
                "min_chunk_seconds": self.asr_min_chunk_seconds,
                "max_chunk_seconds": self.asr_max_chunk_seconds,
            },
            "diarization": {
                "enabled": self.diarization_enabled,
                "model": self.diarization_model,
                "huggingface_token": self.diarization_hf_token,
                "device": self.diarization_device,
                "ssh_identity_file": self.diarization_ssh_identity_file,
                "ssh_port": self.diarization_ssh_port,
                "min_speakers": self.diarization_min_speakers,
                "max_speakers": self.diarization_max_speakers,
                "merge_gap_seconds": self.diarization_merge_gap_seconds,
            },
            "voiceprint": {
                "enabled": self.voiceprint_enabled,
                "model": self.voiceprint_model,
                "match_threshold": self.voiceprint_match_threshold,
                "recognition_strategy": self.voiceprint_recognition_strategy,
                "recording_languages": list(self.voiceprint_recording_languages),
                "recording_default_language": self.voiceprint_recording_default_language,
            },
            "analysis": {
                "url": self.analysis_base_url,
                "model": self.analysis_model,
                "api_key": self.analysis_api_key,
                "verify_ssl": self.analysis_verify_ssl,
                "prompt": self.analysis_prompt,
            },
        }
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        temporary.replace(self.config_path)
