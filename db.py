import os
import sqlite3
from typing import Optional, Dict, Any
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_DIR = os.environ.get("DATABASE_DIR", "instance")
DATABASE_PATH = os.path.join(DATABASE_DIR, "py_rm.sqlite3")


def get_db_path() -> str:
    os.makedirs(DATABASE_DIR, exist_ok=True)
    return DATABASE_PATH


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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


def seed_default_user_from_env() -> None:
    """Auto-seed admin user if specified in environment variables."""
    admin_user = os.environ.get("ADMIN_USERNAME")
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if admin_user and admin_pass:
        create_or_update_user(admin_user, admin_pass)
        print(f"Auto-seeded user '{admin_user}' from environment.")

