import os
import time
import json
import secrets
import hashlib
import sqlite3
import logging
from typing import Optional, Dict, Any
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

DATABASE_DIR = os.environ.get("DATABASE_DIR", "instance")
DATABASE_PATH = os.path.join(DATABASE_DIR, "py_rm.sqlite3")

# Session inactivity timeout (10 minutes)
INACTIVITY_TIMEOUT_SECONDS = int(os.environ.get("SESSION_INACTIVITY_TIMEOUT_SECONDS", 600))

# Global Firestore client cache
_firestore_client = None
_firestore_initialized = False


def get_firestore_client():
    """
    Returns an initialized Firestore client if running in GCP and reachable.
    Gracefully returns None if credentials are missing or disabled.
    """
    global _firestore_client, _firestore_initialized

    if os.environ.get("USE_SQLITE", "").lower() in ("1", "true", "yes"):
        return None

    if not _firestore_initialized:
        _firestore_initialized = True
        should_use = (
            os.environ.get("USE_FIRESTORE", "").lower() in ("1", "true", "yes")
            or bool(os.environ.get("K_SERVICE"))
            or bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))
        )
        if should_use:
            try:
                from google.cloud import firestore
                # Try getting project explicitly from env if present
                project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
                _firestore_client = firestore.Client(project=project) if project else firestore.Client()
                logger.info("Firestore client initialized successfully.")
            except Exception as e:
                logger.warning("Could not initialize Firestore client (%s: %s). Falling back to SQLite.", type(e).__name__, e)
                _firestore_client = None

    return _firestore_client


