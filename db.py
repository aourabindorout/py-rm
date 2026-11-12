import os
import time
import secrets
import hashlib
import logging
from typing import Optional, Dict, Any, List
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

# Session inactivity timeout (10 minutes)
INACTIVITY_TIMEOUT_SECONDS = int(os.environ.get("SESSION_INACTIVITY_TIMEOUT_SECONDS", 600))

# Throttling interval for session last_active_at updates (60 seconds)
# Prevents redundant Firestore writes on every single HTTP request
ACTIVITY_UPDATE_INTERVAL_SECONDS = int(os.environ.get("SESSION_ACTIVITY_UPDATE_INTERVAL_SECONDS", 60))

# Global Firestore client cache
_firestore_client = None
_firestore_initialized = False


def get_firestore_client():
    """
    Returns an initialized Google Cloud Firestore client.
    Automatically connects via Application Default Credentials (Cloud Run)
    or local gcp-key.json if available.
    """
    global _firestore_client, _firestore_initialized

    if not _firestore_initialized:
        _firestore_initialized = True

        # If gcp-key.json exists locally and GOOGLE_APPLICATION_CREDENTIALS is not set, auto-configure it
        key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gcp-key.json")
        if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and os.path.exists(key_path):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_path

        try:
            from google.cloud import firestore

            project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
            _firestore_client = firestore.Client(project=project) if project else firestore.Client()
            logger.info("Firestore client initialized successfully.")
        except Exception as e:
            logger.error("Failed to initialize Google Cloud Firestore: %s", e)
            raise RuntimeError(
                f"Could not connect to Google Cloud Firestore ({type(e).__name__}: {e}). "
                "Ensure GCP credentials or GOOGLE_APPLICATION_CREDENTIALS are configured."
            )

    return _firestore_client


def init_db() -> None:
    """Verifies that the Firestore connection is alive and reachable."""
    client = get_firestore_client()
    if client is None:
        raise RuntimeError("Google Cloud Firestore client is uninitialized.")
    logger.info("Firestore connection verified.")


# ---------------------------------------------------------
# User Operations
# ---------------------------------------------------------

def create_or_update_user(
    username: str,
    password: str,
    is_superuser: bool = False,
) -> bool:
    """Create a new user or update password directly in Firestore."""
    clean_username = username.strip().lower()
    if not clean_username or not password:
        raise ValueError("Username and password must not be empty.")

    password_hash = generate_password_hash(password)
    now = int(time.time())

    fs = get_firestore_client()
    user_ref = fs.collection("users").document(clean_username)
    user_data = {
        "username": clean_username,
        "password_hash": password_hash,
        "is_superuser": bool(is_superuser),
        "created_at": now,
    }
    user_ref.set(user_data, merge=True)
    return True


def delete_user(username: str) -> bool:
    """Delete a user, invalidate their active sessions, and remove their progress."""
    clean_username = username.strip().lower()
    if not clean_username:
        return False

    fs = get_firestore_client()

    # 1. Terminate all active sessions for this user
    delete_user_sessions(clean_username)

    # 2. Delete user and progress documents from Firestore
    fs.collection("users").document(clean_username).delete()
    fs.collection("progress").document(clean_username).delete()
    return True


def list_all_users_with_progress() -> List[Dict[str, Any]]:
    """List all registered users from Firestore along with their superuser flag and progress."""
    results = []
    fs = get_firestore_client()
    docs = list(fs.collection("users").stream())
    for doc in docs:
        data = doc.to_dict()
        uname = data.get("username", doc.id)
        progress = get_user_progress(uname)
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


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    clean_username = username.strip().lower()
    if not clean_username:
        return None

    fs = get_firestore_client()
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


def verify_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    user = get_user_by_username(username)
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
) -> str:
    """Generate and store a new active session token in Firestore."""
    session_token = secrets.token_urlsafe(32)
    now = int(time.time())
    ua_hash = _hash_user_agent(user_agent)
    username = str(user_id).strip().lower()

    fs = get_firestore_client()
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


def validate_and_refresh_session(
    session_token: str,
    user_agent: str = "",
) -> Optional[Dict[str, Any]]:
    if not session_token:
        return None

    now = int(time.time())
    fs = get_firestore_client()

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

    # 2. Browser fingerprint check (strict: missing or mismatched User-Agent is rejected)
    stored_ua_hash = data.get("user_agent_hash", "")
    if stored_ua_hash:
        current_ua_hash = _hash_user_agent(user_agent) if user_agent else ""
        if current_ua_hash != stored_ua_hash:
            doc_ref.delete()
            return None

    # 3. Refresh activity timestamp (throttled to avoid redundant Firestore writes on every request)
    if (now - last_active) >= ACTIVITY_UPDATE_INTERVAL_SECONDS:
        doc_ref.update({"last_active_at": now})

    return {
        "user_id": data["user_id"],
        "username": data["user_id"],
        "is_superuser": bool(data.get("is_superuser", False)),
    }


def delete_session(session_token: str) -> None:
    if not session_token:
        return
    fs = get_firestore_client()
    fs.collection("sessions").document(session_token).delete()


def delete_user_sessions(user_id: Any) -> None:
    username = str(user_id).strip().lower()
    fs = get_firestore_client()
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = fs.collection("sessions").where(filter=FieldFilter("user_id", "==", username))
    except Exception:
        query = fs.collection("sessions").where("user_id", "==", username)
    for s in query.stream():
        s.reference.delete()


# ---------------------------------------------------------
# Progress & Roadmap State
# ---------------------------------------------------------

def _empty_progress() -> Dict[str, Any]:
    """Return a fresh, independent empty progress structure."""
    return {
        "completed_topics": [],
        "checklist": {},
        "quiz_scores": {},
    }


def get_user_progress(username: str) -> Dict[str, Any]:
    clean_username = username.strip().lower()
    fs = get_firestore_client()
    doc = fs.collection("progress").document(clean_username).get()
    if doc.exists:
        data = doc.to_dict() or {}
        return {
            "completed_topics": list(data.get("completed_topics") or []),
            "checklist": dict(data.get("checklist") or {}),
            "quiz_scores": dict(data.get("quiz_scores") or {}),
        }
    return _empty_progress()


def save_user_progress(username: str, progress_data: Dict[str, Any]) -> None:
    clean_username = username.strip().lower()
    now = int(time.time())

    clean_data = {
        "completed_topics": list(progress_data.get("completed_topics", [])),
        "checklist": dict(progress_data.get("checklist", {})),
        "quiz_scores": dict(progress_data.get("quiz_scores", {})),
        "updated_at": now,
    }

    fs = get_firestore_client()
    fs.collection("progress").document(clean_username).set(clean_data, merge=True)


def update_checklist_item(username: str, item_id: str, completed: bool) -> None:
    progress = get_user_progress(username)
    progress["checklist"][item_id] = completed
    save_user_progress(username, progress)


def update_topic_completion(username: str, topic_id: str, completed: bool) -> None:
    progress = get_user_progress(username)
    topics = set(progress.get("completed_topics", []))
    if completed:
        topics.add(topic_id)
    else:
        topics.discard(topic_id)
    progress["completed_topics"] = list(topics)
    save_user_progress(username, progress)


def save_quiz_result(
    username: str,
    quiz_id: str,
    score: int,
    total: int,
    passed: bool,
) -> None:
    progress = get_user_progress(username)
    progress["quiz_scores"][quiz_id] = {
        "score": score,
        "total": total,
        "passed": passed,
        "timestamp": int(time.time()),
    }
    save_user_progress(username, progress)


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
