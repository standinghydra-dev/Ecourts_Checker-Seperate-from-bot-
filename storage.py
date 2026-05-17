"""
storage.py — Cases and status persistence.

MongoDB Atlas is the primary store (same database the Telegram bot uses),
so both stay in sync automatically.

Falls back to local JSON files only if MONGODB_URI is not set — useful
for local dry-runs without credentials.

Env var:
  MONGODB_URI  — MongoDB Atlas connection string (same one used by the bot)
"""

import json
import os
import threading
from pathlib import Path

# ── MongoDB setup ─────────────────────────────────────────────────────────────
MONGODB_URI   = os.environ.get("MONGODB_URI", "")
_mongo_client = None
_db           = None
_mongo_lock   = threading.Lock()


def _get_db():
    global _mongo_client, _db
    if _db is not None:
        return _db
    with _mongo_lock:
        if _db is not None:  # double-checked locking
            return _db
        try:
            from pymongo import MongoClient
            _mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
            _mongo_client.admin.command("ping")   # fail fast on bad credentials
            _db = _mongo_client["ecourts_bot"]    # same DB name as the bot
            print("[storage] Connected to MongoDB Atlas")
            return _db
        except Exception as e:
            print(f"[storage] MongoDB connection failed: {e} — falling back to local JSON")
            return None


def _use_mongo() -> bool:
    return bool(MONGODB_URI)


# ── Fallback local file paths ─────────────────────────────────────────────────
_ROOT       = Path(__file__).parent
_lock       = threading.Lock()
CASES_FILE  = _ROOT / "cases.json"
STATUS_FILE = _ROOT / "last_status.json"


# ── Cases ─────────────────────────────────────────────────────────────────────

def load_cases() -> list:
    """Load all tracked cases. MongoDB → JSON fallback."""
    if _use_mongo():
        db = _get_db()
        if db is not None:
            return [
                {k: v for k, v in doc.items() if k != "_id"}
                for doc in db["cases"].find({}, {"_id": 0})
            ]
    with _lock:
        if not CASES_FILE.exists():
            return []
        with open(CASES_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        return [{k: v for k, v in c.items() if k != "_id"} for c in raw]


# ── Status ────────────────────────────────────────────────────────────────────

def load_status() -> dict:
    """Load last-known status for all cases. MongoDB → JSON fallback."""
    if _use_mongo():
        db = _get_db()
        if db is not None:
            doc = db["status"].find_one({"_id": "last_status"})
            return doc.get("data", {}) if doc else {}
    with _lock:
        if not STATUS_FILE.exists():
            return {}
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)


def save_status(status: dict):
    """Persist status. MongoDB → JSON fallback."""
    if _use_mongo():
        db = _get_db()
        if db is not None:
            db["status"].update_one(
                {"_id": "last_status"},
                {"$set": {"data": status}},
                upsert=True,
            )
            return
    with _lock:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, ensure_ascii=False)


def update_status(results: list) -> list:
    """
    Compare results against last-known status, persist new status.
    Returns list of labels where status changed.
    """
    old     = load_status()
    new     = {}
    changes = []

    for r in results:
        cnr = r.get("cnr", "")
        if not cnr:
            continue
        current_key = r.get("case_status", "") + "|" + r.get("next_hearing", "")
        prev_key    = old.get(cnr, {}).get("_key", "")
        if prev_key and current_key != prev_key:
            changes.append(r.get("label", cnr))
        new[cnr] = {**r, "_key": current_key}

    save_status(new)
    return changes
