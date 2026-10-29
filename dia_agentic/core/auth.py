"""
Simple token-based auth.
No external dependencies — uses hashlib (built-in) for password hashing.
"""
import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import Header, HTTPException

DB_PATH = Path(__file__).parent.parent / "data" / "agentic_runs.db"
TOKEN_TTL_HOURS = 72


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def init_auth_tables():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name  TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token       TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                username    TEXT NOT NULL,
                created_at  TEXT,
                expires_at  TEXT
            )
        """)
        c.commit()


def seed_default_user():
    """Create default user from env if no users exist."""
    username = os.environ.get("DIA_DEFAULT_USER", "admin").strip()
    password = os.environ.get("DIA_DEFAULT_PASSWORD", "admin123").strip()
    display  = os.environ.get("DIA_DEFAULT_DISPLAY", "Admin").strip()

    with _conn() as c:
        exists = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO users (username, password_hash, display_name) VALUES (?,?,?)",
                (username, _hash(password), display)
            )
            c.commit()
            print(f"[DIA Auth] Default user created: {username} / {password}")


def login(username: str, password: str) -> dict | None:
    with _conn() as c:
        user = c.execute(
            "SELECT id, username, display_name FROM users WHERE username=? AND password_hash=?",
            (username.strip(), _hash(password))
        ).fetchone()
        if not user:
            return None

        token = secrets.token_hex(32)
        now   = datetime.now(timezone.utc)
        exp   = now + timedelta(hours=TOKEN_TTL_HOURS)

        c.execute(
            "INSERT INTO sessions (token, user_id, username, created_at, expires_at) VALUES (?,?,?,?,?)",
            (token, user["id"], user["username"], now.isoformat(), exp.isoformat())
        )
        c.commit()

        return {
            "token":        token,
            "user_id":      user["id"],
            "username":     user["username"],
            "display_name": user["display_name"] or user["username"],
            "expires_at":   exp.isoformat(),
        }


def logout(token: str):
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
        c.commit()


def get_session(token: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, username FROM sessions WHERE token=? AND expires_at > datetime('now')",
            (token,)
        ).fetchone()
    if not row:
        return None
    return {"user_id": row["user_id"], "username": row["username"]}


def require_auth(x_auth_token: str = Header(default="")):
    """FastAPI dependency — inject into any route that needs auth."""
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    sess = get_session(x_auth_token)
    if not sess:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return sess


def create_user(username: str, password: str, display_name: str = "") -> bool:
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO users (username, password_hash, display_name) VALUES (?,?,?)",
                (username.strip(), _hash(password), display_name or username)
            )
            c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def list_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id, username, display_name, created_at FROM users ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def change_password(user_id: int, new_password: str):
    with _conn() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (_hash(new_password), user_id))
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        c.commit()
