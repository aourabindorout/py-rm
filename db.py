import os
import time
import json
import secrets
import hashlib
import sqlite3
import logging
from typing import Optional, Dict, Any, List
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

DATABASE_DIR = os.environ.get("DATABASE_DIR", "instance")
DATABASE_PATH = os.path.join(DATABASE_DIR, "py_rm.sqlite3")

# Session inactivity timeout (10 minutes)
INACTIVITY_TIMEOUT_SECONDS = int(os.environ.get("SESSION_INACTIVITY_TIMEOUT_SECONDS", 600))

# Global Firestore client cache
_firestore_client = None
_firestore_checked = False


def is_firestore_enabled() -> bool:
    """
    Returns True if running on Cloud Run (K_SERVICE is set) or explicitly enabled.
    Falls back to SQLite if USE_SQLITE is true or credentials are not found.
    """
    global _firestore_client, _firestore_checked

    if os.environ.get("USE_SQLITE", "").lower() in ("1", "true", "yes"):
        return False

    should_use = (
        os.environ.get("USE_FIRESTORE", "").lower() in ("1", "true", "yes")
        or bool(os.environ.get("K_SERVICE"))
        or bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))
    )

    if not should_use:
        return False

    if not _firestore_checked:
        _firestore_checked = True
        try:
            from google.cloud import firestore
            _firestore_client = firestore.Client()
            logger.info("Successfully connected to Google Cloud Firestore.")
        except Exception as e:
            logger.warning("Firestore connection failed (%s: %s). Falling back to SQLite.", type(e).__name__, e)
            _firestore_client = None

    return _firestore_client is not None


def get_firestore_client():
    if is_firestore_enabled():
        return _firestore_client
    return None


# ---------------------------------------------------------
# SQLite Helpers (Local Development & Fallback)
# ---------------------------------------------------------

def get_db_path() -> str:
    os.makedirs(DATABASE_DIR, exist_ok=True)
    return DATABASE_PATH


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    if is_firestore_enabled():
        # Firestore collections are schemaless and auto-created on document insert
        return

    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with get_connection(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_token TEXT UNIQUE NOT NULL,
                user_id TEXT NOT NULL,
                user_agent_hash TEXT,
                ip_address TEXT,
                created_at INTEGER NOT NULL,
                last_active_at INTEGER NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions (session_token);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                data_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            """
        )
        conn.commit()


# ---------------------------------------------------------
# User Operations
# ---------------------------------------------------------

def create_or_update_user(username: str, password: str, db_path: Optional[str] = None) -> bool:
    """Create a new user or update password if the user already exists."""
    clean_username = username.strip().lower()
    if not clean_username or not password:
        raise ValueError("Username and password must not be empty.")

    password_hash = generate_password_hash(password)
    now = int(time.time())

    fs = get_firestore_client()
    if fs:
        user_ref = fs.collection("users").document(clean_username)
        user_ref.set(
            {
                "username": clean_username,
                "password_hash": password_hash,
                "created_at": now,
            },
            merge=True,
        )
        return True

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute(
            """
            INSERT INTO users (username, password_hash)
            VALUES (?, ?)
            ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash;
            """,
            (clean_username, password_hash),
        )
        conn.commit()
    return True


def get_user_by_username(username: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    clean_username = username.strip().lower()
    if not clean_username:
        return None

    fs = get_firestore_client()
    if fs:
        doc = fs.collection("users").document(clean_username).get()
        if doc.exists:
            data = doc.to_dict()
            return {"id": clean_username, "username": data["username"], "password_hash": data["password_hash"]}
        return None

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        cursor = conn.execute(
            "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
            (clean_username,),
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
    return None


def verify_user(username: str, password: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    user = get_user_by_username(username, db_path=db_path)
    if not user:
        return None
    if check_password_hash(user["password_hash"], password):
        return {"id": user["username"], "username": user["username"]}
    return None


# ---------------------------------------------------------
# Session & Inactivity Management
# ---------------------------------------------------------

def _hash_user_agent(user_agent: str) -> str:
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.strip().encode("utf-8")).hexdigest()


def create_session(
    user_id: Any,
    user_agent: str = "",
    ip_address: str = "",
    db_path: Optional[str] = None,
) -> str:
    """Generate and store a new active session token."""
    session_token = secrets.token_urlsafe(32)
    now = int(time.time())
    ua_hash = _hash_user_agent(user_agent)
    username = str(user_id).strip().lower()

    fs = get_firestore_client()
    if fs:
        session_ref = fs.collection("sessions").document(session_token)
        session_ref.set(
            {
                "session_token": session_token,
                "user_id": username,
                "user_agent_hash": ua_hash,
                "ip_address": ip_address,
                "created_at": now,
                "last_active_at": now,
            }
        )
        return session_token

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_token, user_id, user_agent_hash, ip_address, created_at, last_active_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (session_token, username, ua_hash, ip_address, now, now),
        )
        conn.commit()

    return session_token


def validate_and_refresh_session(
    session_token: str,
    user_agent: str = "",
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Validate session token:
    1. Checks if session exists in DB.
    2. Checks if inactivity timeout (10 mins) has elapsed.
    3. Checks User-Agent fingerprint match.
    4. Refreshes last_active_at if valid.
    """
    if not session_token:
        return None

    now = int(time.time())
    fs = get_firestore_client()

    if fs:
        doc_ref = fs.collection("sessions").document(session_token)
        doc = doc_ref.get()
        if not doc.exists:
            return None

        data = doc.to_dict()
        last_active = data.get("last_active_at", 0)

        # 1. Inactivity check (10 minutes)
        if (now - last_active) > INACTIVITY_TIMEOUT_SECONDS:
            doc_ref.delete()
            return None

        # 2. Browser/Device fingerprint check
        stored_ua_hash = data.get("user_agent_hash", "")
        if stored_ua_hash and user_agent:
            current_ua_hash = _hash_user_agent(user_agent)
            if current_ua_hash != stored_ua_hash:
                doc_ref.delete()
                return None

        # 3. Refresh activity timestamp
        doc_ref.update({"last_active_at": now})
        return {"user_id": data["user_id"], "username": data["user_id"]}

    # Fallback to SQLite
    path = db_path or get_db_path()
    with get_connection(path) as conn:
        cursor = conn.execute(
            """
            SELECT session_token, user_id, user_agent_hash, last_active_at
            FROM sessions
            WHERE session_token = ?;
            """,
            (session_token,),
        )
        row = cursor.fetchone()

        if not row:
            return None

        if (now - row["last_active_at"]) > INACTIVITY_TIMEOUT_SECONDS:
            conn.execute("DELETE FROM sessions WHERE session_token = ?;", (session_token,))
            conn.commit()
            return None

        if row["user_agent_hash"] and user_agent:
            current_ua_hash = _hash_user_agent(user_agent)
            if current_ua_hash != row["user_agent_hash"]:
                conn.execute("DELETE FROM sessions WHERE session_token = ?;", (session_token,))
                conn.commit()
                return None

        conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE session_token = ?;",
            (now, session_token),
        )
        conn.commit()

        return {"user_id": row["user_id"], "username": row["user_id"]}


