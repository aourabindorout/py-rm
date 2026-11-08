import os
import secrets
from functools import wraps
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    Response,
    g,
    abort,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from db import (
    init_db,
    verify_user,
    seed_default_user_from_env,
    create_session,
    validate_and_refresh_session,
    delete_session,
    create_or_update_user,
    delete_user,
    list_all_users_with_progress,
)

app = Flask(__name__)

# Secret key for signing session cookies
# Requires SECRET_KEY in production to preserve cookies across Cloud Run cold starts
secret_key_env = os.environ.get("SECRET_KEY")
if not secret_key_env:
    app.logger.warning(
        "WARNING: SECRET_KEY environment variable is not set! Using an ephemeral key. "
        "Sessions will be invalidated whenever Cloud Run scales to zero. "
        "Set SECRET_KEY in Cloud Run console to maintain persistent sessions."
    )
    app.secret_key = secrets.token_hex(32)
else:
    app.secret_key = secret_key_env

# Session cookie security settings
# Automatically enables Secure cookies in Cloud Run (K_SERVICE) or production, while keeping local dev functional
is_cloud_or_prod = bool(os.environ.get("K_SERVICE")) or os.environ.get("FLASK_ENV") == "production"
force_secure = os.environ.get("SECURE_COOKIES", "true" if is_cloud_or_prod else "false").lower() == "true"

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=force_secure and not app.testing,
)

# Apply ProxyFix for Cloud Run / reverse proxies so real client IPs & HTTPS scheme are resolved
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Rate Limiter setup
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["120 per minute"],
    storage_uri="memory://",
)


@app.context_processor
def inject_csrf_token():
    def get_csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return session["csrf_token"]
    return dict(csrf_token=get_csrf_token)


@app.errorhandler(429)
def ratelimit_handler(e):
    return render_template("429.html"), 429


# Whitelisted endpoints that unauthenticated users can access
PUBLIC_PATHS = {"/login", "/healthz"}


@app.before_request
def enforce_authentication():
    # Allow static assets and public endpoints
    if request.path.startswith("/static/"):
        return None

    if request.path in PUBLIC_PATHS:
        return None

    token = session.get("session_token")
    if not token:
        return redirect(url_for("login"))

    user = validate_and_refresh_session(
        token,
        user_agent=request.headers.get("User-Agent", ""),
    )
    if not user:
        session.clear()
        flash("Your session has expired or was terminated. Please log in again.", "warning")
        return redirect(url_for("login"))

    g.current_user = user


@app.route("/")
def home():
    user = getattr(g, "current_user", None) or {"username": "Learner"}
    return render_template("home.html", current_user=user)


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("3 per minute", key_func=get_remote_address, methods=["GET"])
@limiter.limit("25 per minute", key_func=lambda: "global_get_login", methods=["GET"])
@limiter.limit("3 per minute", key_func=get_remote_address, methods=["POST"])
@limiter.limit("15 per minute", key_func=lambda: "global_post_login", methods=["POST"])
def login():
    # If already holding a valid server-side session, skip login
    token = session.get("session_token")
    if token:
        user = validate_and_refresh_session(
            token,
            user_agent=request.headers.get("User-Agent", ""),
        )
        if user:
            return redirect(url_for("home"))
        else:
            session.clear()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = verify_user(username, password)
        if user:
            session.clear()
            token = create_session(
                user_id=user["id"],
                user_agent=request.headers.get("User-Agent", ""),
                ip_address=request.remote_addr or "",
                is_superuser=user.get("is_superuser", False),
            )
            session["session_token"] = token
            session["csrf_token"] = secrets.token_hex(32)
            flash(f"Welcome back, {user['username']}!", "success")
            return redirect(url_for("home"))
        else:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

    return render_template("login.html")


def require_superuser(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = getattr(g, "current_user", None)
        if not user or not user.get("is_superuser"):
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


@app.route("/admin")
@require_superuser
def admin_dashboard():
    users = list_all_users_with_progress()
    user = getattr(g, "current_user", None)
    return render_template("admin.html", current_user=user, users=users)


@app.route("/admin/create-user", methods=["POST"])
@require_superuser
@limiter.limit("10 per minute", key_func=get_remote_address)
def admin_create_user():
    submitted_token = request.form.get("csrf_token", "")
    session_csrf = session.get("csrf_token", "")
    if not submitted_token or not session_csrf or not secrets.compare_digest(submitted_token, session_csrf):
        abort(403)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    is_superuser = request.form.get("is_superuser") == "on"

    if not username or not password:
        flash("Username and password cannot be empty.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        create_or_update_user(username, password, is_superuser=is_superuser)
        role = "Superuser" if is_superuser else "Student"
        flash(f"{role} '{username.lower()}' created successfully.", "success")
    except Exception as e:
        flash(f"Error creating user: {e}", "error")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete-user", methods=["POST"])
@require_superuser
@limiter.limit("10 per minute", key_func=get_remote_address)
def admin_delete_user():
    submitted_token = request.form.get("csrf_token", "")
    session_csrf = session.get("csrf_token", "")
    if not submitted_token or not session_csrf or not secrets.compare_digest(submitted_token, session_csrf):
        abort(403)

    username = request.form.get("username", "").strip().lower()
    current_username = g.current_user["username"].strip().lower()

    if username == current_username:
        flash("You cannot delete your own active superuser account.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        delete_user(username)
        flash(f"User '{username}' and associated progress permanently deleted.", "info")
    except Exception as e:
        flash(f"Error deleting user: {e}", "error")

    return redirect(url_for("admin_dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    # Verify CSRF token to prevent cross-site logout forgery
    submitted_token = request.form.get("csrf_token", "")
    session_csrf = session.get("csrf_token", "")
    if not submitted_token or not session_csrf or not secrets.compare_digest(submitted_token, session_csrf):
        abort(403)

    token = session.get("session_token")
    if token:
        delete_session(token)
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    return Response("OK", status=200, mimetype="text/plain")


# Ensure database and seeds are initialized when app starts
with app.app_context():
    try:
        init_db()
        seed_default_user_from_env()
    except Exception as e:
        app.logger.error("Startup DB initialization warning: %s", e)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
