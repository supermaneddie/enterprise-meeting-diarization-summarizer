"""LDAP login and a default-deny gate for all Flask requests."""
from datetime import timedelta
import hmac
import secrets
import ssl
from urllib.parse import unquote, urlsplit

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session

from .urls import relative_url, url_for

auth = Blueprint("auth", __name__)


def authenticate(username: str, password: str) -> bool:
    if not username.strip() or not password or "\x00" in username:
        return False
    from ldap3 import Connection, NONE, SIMPLE, Server, Tls
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.dn import escape_rdn

    settings = current_app.config["SETTINGS"]
    connection = None
    try:
        server = Server(
            settings.ldap_server, port=settings.ldap_port, get_info=NONE,
            connect_timeout=5, tls=Tls(validate=ssl.CERT_REQUIRED),
        )
        connection = Connection(
            server, user=settings.ldap_user_dn.format(
                username=escape_rdn(username), base_dn=settings.ldap_base_dn,
            ), password=password, authentication=SIMPLE,
            auto_referrals=False, receive_timeout=5,
        )
        return bool(connection.bind())
    except (LDAPException, OSError, ValueError, KeyError):
        current_app.logger.warning("LDAP authentication failed")
        return False
    finally:
        if connection is not None:
            try:
                connection.unbind()
            except (LDAPException, OSError):
                pass


def _local_next(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return url_for("web.index")
    if (not value.startswith("/") or value.startswith("//") or parsed.netloc
            or parsed.scheme or "\\" in value or any(ord(c) < 32 for c in value)):
        return url_for("web.index")
    decoded = unquote(parsed.path)
    if ("\\" in decoded or any(ord(c) < 32 for c in decoded)
            or any(segment in {".", ".."} for segment in decoded.split("/"))):
        return url_for("web.index")
    return relative_url(value)


@auth.route("/login", methods=["GET", "POST"])
def login():
    if (request.method == "GET" and session.get("username")
            in current_app.config["SETTINGS"].authorized_users):
        return redirect(_local_next(request.args.get("next", "/")))
    error = None
    status = 200
    if request.method == "POST":
        csrf_token = session.get("login_csrf", "")
        if not request.is_json and (not csrf_token or not hmac.compare_digest(
            request.form.get("csrf_token", "").encode(), csrf_token.encode()
        )):
            return "Invalid login form. Reload and try again.", 400
        data = request.get_json(silent=True) if request.is_json else request.form
        if not hasattr(data, "get"):
            data = {}
        username, password = data.get("username", ""), data.get("password", "")
        if (isinstance(username, str) and isinstance(password, str)
                and username in current_app.config["SETTINGS"].authorized_users
                and authenticate(username, password)):
            session.clear()
            session["username"] = username
            session.permanent = (
                data.get("remember_me") is True if request.is_json
                else data.get("remember_me") == "on"
            )
            if request.is_json:
                return jsonify(username=username)
            return redirect(_local_next(request.args.get("next", "/")))
        error = "Unable to sign in. Check your username and password and try again."
        status = 401
        if request.is_json:
            return jsonify(error=error), status
    session.setdefault("login_csrf", secrets.token_urlsafe(32))
    return render_template("login.html", error=error), status


@auth.post("/logout")
def logout():
    session.clear()
    if request.is_json:
        return jsonify(message="Signed out")
    return redirect(url_for("auth.login"))


def init_auth(app):
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=app.config["SETTINGS"].auto_login_expiry),
        SESSION_REFRESH_EACH_REQUEST=False,
    )
    app.register_blueprint(auth)

    def debug_request():
        current_app.logger.debug("\n" + "="*50)
        current_app.logger.debug("      INCOMING REQUEST DETAILS")
        current_app.logger.debug("="*50)

        # 1. Basic Metadata
        current_app.logger.debug(f"Method:      {request.method}")
        current_app.logger.debug(f"URL:         {request.url}")
        current_app.logger.debug(f"Path:        {request.path}")
        current_app.logger.debug(f"Remote Addr: {request.remote_addr}")
        current_app.logger.debug("-"*50)

        # 2. HTTP Headers
        current_app.logger.debug("HEADERS:")
        for key, value in request.headers.items():
            current_app.logger.debug(f"  {key}: {value}")
        current_app.logger.debug("-"*50)

        # 3. Query String Parameters (e.g., ?search=python&page=2)
        current_app.logger.debug("QUERY PARAMS (ARGS):")
        for key, value in request.args.items():
            current_app.logger.debug(f"  {key}: {value}")
        current_app.logger.debug("-"*50)

        # 4. Form Data (from URL-encoded form submissions)
        current_app.logger.debug("FORM DATA:")
        for key, value in request.form.items():
            current_app.logger.debug(f"  {key}: {value}")
        current_app.logger.debug("-"*50)

        # 5. Raw Data or JSON Body
        current_app.logger.debug("BODY / DATA:")
        if request.is_json:
            current_app.logger.debug(f"  JSON: {request.get_json()}")
        else:
            current_app.logger.debug(f"  Raw: {request.data.decode('utf-8', errors='ignore')}")
        current_app.logger.debug("="*50 + "\n")

    @app.before_request
    def require_authentication():
        # Reject cross-origin credential submissions and authenticated mutations.
        origin = request.headers.get("Origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            if origin != request.host_url.rstrip("/"):
                return jsonify(error="Cross-origin request rejected"), 403
        if request.endpoint == "auth.login":
            return None
        if session.get("username"):
            if session["username"] not in current_app.config["SETTINGS"].authorized_users:
                session.clear()
                return jsonify(error="Access denied", login_url=url_for("auth.login")), 403
            return None
        # Browsers can rank signed exchanges above HTML. Compare only the
        # response formats we actually offer instead of the overall best type.
        accepts = request.accept_mimetypes
        if (request.method in {"GET", "HEAD"}
                and accepts["text/html"] > 0
                and accepts["text/html"] >= accepts["application/json"]
                and not request.path.startswith("/api/")
                and not request.environ.get("wsgi.websocket")
                and request.headers.get("Upgrade", "").lower() != "websocket"):
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return jsonify(error="Authentication required", login_url=url_for("auth.login")), 401

    @app.after_request
    def prevent_private_caching(response):
        response.headers["Cache-Control"] = "no-store"
        return response
