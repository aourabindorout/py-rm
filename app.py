import os
import secrets
from functools import wraps
from typing import Optional
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
    jsonify,
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
    get_user_progress,
    update_checklist_item,
    update_topic_completion,
    save_quiz_result,
)
from curriculum import (
    get_curriculum_data,
    get_topic,
    get_quiz,
    is_valid_checklist_item,
    grade_quiz,
    compute_progress_summary,
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


def _verify_csrf_or_abort(submitted_token: Optional[str] = None) -> None:
    """Validate synchronizer CSRF token or raise HTTP 403."""
    token = submitted_token if submitted_token is not None else request.form.get("csrf_token", "")
    session_csrf = session.get("csrf_token", "")
    if not token or not session_csrf or not secrets.compare_digest(str(token), str(session_csrf)):
        abort(403)


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

    is_api = request.path.startswith("/api/")
    token = session.get("session_token")
    if not token:
        if is_api:
            return jsonify({"error": "Authentication required."}), 401
        return redirect(url_for("login"))

    user = validate_and_refresh_session(
        token,
        user_agent=request.headers.get("User-Agent", ""),
    )
    if not user:
        session.clear()
        if is_api:
            return jsonify({"error": "Session expired or invalid."}), 401
        flash("Your session has expired or was terminated. Please log in again.", "warning")
        return redirect(url_for("login"))

    g.current_user = user


@app.route("/")
def home():
    user = getattr(g, "current_user", None) or {"username": "Learner"}
    curriculum = get_curriculum_data()
    progress = get_user_progress(user["username"])
    summary = compute_progress_summary(progress)
    return render_template(
        "home.html",
        current_user=user,
        curriculum=curriculum,
        summary=summary,
    )


@app.route("/topic/<topic_id>")
def topic_detail(topic_id: str):
    topic, prev_topic, next_topic = get_topic(topic_id)
    if not topic:
        abort(404)

    user = g.current_user
    progress = get_user_progress(user["username"])
    checklist_map = progress.get("checklist", {})
    is_completed = topic_id in set(progress.get("completed_topics", []))

    quiz = topic.get("quiz")
    quiz_result = progress.get("quiz_scores", {}).get(quiz["id"]) if quiz else None

    items = topic.get("checklist", [])
    checklist_done = sum(1 for item in items if checklist_map.get(item["id"]))

    return render_template(
        "topic.html",
        current_user=user,
        topic=topic,
        prev_topic=prev_topic,
        next_topic=next_topic,
        checklist_map=checklist_map,
        checklist_done=checklist_done,
        checklist_total=len(items),
        is_completed=is_completed,
        quiz_result=quiz_result,
    )


@app.route("/topic/<topic_id>/toggle", methods=["POST"])
def toggle_topic(topic_id: str):
    _verify_csrf_or_abort()
    topic, _, _ = get_topic(topic_id)
    if not topic:
        abort(404)

    completed = request.form.get("completed", "").lower() == "true"
    update_topic_completion(g.current_user["username"], topic_id, completed)

    if completed:
        flash(f"Awesome! Marked '{topic['title']}' as completed.", "success")
    else:
        flash(f"Reopened '{topic['title']}' for review.", "info")

    return redirect(url_for("topic_detail", topic_id=topic_id))


@app.route("/api/checklist", methods=["POST"])
def toggle_checklist():
    payload = request.get_json(silent=True) or {}
    csrf_header = request.headers.get("X-CSRF-Token")
    submitted_csrf = csrf_header if csrf_header is not None else payload.get("csrf_token", "")
    _verify_csrf_or_abort(submitted_csrf)

    item_id = str(payload.get("item_id", "")).strip()
    if not item_id or not is_valid_checklist_item(item_id):
        return jsonify({"error": "Invalid checklist item ID."}), 400

    completed = bool(payload.get("completed", False))
    update_checklist_item(g.current_user["username"], item_id, completed)
    return jsonify({"ok": True, "item_id": item_id, "completed": completed})


@app.route("/quiz/<quiz_id>", methods=["GET", "POST"])
def quiz_view(quiz_id: str):
    topic, quiz = get_quiz(quiz_id)
    if not topic or not quiz:
        abort(404)

    user = g.current_user
    _, _, next_topic = get_topic(topic["id"])
    progress = get_user_progress(user["username"])
    previous_result = progress.get("quiz_scores", {}).get(quiz_id)
    is_topic_completed = topic["id"] in set(progress.get("completed_topics", []))

    graded = None
    if request.method == "POST":
        _verify_csrf_or_abort()
        graded = grade_quiz(quiz, request.form)
        save_quiz_result(
            username=user["username"],
            quiz_id=quiz_id,
            score=graded["score"],
            total=graded["total"],
            passed=graded["passed"],
        )
        previous_result = {
            "score": graded["score"],
            "total": graded["total"],
            "passed": graded["passed"],
        }

    return render_template(
        "quiz.html",
        current_user=user,
        topic=topic,
        next_topic=next_topic,
        quiz=quiz,
        graded=graded,
        previous_result=previous_result,
        is_topic_completed=is_topic_completed,
        pass_threshold=get_curriculum_data()["pass_threshold_percent"],
    )


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
    _verify_csrf_or_abort()

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
    _verify_csrf_or_abort()

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
    _verify_csrf_or_abort()

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
