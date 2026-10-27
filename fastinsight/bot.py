#!/usr/bin/env python3
"""FastInsight Feishu bot: official lark-oapi WebSocket long connection.

Single function, copied out of the OpenClaw FastInsight skill:

    @fastinsight  →  process images from one minute before through one minute after
                   →  verify / classify / publish each image to FastResearch
                   →  send per-image confirmation in the source chat

Never DM owners. Never send owner cards. No public webhook is required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

import lark_oapi as lark

from .capture import CaptureStore
from .config import ROOT, env, load_env, require_env
from .feishu_api import FeishuApp, timeout_hint
from .parse import (
    ParsedMessage,
    batch_payloads,
    create_time_seconds,
    parse_event,
    parse_feishu_message,
)
from .pipeline import (
    confirmation_text,
    paper_from_ocr,
    paper_from_url,
    process_paper,
    vision_paper,
)

WORKING_TEXT = "正在识别并发布到 FastResearch…"
CAPTURE_WINDOW_SECONDS = 60
POLL_SECONDS = 2.0
SEEN_LIMIT = 2048

LOGGER = logging.getLogger("fastinsight")


class SeenSet:
    def __init__(self, limit: int = SEEN_LIMIT) -> None:
        self.limit = limit
        self._items: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.Lock()

    def add(self, key: str) -> bool:
        if not key:
            return True
        with self._lock:
            if key in self._items:
                return False
            self._items[key] = None
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
            return True


class FastInsightBot:
    def __init__(self) -> None:
        self.feishu = FeishuApp(
            env("FEISHU_APP_ID"),
            env("FEISHU_APP_SECRET"),
            capture_dir=ROOT / ".captures",
            bot_open_id=env("FEISHU_BOT_OPEN_ID"),
        )
        self.capture = CaptureStore(ROOT / ".pending-capture.json")
        self._seen = SeenSet()
        self._batch_stops: Dict[str, tuple] = {}
        self._batch_lock = threading.Lock()

    def start(self) -> None:
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.on_message)
            .build()
        )
        client = lark.ws.Client(
            self.feishu.app_id,
            self.feishu.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        LOGGER.info("FastInsight Feishu long connection started (no public webhook required)")
        print("FastInsight Feishu long connection started (no public webhook required)", flush=True)
        client.start()

    def on_message(self, data: Any) -> None:
        threading.Thread(target=self._safe_handle, args=(data,), daemon=True).start()

    def _safe_handle(self, data: Any) -> None:
        try:
            self.handle_event(data)
        except Exception as error:  # noqa: BLE001 - keep the WS callback alive
            LOGGER.exception("handle event failed: %s", error)
            print(json.dumps({"event": "failed", "error": str(error)}, ensure_ascii=False), flush=True)

    def handle_event(self, data: Any) -> None:
        parsed = parse_event(data)
        event_id = parsed.event_id or parsed.message_id
        if not event_id:
            event_id = hashlib.sha256(
                json.dumps(
                    {
                        "chat_id": parsed.chat_id,
                        "message_id": parsed.message_id,
                        "text": parsed.text,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        if not self._seen.add(event_id):
            return
        if parsed.sender_type.lower() == "app":
            return
        bot_id = self.feishu.bot_open_id()
        if bot_id and parsed.sender_id == bot_id:
            return
        if not parsed.chat_id:
            print(json.dumps({"event": "skipped", "reason": "missing_chat_id"}, ensure_ascii=False), flush=True)
            return
        self.dispatch(parsed)

    def dispatch(self, parsed: ParsedMessage) -> None:
        chat_id = parsed.chat_id
        if parsed.command == "cancel":
            self._cancel_image_batch(chat_id)
            self.feishu.send_text(chat_id, "已取消 FastInsight 捕获。")
            print(json.dumps({"event": "cancelled", "chat_id": chat_id}, ensure_ascii=False), flush=True)
            return

        if parsed.command == "fastinsight":
            self._start_image_batch(chat_id, parsed)
            return

        batch_id = self._active_image_batch(chat_id)
        if not batch_id:
            return
        self._queue_batch_payloads(parsed, batch_id=batch_id, source="event")

    def _start_image_batch(self, chat_id: str, parsed: ParsedMessage) -> None:
        command_time = create_time_seconds(parsed.create_time) or time.time()
        window_start = command_time - CAPTURE_WINDOW_SECONDS
        window_end = command_time + CAPTURE_WINDOW_SECONDS
        if time.time() > window_end:
            self.feishu.send_text(chat_id, "该 @fastinsight 消息已超过 1 分钟处理窗口，不再处理。")
            return

        batch_id = hashlib.sha256(
            f"{chat_id}:{parsed.event_id or parsed.message_id}:{command_time}".encode("utf-8")
        ).hexdigest()
        stop = threading.Event()
        with self._batch_lock:
            previous = self._batch_stops.get(chat_id)
            self._batch_stops[chat_id] = (batch_id, stop)
        if previous:
            previous_stop = previous[1]
            previous_stop.set()
            self.capture.finish_batch(chat_id, batch_id=str(previous[0]), status="cancelled")

        self.capture.arm(
            chat_id,
            chat_id=chat_id,
            mode="image_batch",
            batch_id=batch_id,
            command_message_id=parsed.message_id,
            window_start=window_start,
            window_end=window_end,
        )
        probe = self.feishu.probe_list(chat_id)
        prompt = (
            f"已开启 FastInsight 批量处理：将分发命令前 {CAPTURE_WINDOW_SECONDS // 60} 分钟至 "
            f"命令后 {CAPTURE_WINDOW_SECONDS // 60} 分钟内的全部图片，每张图片只处理一次。"
        )
        if probe.get("list_permission_denied") or probe.get("missing_scopes"):
            prompt += "\n\n" + timeout_hint(list(probe.get("missing_scopes") or []))
        self.feishu.send_text(chat_id, prompt)

        self._queue_batch_payloads(
            parsed,
            batch_id=batch_id,
            source="command_event",
            include_urls=True,
        )
        threading.Thread(
            target=self._image_batch_loop,
            kwargs={
                "chat_id": chat_id,
                "batch_id": batch_id,
                "command_message_id": parsed.message_id,
                "window_start": window_start,
                "window_end": window_end,
                "stop": stop,
                "can_list": bool(probe.get("list_ok")),
            },
            daemon=True,
        ).start()
        print(
            json.dumps(
                {
                    "event": "image_batch_armed",
                    "chat_id": chat_id,
                    "batch_id": batch_id,
                    "window_start": window_start,
                    "window_end": window_end,
                    "missing_scopes": probe.get("missing_scopes") or [],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def _active_image_batch(self, chat_id: str) -> str:
        status = self.capture.status(chat_id)
        record = status.get("record") if isinstance(status.get("record"), dict) else {}
        if not status.get("armed") or record.get("mode") != "image_batch":
            return ""
        return str(record.get("batch_id") or "")

    def _queue_batch_payloads(
        self,
        parsed: ParsedMessage,
        *,
        batch_id: str,
        source: str,
        include_urls: bool = True,
    ) -> None:
        message_time = create_time_seconds(parsed.create_time) or time.time()
        for payload in batch_payloads(parsed, include_urls=include_urls):
            claim = self.capture.claim_payload(
                parsed.chat_id,
                batch_id=batch_id,
                payload_id=str(payload.get("payload_id") or ""),
                message_id=parsed.message_id,
                image_key=str(payload.get("image_key") or ""),
                message_time=message_time,
            )
            if not claim.get("claimed"):
                continue
            payload["message_id"] = parsed.message_id
            payload["source"] = source
            payload["sender_id"] = parsed.sender_id
            payload["batch_id"] = batch_id
            self._process_async(parsed.chat_id, payload, batch_id=batch_id)

    def _image_batch_loop(
        self,
        *,
        chat_id: str,
        batch_id: str,
        command_message_id: str,
        window_start: float,
        window_end: float,
        stop: threading.Event,
        can_list: bool,
    ) -> None:
        while not stop.is_set() and time.time() <= window_end:
            if can_list:
                try:
                    end_time = min(time.time(), window_end)
                    messages = self.feishu.list_messages_between(
                        chat_id,
                        window_start,
                        end_time,
                    )
                    bot_id = self.feishu.bot_open_id()
                    for message in messages:
                        parsed = parse_feishu_message(message)
                        if not parsed.message_id or parsed.message_id == command_message_id:
                            continue
                        if parsed.sender_type.lower() == "app":
                            continue
                        if bot_id and parsed.sender_id == bot_id:
                            continue
                        self._queue_batch_payloads(
                            parsed,
                            batch_id=batch_id,
                            source="feishu_list",
                        )
                except Exception as error:  # noqa: BLE001
                    LOGGER.warning("image batch list messages failed: %s", error)
            remaining = window_end - time.time()
            if remaining <= 0 or stop.wait(timeout=min(POLL_SECONDS, remaining)):
                break

        if stop.is_set():
            return
        result = self.capture.finish_batch(chat_id, batch_id=batch_id, status="completed")
        with self._batch_lock:
            current = self._batch_stops.get(chat_id)
            if current and current[0] == batch_id:
                self._batch_stops.pop(chat_id, None)
        count = int(result.get("processed_count") or 0)
        self.feishu.send_text(
            chat_id,
            f"图片处理时间窗已结束，本批次共登记 {count} 个处理项；时间窗外的新消息不再处理。",
        )

    def _cancel_image_batch(self, chat_id: str) -> None:
        with self._batch_lock:
            current = self._batch_stops.pop(chat_id, None)
        if not current:
            return
        batch_id, stop = current
        stop.set()
        self.capture.finish_batch(chat_id, batch_id=str(batch_id), status="cancelled")

    def _process_async(
        self,
        chat_id: str,
        payload: Dict[str, Any],
        *,
        batch_id: str = "",
    ) -> None:
        threading.Thread(
            target=self._process,
            args=(chat_id, payload, batch_id),
            daemon=True,
        ).start()

    def _process(self, chat_id: str, payload: Dict[str, Any], batch_id: str = "") -> None:
        print(json.dumps({"event": "captured", "chat_id": chat_id, "payload": _public_payload(payload)}, ensure_ascii=False), flush=True)
        try:
            self.feishu.send_text(chat_id, WORKING_TEXT)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("working notify failed: %s", error)
        try:
            if payload.get("kind") == "image":
                payload["image_path"] = self.feishu.download_image(
                    str(payload.get("message_id") or ""),
                    str(payload.get("image_key") or ""),
                )
            paper = self._payload_to_paper(chat_id, payload)
            if paper is None:
                return
            result = process_paper(paper)
            text = confirmation_text(result)
            self.feishu.send_text(chat_id, text)
            print(
                json.dumps(
                    {
                        "event": "processed",
                        "chat_id": chat_id,
                        "title": (result.get("paper") or {}).get("title"),
                        "result": _public_result(result),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("pipeline failed: %s", error)
            try:
                self.feishu.send_text(chat_id, f"FastInsight 处理失败：{error}")
            except Exception:
                pass
            print(json.dumps({"event": "failed", "chat_id": chat_id, "error": str(error)}, ensure_ascii=False), flush=True)

    def _payload_to_paper(
        self,
        chat_id: str,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        kind = payload.get("kind")
        if kind == "url":
            url = str(payload.get("url") or "")
            text = str(payload.get("text") or "")
            if url:
                return paper_from_url(url, text)
            title = text.splitlines()[0][:300].strip()
            if not title:
                raise RuntimeError("empty title")
            return {"title": title, "url": "", "text": text}
        image_path = str(payload.get("image_path") or "")
        if not image_path:
            raise RuntimeError("image payload missing image_path")
        try:
            paper = vision_paper(image_path)
        except Exception as error:  # noqa: BLE001 - continue to OCR/content fallback
            LOGGER.warning("vision extraction failed: %s", error)
            paper = None
        if paper and (
            paper.get("title")
            or paper.get("url")
            or paper.get("search_query")
            or paper.get("text")
        ):
            return paper
        try:
            texts = self.feishu.ocr_image(image_path)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("feishu ocr failed: %s", error)
            texts = []
        paper = paper_from_ocr(texts, extra_urls=list(payload.get("urls") or []))
        if paper.get("title") or paper.get("url") or paper.get("search_query"):
            if not paper.get("url") and payload.get("urls"):
                paper["url"] = payload["urls"][0]
            return paper
        raise RuntimeError("未能识别论文标题/链接")


def _public_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: payload.get(key)
        for key in ("kind", "url", "urls", "image_path", "source", "message_id")
        if key in payload
    }


def _public_result(result: Dict[str, Any]) -> Dict[str, Any]:
    paper = result.get("paper") if isinstance(result.get("paper"), dict) else {}
    route = result.get("route") if isinstance(result.get("route"), dict) else {}
    return {
        "title": paper.get("title"),
        "url": paper.get("url"),
        "matched_direction": route.get("matched_direction"),
        "delivered": result.get("delivered"),
        "errors": result.get("errors"),
    }


def check_chat(chat_id: str) -> int:
    app = FeishuApp(
        env("FEISHU_APP_ID"),
        env("FEISHU_APP_SECRET"),
        capture_dir=ROOT / ".captures",
        bot_open_id=env("FEISHU_BOT_OPEN_ID"),
    )
    result = {
        "action": "check",
        "chat_id": chat_id,
        "bot_open_id": app.bot_open_id(),
        **app.probe_list(chat_id),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def main() -> None:
    load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="FastInsight Feishu long-connection bot")
    parser.add_argument(
        "--check",
        metavar="CHAT_ID",
        default="",
        help="probe im:message.group_msg by listing recent messages in oc_... and exit",
    )
    args = parser.parse_args()
    require_env("FEISHU_APP_ID", "FEISHU_APP_SECRET")
    if args.check:
        raise SystemExit(check_chat(args.check))
    FastInsightBot().start()


if __name__ == "__main__":
    main()
