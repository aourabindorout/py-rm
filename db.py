import os
import time
import secrets
import hashlib
import sqlite3
from typing import Optional, Dict, Any
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_DIR = os.environ.get("DATABASE_DIR", "instance")
DATABASE_PATH = os.path.join(DATABASE_DIR, "py_rm.sqlite3")

# Session inactivity timeout (10 minutes)
INACTIVITY_TIMEOUT_SECONDS = int(os.environ.get("SESSION_INACTIVITY_TIMEOUT_SECONDS", 600))


def get_db_path() -> str:
    os.makedirs(DATABASE_DIR, exist_ok=True)
    return DATABASE_PATH


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
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
                user_id INTEGER NOT NULL,
                user_agent_hash TEXT,
                ip_address TEXT,
                created_at INTEGER NOT NULL,
                last_active_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions (session_token);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);")
        conn.commit()


def create_or_update_user(username: str, password: str, db_path: Optional[str] = None) -> bool:
    """Create a new user or update password if the user already exists."""
    clean_username = username.strip().lower()
    if not clean_username or not password:
        raise ValueError("Username and password must not be empty.")

    password_hash = generate_password_hash(password)
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
        return {"id": user["id"], "username": user["username"]}
    return None


def _hash_user_agent(user_agent: str) -> str:
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.strip().encode("utf-8")).hexdigest()


def create_session(
    user_id: int,
    user_agent: str = "",
    ip_address: str = "",
    db_path: Optional[str] = None,
) -> str:
    """Generate and store a new active session token."""
    session_token = secrets.token_urlsafe(32)
    now = int(time.time())
    ua_hash = _hash_user_agent(user_agent)
    path = db_path or get_db_path()

    with get_connection(path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_token, user_id, user_agent_hash, ip_address, created_at, last_active_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (session_token, user_id, ua_hash, ip_address, now, now),
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

    path = db_path or get_db_path()
    now = int(time.time())

    with get_connection(path) as conn:
        cursor = conn.execute(
            """
            SELECT s.session_token, s.user_id, s.user_agent_hash, s.last_active_at, u.username
            FROM sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.session_token = ?;
            """,
            (session_token,),
        )
        row = cursor.fetchone()

        if not row:
            return None

        # 1. Inactivity check (10 minutes)
        if (now - row["last_active_at"]) > INACTIVITY_TIMEOUT_SECONDS:
            conn.execute("DELETE FROM sessions WHERE session_token = ?;", (session_token,))
            conn.commit()
            return None

        # 2. Browser/Device fingerprint check (if user-agent was recorded)
        if row["user_agent_hash"] and user_agent:
            current_ua_hash = _hash_user_agent(user_agent)
            if current_ua_hash != row["user_agent_hash"]:
                # Token replayed on an entirely different browser/device
                conn.execute("DELETE FROM sessions WHERE session_token = ?;", (session_token,))
                conn.commit()
                return None

        # 3. Refresh activity timestamp (sliding 10-minute window)
        conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE session_token = ?;",
            (now, session_token),
        )
        conn.commit()

        return {"user_id": row["user_id"], "username": row["username"]}


def delete_session(session_token: str, db_path: Optional[str] = None) -> None:
    """Invalidate and remove an active session upon logout."""
    if not session_token:
        return
    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute("DELETE FROM sessions WHERE session_token = ?;", (session_token,))
        conn.commit()


def delete_user_sessions(user_id: int, db_path: Optional[str] = None) -> None:
    """Terminate all sessions for a specific user (e.g. log out everywhere)."""
    path = db_path or get_db_path()
    with get_connection(path) as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?;", (user_id,))
        conn.commit()


def cleanup_expired_sessions(db_path: Optional[str] = None) -> int:
    """Remove any sessions idle for more than the timeout."""
    path = db_path or get_db_path()
    cutoff = int(time.time()) - INACTIVITY_TIMEOUT_SECONDS
    with get_connection(path) as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE last_active_at < ?;", (cutoff,))
        conn.commit()
        return cursor.rowcount


def seed_default_user_from_env() -> None:
    """Auto-seed admin user if specified in environment variables."""
    admin_user = os.environ.get("ADMIN_USERNAME")
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if admin_user and admin_pass:
        create_or_update_user(admin_user, admin_pass)
        print(f"Auto-seeded user '{admin_user}' from environment.")
