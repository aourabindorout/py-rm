import os
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
)

app = Flask(__name__)

# Secret key for signing session cookies
app.secret_key = os.environ.get("SECRET_KEY", "py-rm-dev-secret-key-change-in-prod")

# Session cookie security settings
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production"
    or os.environ.get("SECURE_COOKIES", "false").lower() == "true",
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
            )
            session["session_token"] = token
            flash(f"Welcome back, {user['username']}!", "success")
            return redirect(url_for("home"))
        else:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
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
    init_db()
    seed_default_user_from_env()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