# ---------------------------------------------------------
# SQLite Helpers (Always Available as Resilient Fallback)
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
    """Always ensure SQLite schema is ready as a reliable fallback."""
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with get_connection(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_superuser INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        try:
            conn.execute("ALTER TABLE users ADD COLUMN is_superuser INTEGER NOT NULL DEFAULT 0;")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_token TEXT UNIQUE NOT NULL,
                user_id TEXT NOT NULL,
                user_agent_hash TEXT,
                ip_address TEXT,
                is_superuser INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_active_at INTEGER NOT NULL
            );
            """
        )
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN is_superuser INTEGER NOT NULL DEFAULT 0;")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

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

def create_or_update_user(
    username: str,
    password: str,
    is_superuser: bool = False,
    db_path: Optional[str] = None,
) -> bool:
    """Create a new user or update password with automatic Firestore -> SQLite fallback."""
    clean_username = username.strip().lower()
    if not clean_username or not password:
        raise ValueError("Username and password must not be empty.")

    password_hash = generate_password_hash(password)
    now = int(time.time())

    fs = get_firestore_client()
    if fs:
        try:
            user_ref = fs.collection("users").document(clean_username)
            user_ref.set(
                {
                    "username": clean_username,
                    "password_hash": password_hash,
                    "is_superuser": bool(is_superuser),
                    "created_at": now,
                },
                merge=True,
            )
            return True
        except Exception as e:
            logger.error("Firestore error in create_or_update_user: %s. Falling back to SQLite.", e)

    # SQLite fallback
    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute(
            """
            INSERT INTO users (username, password_hash, is_superuser)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET 
                password_hash = excluded.password_hash,
                is_superuser = excluded.is_superuser;
            """,
            (clean_username, password_hash, 1 if is_superuser else 0),
        )
        conn.commit()
    return True


def delete_user(username: str, db_path: Optional[str] = None) -> bool:
    """Delete a user, invalidate their active sessions, and remove their progress."""
    clean_username = username.strip().lower()
    if not clean_username:
        return False

    # 1. Terminate all active sessions for this user
    delete_user_sessions(clean_username, db_path=db_path)

    # 2. Delete from Firestore if connected
    fs = get_firestore_client()
    if fs:
        try:
            fs.collection("users").document(clean_username).delete()
            fs.collection("progress").document(clean_username).delete()
        except Exception as e:
            logger.error("Firestore error in delete_user: %s. Falling back to SQLite.", e)

    # 3. Delete from SQLite
    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute("DELETE FROM users WHERE username = ?;", (clean_username,))
        conn.execute("DELETE FROM progress WHERE username = ?;", (clean_username,))
        conn.commit()

    return True


def list_all_users_with_progress(db_path: Optional[str] = None) -> list:
    """List all registered users along with their superuser flag and progress payload."""
    results = []
    fs = get_firestore_client()
    if fs:
        try:
            docs = list(fs.collection("users").stream())
            for doc in docs:
                data = doc.to_dict()
                uname = data.get("username", doc.id)
                progress = get_user_progress(uname, db_path=db_path)
                results.append(
                    {
                        "username": uname,
                        "is_superuser": bool(data.get("is_superuser", False)),
                        "created_at": data.get("created_at"),
                        "progress": progress,
                    }
                )
            results.sort(key=lambda u: u["username"])
            return results
        except Exception as e:
            logger.error("Firestore error in list_all_users_with_progress: %s. Falling back to SQLite.", e)

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        cursor = conn.execute(
            "SELECT id, username, is_superuser, created_at FROM users ORDER BY username ASC"
        )
        for row in cursor.fetchall():
            uname = row["username"]
            progress = get_user_progress(uname, db_path=db_path)
            is_super = bool(row["is_superuser"]) if "is_superuser" in row.keys() else False
            results.append(
                {
                    "id": row["id"],
                    "username": uname,
                    "is_superuser": is_super,
                    "created_at": row["created_at"],
                    "progress": progress,
                }
            )
    return results


def get_user_by_username(username: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    clean_username = username.strip().lower()
    if not clean_username:
        return None

    fs = get_firestore_client()
    if fs:
        try:
            doc = fs.collection("users").document(clean_username).get()
            if doc.exists:
                data = doc.to_dict()
                return {
                    "id": clean_username,
                    "username": data["username"],
                    "password_hash": data["password_hash"],
                    "is_superuser": bool(data.get("is_superuser", False)),
                    "created_at": data.get("created_at"),
                }
            return None
        except Exception as e:
            logger.error("Firestore error in get_user_by_username: %s. Falling back to SQLite.", e)

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        cursor = conn.execute(
            "SELECT id, username, password_hash, is_superuser, created_at FROM users WHERE username = ?",
            (clean_username,),
        )
        row = cursor.fetchone()
        if row:
            data = dict(row)
            data["is_superuser"] = bool(data.get("is_superuser", 0))
            return data
    return None


def verify_user(username: str, password: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    user = get_user_by_username(username, db_path=db_path)
    if not user:
        return None
    if check_password_hash(user["password_hash"], password):
        return {
            "id": user["username"],
            "username": user["username"],
            "is_superuser": bool(user.get("is_superuser", False)),
        }
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
    is_superuser: bool = False,
    db_path: Optional[str] = None,
) -> str:
    """Generate and store a new active session token with fallback."""
    session_token = secrets.token_urlsafe(32)
    now = int(time.time())
    ua_hash = _hash_user_agent(user_agent)
    username = str(user_id).strip().lower()

    fs = get_firestore_client()
    if fs:
        try:
            session_ref = fs.collection("sessions").document(session_token)
            session_ref.set(
                {
                    "session_token": session_token,
                    "user_id": username,
                    "user_agent_hash": ua_hash,
                    "ip_address": ip_address,
                    "is_superuser": bool(is_superuser),
                    "created_at": now,
                    "last_active_at": now,
                }
            )
            return session_token
        except Exception as e:
            logger.error("Firestore error in create_session: %s. Falling back to SQLite.", e)

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_token, user_id, user_agent_hash, ip_address, is_superuser, created_at, last_active_at)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (session_token, username, ua_hash, ip_address, 1 if is_superuser else 0, now, now),
        )
        conn.commit()

    return session_token


def validate_and_refresh_session(
    session_token: str,
    user_agent: str = "",
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not session_token:
        return None

    now = int(time.time())
    fs = get_firestore_client()

    if fs:
        try:
            doc_ref = fs.collection("sessions").document(session_token)
            doc = doc_ref.get()
            if doc.exists:
                data = doc.to_dict()
                last_active = data.get("last_active_at", 0)

                # 1. Inactivity check (10 minutes)
                if (now - last_active) > INACTIVITY_TIMEOUT_SECONDS:
                    doc_ref.delete()
                    return None

                # 2. Browser fingerprint check (strict: missing or mismatched User-Agent is rejected)
                stored_ua_hash = data.get("user_agent_hash", "")
                if stored_ua_hash:
                    current_ua_hash = _hash_user_agent(user_agent) if user_agent else ""
                    if current_ua_hash != stored_ua_hash:
                        doc_ref.delete()
                        return None

                # 3. Refresh activity timestamp
                doc_ref.update({"last_active_at": now})
                return {
                    "user_id": data["user_id"],
                    "username": data["user_id"],
                    "is_superuser": bool(data.get("is_superuser", False)),
                }
            return None
        except Exception as e:
            logger.error("Firestore error in validate_and_refresh_session: %s. Falling back to SQLite.", e)

    # SQLite fallback
    path = db_path or get_db_path()
    with get_connection(path) as conn:
        cursor = conn.execute(
            """
            SELECT session_token, user_id, user_agent_hash, is_superuser, last_active_at
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

        # 2. Browser fingerprint check (strict: missing or mismatched User-Agent is rejected)
        stored_ua_hash = row["user_agent_hash"]
        if stored_ua_hash:
            current_ua_hash = _hash_user_agent(user_agent) if user_agent else ""
            if current_ua_hash != stored_ua_hash:
                conn.execute("DELETE FROM sessions WHERE session_token = ?;", (session_token,))
                conn.commit()
                return None

        conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE session_token = ?;",
            (now, session_token),
        )
        conn.commit()

        is_super = bool(row["is_superuser"]) if "is_superuser" in row.keys() else False
        return {
            "user_id": row["user_id"],
            "username": row["user_id"],
            "is_superuser": is_super,
        }


