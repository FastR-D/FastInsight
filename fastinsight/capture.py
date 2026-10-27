"""Per-chat @fastinsight capture state (arm / consume / cancel).

Mirrors skills/FastInsight/scripts/pending_capture.py, minus OpenClaw inbound
media. Long-connection events plus optional history polling feed this store.
"""
from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .parse import extract_chat_id

TTL_SECONDS = 30 * 60
PROCESSED_LIMIT = 2048


def now() -> float:
    return time.time()


def _bounded(items: Iterable[str]) -> List[str]:
    values = list(dict.fromkeys(str(item) for item in items if str(item)))
    return values[-PROCESSED_LIMIT:]


class CaptureStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    @contextmanager
    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"sessions": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"sessions": {}}
        sessions = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(sessions, dict):
            return {"sessions": {}}
        return {"sessions": sessions}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _expired(self, record: Dict[str, Any], ts: float) -> bool:
        try:
            armed_at = float(record.get("armed_at") or 0)
            ttl = int(record.get("ttl_seconds") or TTL_SECONDS)
        except (TypeError, ValueError):
            return True
        return ts - armed_at > ttl

    def _expires_in(self, record: Dict[str, Any], ts: float) -> int:
        try:
            armed_at = float(record.get("armed_at") or 0)
            ttl = int(record.get("ttl_seconds") or TTL_SECONDS)
        except (TypeError, ValueError):
            return 0
        return max(0, int(ttl - (ts - armed_at)))

    def _prune(self, data: Dict[str, Any], ts: float) -> int:
        sessions = data.setdefault("sessions", {})
        stale = [
            key
            for key, rec in sessions.items()
            if not isinstance(rec, dict) or self._expired(rec, ts)
        ]
        for key in stale:
            sessions.pop(key, None)
        return len(stale)

    def _aliases(self, session: str, extra: Optional[Iterable[str]] = None) -> List[str]:
        keys: List[str] = []

        def add(value: Optional[str]) -> None:
            text = (value or "").strip()
            if text and text not in keys:
                keys.append(text)

        add(session)
        for item in extra or []:
            add(item)
        chat = extract_chat_id(*keys)
        if chat:
            add(chat)
        return keys

    def _find(self, data: Dict[str, Any], session: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        sessions = data.get("sessions") or {}
        for key in self._aliases(session):
            record = sessions.get(key)
            if isinstance(record, dict):
                return key, record
        return None, None

    def _write(self, data: Dict[str, Any], aliases: Iterable[str], record: Dict[str, Any]) -> None:
        sessions = data.setdefault("sessions", {})
        stored: List[str] = []
        for key in aliases:
            text = str(key).strip()
            if not text:
                continue
            sessions[text] = record
            if text not in stored:
                stored.append(text)
        record["aliases"] = stored

    def _drop(self, data: Dict[str, Any], session: str, record: Optional[Dict[str, Any]] = None) -> bool:
        sessions = data.setdefault("sessions", {})
        keys = set(self._aliases(session))
        if isinstance(record, dict):
            for item in record.get("aliases") or []:
                if item:
                    keys.add(str(item))
        existed = False
        for key in keys:
            if sessions.pop(key, None) is not None:
                existed = True
        return existed

    def arm(
        self,
        session: str,
        chat_id: str = "",
        accept_title: bool = False,
        *,
        mode: str = "capture",
        batch_id: str = "",
        command_message_id: str = "",
        window_start: float = 0.0,
        window_end: float = 0.0,
    ) -> Dict[str, Any]:
        ts = now()
        resolved = extract_chat_id(chat_id, session) or session
        aliases = self._aliases(session, [resolved, chat_id])
        with self._locked():
            data = self._load()
            self._prune(data, ts)
            _, existing = self._find(data, session)
            if isinstance(existing, dict):
                aliases = self._aliases(session, list(aliases) + list(existing.get("aliases") or []))
            record = {
                "status": "armed",
                "armed_at": ts,
                "ttl_seconds": TTL_SECONDS,
                "chat_id": resolved or (existing.get("chat_id") if isinstance(existing, dict) else ""),
                "notify_message_id": existing.get("notify_message_id") if isinstance(existing, dict) else "",
                "accept_title": bool(accept_title),
                "mode": mode,
            }
            if mode == "image_batch":
                previous_payloads = existing.get("processed_payload_ids") if isinstance(existing, dict) else []
                previous_images = existing.get("processed_image_keys") if isinstance(existing, dict) else []
                record.update(
                    {
                        "batch_id": batch_id,
                        "command_message_id": command_message_id,
                        "window_start": float(window_start),
                        "window_end": float(window_end),
                        "processed_payload_ids": _bounded(previous_payloads or []),
                        "processed_image_keys": _bounded(previous_images or []),
                        "batch_claimed_count": 0,
                    }
                )
            if resolved:
                record["chat_id"] = resolved
            self._write(data, aliases, record)
            self._save(data)
        result = {
            "ok": True,
            "action": "arm",
            "armed": True,
            "session": session,
            "chat_id": record.get("chat_id") or "",
            "aliases": record.get("aliases") or aliases,
            "ttl_seconds": TTL_SECONDS,
            "expires_in": TTL_SECONDS,
        }
        if mode == "image_batch":
            result.update(
                {
                    "batch_id": batch_id,
                    "window_start": float(window_start),
                    "window_end": float(window_end),
                }
            )
        return result

    def status(self, session: str) -> Dict[str, Any]:
        ts = now()
        with self._locked():
            data = self._load()
            removed = self._prune(data, ts)
            if removed:
                self._save(data)
            key, record = self._find(data, session)
        if not isinstance(record, dict):
            return {"ok": True, "armed": False, "session": session}
        armed = str(record.get("status") or "armed") == "armed"
        if record.get("mode") == "image_batch":
            try:
                armed = armed and ts <= float(record.get("window_end") or 0)
            except (TypeError, ValueError):
                armed = False
        return {
            "ok": True,
            "armed": armed,
            "session": session,
            "matched_key": key,
            "chat_id": record.get("chat_id") or extract_chat_id(session, key),
            "aliases": record.get("aliases") or [],
            "expires_in": self._expires_in(record, ts),
            "record": record,
        }

    def claim_payload(
        self,
        session: str,
        *,
        batch_id: str,
        payload_id: str,
        message_id: str = "",
        image_key: str = "",
        message_time: float = 0.0,
    ) -> Dict[str, Any]:
        ts = now()
        payload_id = str(payload_id or image_key or message_id).strip()
        image_key = str(image_key or "").strip()
        with self._locked():
            data = self._load()
            self._prune(data, ts)
            _key, record = self._find(data, session)
            if not isinstance(record, dict):
                return {"ok": False, "claimed": False, "reason": "not_armed"}
            if record.get("mode") != "image_batch":
                return {"ok": False, "claimed": False, "reason": "not_image_batch"}
            if str(record.get("batch_id") or "") != str(batch_id):
                return {"ok": False, "claimed": False, "reason": "stale_batch"}
            if str(record.get("status") or "") != "armed":
                return {"ok": False, "claimed": False, "reason": "batch_closed"}
            try:
                window_start = float(record.get("window_start") or 0)
                window_end = float(record.get("window_end") or 0)
            except (TypeError, ValueError):
                return {"ok": False, "claimed": False, "reason": "invalid_window"}
            if ts > window_end:
                record["status"] = "completed"
                record["completed_at"] = ts
                self._save(data)
                return {"ok": False, "claimed": False, "reason": "window_expired"}
            normalized_message_time = float(message_time or 0)
            if normalized_message_time > 1e12:
                normalized_message_time /= 1000.0
            if normalized_message_time and not (
                window_start <= normalized_message_time <= window_end
            ):
                return {"ok": False, "claimed": False, "reason": "outside_window"}

            processed_payloads = list(record.get("processed_payload_ids") or [])
            processed_images = list(record.get("processed_image_keys") or [])
            if payload_id in processed_payloads or (image_key and image_key in processed_images):
                return {"ok": False, "claimed": False, "reason": "already_processed"}

            processed_payloads.append(payload_id)
            if image_key:
                processed_images.append(image_key)
            record["processed_payload_ids"] = _bounded(processed_payloads)
            record["processed_image_keys"] = _bounded(processed_images)
            record["batch_claimed_count"] = int(record.get("batch_claimed_count") or 0) + 1
            self._save(data)
        return {"ok": True, "claimed": True, "payload_id": payload_id}

    def finish_batch(
        self,
        session: str,
        *,
        batch_id: str,
        status: str = "completed",
    ) -> Dict[str, Any]:
        ts = now()
        with self._locked():
            data = self._load()
            self._prune(data, ts)
            _key, record = self._find(data, session)
            if not isinstance(record, dict) or str(record.get("batch_id") or "") != str(batch_id):
                return {"ok": False, "finished": False}
            record["status"] = status if status in {"completed", "cancelled"} else "completed"
            record["completed_at"] = ts
            self._save(data)
            return {
                "ok": True,
                "finished": True,
                "status": record["status"],
                "processed_count": int(record.get("batch_claimed_count") or 0),
            }

    def consume(self, session: str) -> Dict[str, Any]:
        ts = now()
        with self._locked():
            data = self._load()
            self._prune(data, ts)
            _key, record = self._find(data, session)
            existed = self._drop(data, session, record)
            self._save(data)
        if not existed or not isinstance(record, dict):
            return {
                "ok": False,
                "consumed": False,
                "armed": False,
                "session": session,
                "reason": "not_armed",
            }
        return {
            "ok": True,
            "consumed": True,
            "armed": False,
            "session": session,
            "record": record,
        }

    def cancel(self, session: str) -> Dict[str, Any]:
        with self._locked():
            data = self._load()
            _key, record = self._find(data, session)
            existed = self._drop(data, session, record)
            self._save(data)
        return {"ok": True, "cancelled": existed, "armed": False, "session": session}

    def update_notify_id(self, session: str, message_id: str, chat_id: str = "") -> None:
        if not message_id:
            return
        with self._locked():
            data = self._load()
            _key, record = self._find(data, session)
            if not isinstance(record, dict):
                return
            record["notify_message_id"] = message_id
            if chat_id:
                record["chat_id"] = chat_id
            aliases = record.get("aliases") or self._aliases(session, [chat_id])
            self._write(data, aliases, record)
            self._save(data)
