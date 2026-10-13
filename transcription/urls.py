"""Browser URLs relative to the current request, including stripped proxy mounts."""
from urllib.parse import quote, urlsplit, urlunsplit

from flask import has_request_context, request, url_for as flask_url_for


def relative_url(value: str) -> str:
    """Convert an application-root path to a request-relative URL."""
    parts = urlsplit(value)
    # Climb to the application root from the current request's directory.
    prefix = "../" * request.path.count("/", 1)
    path = prefix + parts.path.lstrip("/")
    # './' also prevents a colon in the first segment becoming a URL scheme.
    return urlunsplit(("", "", "./" + path, parts.query, parts.fragment))


def url_for(endpoint: str, **values) -> str:
    if has_request_context():
        values.setdefault("_external", False)
    value = flask_url_for(endpoint, **values)
    if not has_request_context() or values.get("_external"):
        return value
    parts = urlsplit(value)
    # Werkzeug emits absolute ws(s) URLs even with _external=False when the
    # page request uses HTTP. The browser selects ws/wss for this relative path.
    if parts.scheme in {"ws", "wss"}:
        value = urlunsplit(("", "", parts.path, parts.query, parts.fragment))
    elif parts.netloc:
        return value
    script_root = quote(request.script_root, safe="/:")
    if script_root and value.startswith(script_root + "/"):
        value = value[len(script_root):]
    return relative_url(value)
