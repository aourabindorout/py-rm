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
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from db import (
    init_db,
    verify_user,
    seed_default_user_from_env,
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

    # Redirect unauthenticated traffic directly to /login
    if not session.get("user_id"):
        return redirect(url_for("login"))


@app.route("/")
def home():
    user = {
        "id": session.get("user_id"),
        "username": session.get("username", "Learner"),
    }
    return render_template("home.html", current_user=user)


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("3 per minute", key_func=get_remote_address, methods=["GET"])
@limiter.limit("25 per minute", key_func=lambda: "global_get_login", methods=["GET"])
@limiter.limit("3 per minute", key_func=get_remote_address, methods=["POST"])
@limiter.limit("15 per minute", key_func=lambda: "global_post_login", methods=["POST"])
def login():
    # If already logged in, send directly to home
    if session.get("user_id"):
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = verify_user(username, password)
        if user:
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            flash(f"Welcome back, {user['username']}!", "success")
            return redirect(url_for("home"))
        else:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
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

