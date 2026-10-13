from __future__ import annotations

import atexit
from dataclasses import replace
from pathlib import Path

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from .logging_config import configure_logging
from .settings import Settings
from .signing import persistent_secret_key
from .auth import init_auth
from .urls import url_for
from .db import JobStore
from .routes import web
from .runner import JobRunner
from .voiceprints import VoiceprintEngine, VoiceprintStore
from .devices import DeviceConfigurationError
from .diarization import DiarizationError, check_remote_connection


def create_app(test_config: dict | None = None) -> Flask:
    configure_logging()
    app = Flask(__name__)
    settings = Settings.load()
    app.config.update(
        SECRET_KEY=settings.secret_key,
        MAX_CONTENT_LENGTH=settings.max_upload_bytes,
        SETTINGS=settings,
        SOCK_SERVER_OPTIONS={"ping_interval": 20, "max_message_size": 8192},
    )
    if test_config:
        app.config.update(test_config)
        if "SETTINGS" in test_config:
            settings = test_config["SETTINGS"]

    secret_key = (test_config or {}).get("SECRET_KEY") or settings.secret_key
    if not secret_key:
        secret_key = persistent_secret_key(settings.data_dir)
    settings = replace(settings, secret_key=secret_key)
    app.config.update(SECRET_KEY=secret_key, SETTINGS=settings)

    if settings.trust_proxy_proto:
        # nginx overwrites X-Forwarded-Proto with the browser-facing scheme.
        # Keep Host and all other forwarded headers outside this trust setting.
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0,
        )

    app.jinja_env.globals["url_for"] = url_for
    init_auth(app)

    try:
        check_remote_connection(
            settings, lambda stage, message: app.logger.info("%s: %s", stage, message)
        )
    except (DiarizationError, DeviceConfigurationError) as exc:
        # Keep settings and existing results accessible when the remote host is down.
        app.logger.warning("Diarization startup check: %s", exc)

    settings.ensure_directories()
    store = JobStore(settings.database_path)
    store.initialize()
    store.mark_abandoned_jobs()
    voiceprint_store = VoiceprintStore(settings.data_dir / "voiceprints")
    voiceprint_store.initialize()
    voiceprint_engine = VoiceprintEngine(voiceprint_store)
    runner = JobRunner(settings, store, voiceprint_engine)

    app.extensions["job_store"] = store
    app.extensions["job_runner"] = runner
    app.extensions["voiceprint_store"] = voiceprint_store
    app.extensions["voiceprint_engine"] = voiceprint_engine
    app.register_blueprint(web)
    atexit.register(runner.shutdown)
    return app
