"""Feishu Open API helpers on top of the official lark-oapi SDK."""
from __future__ import annotations

import json
import math
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetImageRequest,
    GetMessageResourceRequest,
    ListMessageRequest,
)
from lark_oapi.api.optical_char_recognition.v1 import (
    BasicRecognizeImageRequest,
    BasicRecognizeImageRequestBody,
)
from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.core.model import BaseRequest

from .parse import to_dict

SCOPE_RE = re.compile(r"need scope:\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)
PERM_ERROR_CODES = {230027, 41050, 99991663, 99991672}
IMAGE_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


class FeishuAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        action: str = "",
        code: Any = None,
        msg: str = "",
        scopes: Optional[List[str]] = None,
    ) -> None:
        super().__init__(message)
        self.action = action
        self.code = code
        self.msg = msg
        self.scopes = [item for item in (scopes or []) if item]

    @property
    def permission_denied(self) -> bool:
        try:
            code_int = int(self.code) if self.code is not None else None
        except (TypeError, ValueError):
            code_int = None
        if code_int in PERM_ERROR_CODES or self.scopes:
            return True
        blob = f"{self.msg} {self}".lower()
        return any(
            needle in blob
            for needle in (
                "need scope",
                "lack of necessary permissions",
                "no user authority",
                "permission denied",
            )
        )


def extract_missing_scopes(*values: Any) -> List[str]:
    found: List[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, FeishuAPIError):
            for scope in list(value.scopes) + extract_missing_scopes(str(value), value.msg):
                if scope not in found:
                    found.append(scope)
            try:
                code_int = int(value.code) if value.code is not None else None
            except (TypeError, ValueError):
                code_int = None
            if code_int == 41050 and "im:resource" not in found:
                found.append("im:resource")
            continue
        for match in SCOPE_RE.findall(str(value)):
            if match not in found:
                found.append(match)
    return found


def timeout_hint(scopes: List[str]) -> str:
    if not scopes:
        return "请回复本条提示，或 @机器人 后再发送论文截图/链接。"
    extra = "下载群图片通常还需要 im:resource。" if "im:message.group_msg" in scopes else ""
    return (
        "当前飞书应用缺少权限（"
        + ", ".join(scopes)
        + "）。只在开放平台勾选不会生效，必须到「版本管理」创建版本并发布"
        + "（企业自建应用可能还需管理员审核）。"
        + extra
        + "生效前请回复本条提示，或 @机器人 后再发送论文截图/链接。"
    )


def _raise_if_failed(response: Any, action: str) -> None:
    if response is not None and getattr(response, "success", lambda: False)():
        return
    code = getattr(response, "code", None)
    msg = str(getattr(response, "msg", "") or "")
    error = getattr(response, "error", None)
    extra = ""
    if error is not None:
        extra = str(to_dict(error) or error)
    blob = " ".join(part for part in (msg, extra) if part)
    scopes = extract_missing_scopes(blob)
    raise FeishuAPIError(
        f"{action} failed: code={code} msg={msg or blob or 'unknown'}",
        action=action,
        code=code,
        msg=msg or blob,
        scopes=scopes,
    )