def delete_session(session_token: str, db_path: Optional[str] = None) -> None:
    if not session_token:
        return

    fs = get_firestore_client()
    if fs:
        try:
            fs.collection("sessions").document(session_token).delete()
            return
        except Exception as e:
            logger.error("Firestore error in delete_session: %s. Falling back to SQLite.", e)

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute("DELETE FROM sessions WHERE session_token = ?;", (session_token,))
        conn.commit()


def delete_user_sessions(user_id: Any, db_path: Optional[str] = None) -> None:
    username = str(user_id).strip().lower()

    fs = get_firestore_client()
    if fs:
        try:
            sessions = fs.collection("sessions").where("user_id", "==", username).stream()
            for s in sessions:
                s.reference.delete()
            return
        except Exception as e:
            logger.error("Firestore error in delete_user_sessions: %s. Falling back to SQLite.", e)

    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?;", (username,))
        conn.commit()


# ---------------------------------------------------------
# Progress & Roadmap State
# ---------------------------------------------------------

DEFAULT_PROGRESS = {
    "completed_topics": [],
    "checklist": {},
    "quiz_scores": {},
}


def get_user_progress(username: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    clean_username = username.strip().lower()

    fs = get_firestore_client()
    if fs:
        try:
            doc = fs.collection("progress").document(clean_username).get()
            if doc.exists:
                data = doc.to_dict()
                return {
                    "completed_topics": data.get("completed_topics", []),
                    "checklist": data.get("checklist", {}),
                    "quiz_scores": data.get("quiz_scores", {}),
                }
            return dict(DEFAULT_PROGRESS)
        except Exception as e:
            logger.error("Firestore error in get_user_progress: %s. Falling back to SQLite.", e)

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
        try:
            fs.collection("progress").document(clean_username).set(clean_data, merge=True)
            return
        except Exception as e:
            logger.error("Firestore error in save_user_progress: %s. Falling back to SQLite.", e)

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
    progress = get_user_progress(username, db_path=db_path)
    progress["checklist"][item_id] = completed
    save_user_progress(username, progress, db_path=db_path)


def update_topic_completion(username: str, topic_id: str, completed: bool, db_path: Optional[str] = None) -> None:
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
    progress = get_user_progress(username, db_path=db_path)
    progress["quiz_scores"][quiz_id] = {
        "score": score,
        "total": total,
        "passed": passed,
        "timestamp": int(time.time()),
    }
    save_user_progress(username, progress, db_path=db_path)


def seed_default_user_from_env() -> None:
    """Auto-seed superuser if specified in environment variables safely."""
    superuser = os.environ.get("SUPERUSER_USERNAME")
    superpass = os.environ.get("SUPERUSER_PASSWORD")
    if superuser and superpass:
        try:
            create_or_update_user(superuser, superpass, is_superuser=True)
            print(f"Auto-seeded superuser '{superuser}' from environment.", flush=True)
        except Exception as e:
            logger.error("Failed to auto-seed superuser from environment: %s", e)
