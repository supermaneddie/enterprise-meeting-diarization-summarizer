"""Console logging configuration shared by application entry points."""

import logging
import os


def configure_logging() -> None:
    level = logging.DEBUG if os.environ.get("DEBUG", "").lower() == "true" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # basicConfig is a no-op when the host already has logging handlers.
    logging.getLogger().setLevel(level)
    logging.getLogger("transcription").setLevel(level)