def delete_session(session_token: str, db_path: Optional[str] = None) -> None:
    """Invalidate and remove an active session upon logout."""
    if not session_token:
        return

    fs = get_firestore_client()
    if fs:
        fs.collection("sessions").document(session_token).delete()
        return

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute("DELETE FROM sessions WHERE session_token = ?;", (session_token,))
        conn.commit()


def delete_user_sessions(user_id: Any, db_path: Optional[str] = None) -> None:
    """Terminate all sessions for a specific user (e.g. log out everywhere)."""
    username = str(user_id).strip().lower()

    fs = get_firestore_client()
    if fs:
        sessions = fs.collection("sessions").where("user_id", "==", username).stream()
        for s in sessions:
            s.reference.delete()
        return

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?;", (username,))
        conn.commit()


# ---------------------------------------------------------
# Progress & Roadmap State (Checklists, Quizzes)
# ---------------------------------------------------------

DEFAULT_PROGRESS = {
    "completed_topics": [],
    "checklist": {},
    "quiz_scores": {},
}


def get_user_progress(username: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve learning progress for a user."""
    clean_username = username.strip().lower()

    fs = get_firestore_client()
    if fs:
        doc = fs.collection("progress").document(clean_username).get()
        if doc.exists:
            data = doc.to_dict()
            return {
                "completed_topics": data.get("completed_topics", []),
                "checklist": data.get("checklist", {}),
                "quiz_scores": data.get("quiz_scores", {}),
            }
        return dict(DEFAULT_PROGRESS)

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        cursor = conn.execute("SELECT data_json FROM progress WHERE username = ?", (clean_username,))
        row = cursor.fetchone()
        if row:
            try:
                data = json.loads(row["data_json"])
                return {
                    "completed_topics": data.get("completed_topics", []),
                    "checklist": data.get("checklist", {}),
                    "quiz_scores": data.get("quiz_scores", {}),
                }
            except json.JSONDecodeError:
                pass
    return dict(DEFAULT_PROGRESS)


def save_user_progress(username: str, progress_data: Dict[str, Any], db_path: Optional[str] = None) -> None:
    """Save user progress payload."""
    clean_username = username.strip().lower()
    now = int(time.time())

    clean_data = {
        "completed_topics": list(progress_data.get("completed_topics", [])),
        "checklist": dict(progress_data.get("checklist", {})),
        "quiz_scores": dict(progress_data.get("quiz_scores", {})),
        "updated_at": now,
    }

    fs = get_firestore_client()
    if fs:
        fs.collection("progress").document(clean_username).set(clean_data, merge=True)
        return

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute(
            """
            INSERT INTO progress (username, data_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET data_json = excluded.data_json, updated_at = excluded.updated_at;
            """,
            (clean_username, json.dumps(clean_data), now),
        )
        conn.commit()


def update_checklist_item(username: str, item_id: str, completed: bool, db_path: Optional[str] = None) -> None:
    """Toggle a specific checklist milestone."""
    progress = get_user_progress(username, db_path=db_path)
    progress["checklist"][item_id] = completed
    save_user_progress(username, progress, db_path=db_path)


def update_topic_completion(username: str, topic_id: str, completed: bool, db_path: Optional[str] = None) -> None:
    """Mark a topic as completed or uncompleted."""
    progress = get_user_progress(username, db_path=db_path)
    topics = set(progress.get("completed_topics", []))
    if completed:
        topics.add(topic_id)
    else:
        topics.discard(topic_id)
    progress["completed_topics"] = list(topics)
    save_user_progress(username, progress, db_path=db_path)


def save_quiz_result(
    username: str,
    quiz_id: str,
    score: int,
    total: int,
    passed: bool,
    db_path: Optional[str] = None,
) -> None:
    """Record score and timestamp for a quiz attempt."""
    progress = get_user_progress(username, db_path=db_path)
    progress["quiz_scores"][quiz_id] = {
        "score": score,
        "total": total,
        "passed": passed,
        "timestamp": int(time.time()),
    }
    save_user_progress(username, progress, db_path=db_path)


def seed_default_user_from_env() -> None:
    """Auto-seed admin user if specified in environment variables."""
    admin_user = os.environ.get("ADMIN_USERNAME")
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if admin_user and admin_pass:
        create_or_update_user(admin_user, admin_pass)
        print(f"Auto-seeded user '{admin_user}' from environment.")
