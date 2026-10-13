"""Keep the browser-cookie signing key stable across server restarts."""
import os
from pathlib import Path
import secrets
import tempfile


def persistent_secret_key(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".session-secret"
    if not path.exists():
        # Publish a complete, owner-only file atomically. Concurrent workers
        # must all read the winning key rather than overwrite one another.
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".session-secret-") as temporary:
            temporary.write(secrets.token_hex(32).encode("ascii"))
            temporary.flush()
            os.fsync(temporary.fileno())
            try:
                os.link(temporary.name, path)
            except FileExistsError:
                pass
    key = path.read_text(encoding="ascii").strip()
    if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
        raise RuntimeError(f"Invalid cookie signing key in {path}")
    return key
