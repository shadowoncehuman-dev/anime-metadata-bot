#!/usr/bin/env python3
"""
Allowed-user whitelist — persisted in allowed_users.json alongside the bot.
Admin (TELEGRAM_ALLOWED_CHAT_ID) is always granted access implicitly.
"""

import json, logging, os
from pathlib import Path

log = logging.getLogger(__name__)

_FILE = Path(__file__).parent / "allowed_users.json"
_ADMIN_ID: int = int(os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "0"))


def _load() -> dict:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text())
        except Exception:
            pass
    return {"users": {}}          # {str(user_id): {"name": ..., "username": ...}}


def _save(data: dict) -> None:
    _FILE.write_text(json.dumps(data, indent=2))


# ── public API ────────────────────────────────────────────────────────────────

def is_admin(chat_id: int) -> bool:
    return chat_id == _ADMIN_ID


def is_allowed(chat_id: int) -> bool:
    if is_admin(chat_id):
        return True
    data = _load()
    return str(chat_id) in data["users"]


def add_user(user_id: int, name: str = "", username: str = "") -> bool:
    if user_id == _ADMIN_ID:
        return False          # admin is always allowed, no need to add
    data = _load()
    data["users"][str(user_id)] = {"name": name, "username": username}
    _save(data)
    return True


def remove_user(user_id: int) -> bool:
    data = _load()
    key = str(user_id)
    if key not in data["users"]:
        return False
    del data["users"][key]
    _save(data)
    return True


def list_users() -> list[dict]:
    data = _load()
    result = []
    for uid, info in data["users"].items():
        result.append({"id": int(uid), **info})
    return result


def user_count() -> int:
    return len(_load()["users"])
