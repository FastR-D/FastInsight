#!/usr/bin/env python3
"""Receive Feishu events over the official long-lived WebSocket connection."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from typing import Any, Dict, Optional, Set

from auto_pipeline import process

try:
    import lark_oapi as lark
except ImportError as error:  # pragma: no cover - environment dependent
    raise SystemExit("缺少 lark-oapi，请先执行: pip install -r requirements.txt") from error


_SEEN_EVENTS: Set[str] = set()
_SEEN_LIMIT = 2048


def _as_dict(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        return data
    try:
        raw = lark.JSON.marshal(data)
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _content_text(payload: Dict[str, Any]) -> str:
    event = payload.get("event", payload)
    message = event.get("message", {}) if isinstance(event, dict) else {}
    content = message.get("content", event.get("content", "")) if isinstance(message, dict) else ""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            pass
    if isinstance(content, dict):
        return str(content.get("text") or content.get("title") or content.get("content") or "").strip()
    return str(content or "").strip()


def _paper_from_event(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    event = payload.get("event", payload)
    message = event.get("message", {}) if isinstance(event, dict) else {}
    if isinstance(message, dict) and message.get("message_type") == "image":
        print(json.dumps({"event": "skipped", "reason": "image_requires_ocr"}, ensure_ascii=False))
        return None
    text = _content_text(payload)
    if not text:
        print(json.dumps({"event": "skipped", "reason": "empty_message"}, ensure_ascii=False))
        return None
    urls = re.findall(r"https?://[^\s<>]+", text)
    title = re.sub(r"https?://[^\s<>]+", "", text).strip().splitlines()[0][:300] if text else ""
    return {"title": title or (urls[0] if urls else "未命名论文"), "url": urls[0] if urls else "", "text": text}


def _event_id(payload: Dict[str, Any]) -> str:
    header = payload.get("header", {})
    if isinstance(header, dict) and header.get("event_id"):
        return str(header["event_id"])
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _process(paper: Dict[str, str]) -> None:
    try:
        result = process(paper)
        print(json.dumps({"event": "processed", "title": paper.get("title"), "result": result}, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - keep the WebSocket callback alive
        print(json.dumps({"event": "failed", "error": str(error)}, ensure_ascii=False))


def on_message(data: Any) -> None:
    """Handle ``im.message.receive_v1`` events dispatched by lark-oapi."""
    payload = _as_dict(data)
    event_id = _event_id(payload)
    if event_id in _SEEN_EVENTS:
        return
    _SEEN_EVENTS.add(event_id)
    if len(_SEEN_EVENTS) > _SEEN_LIMIT:
        _SEEN_EVENTS.pop()
    paper = _paper_from_event(payload)
    if paper:
        threading.Thread(target=_process, args=(paper,), daemon=True).start()


def main() -> None:
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise SystemExit("请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
    client = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
    print("FastInsight Feishu long connection started (no public webhook required)")
    client.start()


if __name__ == "__main__":
    main()