class FeishuApp:
    def __init__(self, app_id: str, app_secret: str, *, capture_dir: Path, bot_open_id: str = "") -> None:
        if not app_id or not app_secret:
            raise SystemExit("请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        self.app_id = app_id
        self.app_secret = app_secret
        self.capture_dir = capture_dir
        self._bot_open_id = bot_open_id
        self.client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

    def bot_open_id(self) -> str:
        if self._bot_open_id:
            return self._bot_open_id
        request = (
            BaseRequest.builder()
            .http_method(HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({AccessTokenType.TENANT})
            .build()
        )
        try:
            response = self.client.request(request)
            raw = getattr(getattr(response, "raw", None), "content", None)
            if raw:
                body = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
                bot = body.get("bot") if isinstance(body, dict) else None
                if not isinstance(bot, dict) and isinstance(body, dict):
                    bot = body.get("data") if isinstance(body.get("data"), dict) else body
                open_id = str((bot or {}).get("open_id") or "").strip()
                if open_id:
                    self._bot_open_id = open_id
                    return open_id
        except Exception:
            pass
        return ""

    def send_text(self, receive_id: str, text: str) -> Dict[str, Any]:
        receive_id_type = "chat_id" if receive_id.startswith("oc_") else "open_id"
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.create(request)
        _raise_if_failed(response, "send text")
        data = to_dict(getattr(response, "data", None))
        return {
            "message_id": str(data.get("message_id") or ""),
            "receive_id_type": receive_id_type,
        }

    def list_messages_page(
        self,
        chat_id: str,
        page_size: int = 20,
        *,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        page_token: str = "",
    ) -> Tuple[List[Dict[str, Any]], str, bool]:
        builder = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .sort_type("ByCreateTimeDesc")
            .page_size(page_size)
        )
        if start_time is not None:
            builder = builder.start_time(str(math.floor(start_time)))
        if end_time is not None:
            builder = builder.end_time(str(math.ceil(end_time)))
        if page_token:
            builder = builder.page_token(page_token)
        response = self.client.im.v1.message.list(builder.build())
        _raise_if_failed(response, "list messages")
        data = to_dict(getattr(response, "data", None))
        items = data.get("items") or []
        return (
            [item for item in items if isinstance(item, dict)],
            str(data.get("page_token") or ""),
            bool(data.get("has_more")),
        )

    def list_messages(self, chat_id: str, page_size: int = 20) -> List[Dict[str, Any]]:
        items, _page_token, _has_more = self.list_messages_page(chat_id, page_size=page_size)
        return items

    def list_messages_between(
        self,
        chat_id: str,
        start_time: float,
        end_time: float,
        *,
        page_size: int = 50,
        max_pages: int = 20,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token = ""
        for _page in range(max(1, max_pages)):
            page, page_token, has_more = self.list_messages_page(
                chat_id,
                page_size=page_size,
                start_time=start_time,
                end_time=end_time,
                page_token=page_token,
            )
            items.extend(page)
            if not has_more or not page_token:
                break
        return items

    def probe_list(self, chat_id: str) -> Dict[str, Any]:
        try:
            items = self.list_messages(chat_id)
            return {
                "ok": True,
                "list_ok": True,
                "list_permission_denied": False,
                "missing_scopes": [],
                "sample_count": len(items),
            }
        except Exception as error:
            scopes = extract_missing_scopes(error)
            denied = isinstance(error, FeishuAPIError) and error.permission_denied
            return {
                "ok": False,
                "list_ok": False,
                "list_permission_denied": denied,
                "missing_scopes": scopes,
                "error": str(error),
            }

    def download_image(self, message_id: str, file_key: str) -> str:
        last_error: Optional[Exception] = None
        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type("image")
                .build()
            )
            response = self.client.im.v1.message_resource.get(request)
            _raise_if_failed(response, "download image")
            data = getattr(response, "file", None)
            name = str(getattr(response, "file_name", "") or "")
            return self._save_image(data, name)
        except Exception as error:
            last_error = error
        try:
            request = GetImageRequest.builder().image_key(file_key).build()
            response = self.client.im.v1.image.get(request)
            _raise_if_failed(response, "download image fallback")
            data = getattr(response, "file", None)
            name = str(getattr(response, "file_name", "") or "")
            return self._save_image(data, name)
        except Exception as error:
            last_error = error
        raise RuntimeError(f"image download failed: {last_error}")

    def ocr_image(self, image_path: str) -> List[str]:
        raw = Path(image_path).read_bytes()
        import base64

        request = (
            BasicRecognizeImageRequest.builder()
            .request_body(
                BasicRecognizeImageRequestBody.builder()
                .image(base64.b64encode(raw).decode("ascii"))
                .build()
            )
            .build()
        )
        response = self.client.optical_char_recognition.v1.image.basic_recognize(request)
        _raise_if_failed(response, "ocr")
        data = to_dict(getattr(response, "data", None))
        texts = data.get("text_list") or []
        return [str(item).strip() for item in texts if str(item).strip()]

    def _save_image(self, data: Any, file_name: str) -> str:
        if hasattr(data, "read"):
            content = data.read()
        elif isinstance(data, (bytes, bytearray)):
            content = bytes(data)
        else:
            raise RuntimeError("empty image download")
        if not content:
            raise RuntimeError("empty image download")
        if content[:1] == b"{" or content.lstrip().startswith(b"{"):
            try:
                err = json.loads(content.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                err = None
            if isinstance(err, dict) and err.get("code") not in (0, None, ""):
                raise FeishuAPIError(
                    f"image download failed: code={err.get('code')} msg={err.get('msg')}",
                    action="image download",
                    code=err.get("code"),
                    msg=str(err.get("msg") or ""),
                    scopes=extract_missing_scopes(err.get("msg")),
                )
        suffix = Path(file_name or "").suffix.lower()
        if suffix not in IMAGE_EXTS:
            if content[:8] == b"\x89PNG\r\n\x1a\n":
                suffix = ".png"
            elif content[:2] == b"\xff\xd8":
                suffix = ".jpg"
            elif content[:6] in (b"GIF87a", b"GIF89a"):
                suffix = ".gif"
            else:
                suffix = ".jpg"
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        path = self.capture_dir / f"{uuid.uuid4().hex}{suffix}"
        path.write_bytes(content)
        return str(path)
