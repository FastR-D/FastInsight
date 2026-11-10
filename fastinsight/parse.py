"""Parse Feishu message events the same way the OpenClaw FastInsight skill does."""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

CHAT_RE = re.compile(r"(oc_[a-zA-Z0-9]+)")
USER_RE = re.compile(r"(ou_[a-zA-Z0-9]+)")
URL_RE = re.compile(r"https?://[^\s<>\"\'）)\]]+", re.IGNORECASE)
# Trigger is the @fastinsight mention, not the old /fastinsight slash command.
FASTINSIGHT_RE = re.compile(r"(?<![\w@＠])[@＠]\s*fastinsight(?![\w])", re.IGNORECASE)
CANCEL_RE = re.compile(r"(?:^|[^\w])cancel(?:[^\w]|$)", re.IGNORECASE)
MENTION_RE = re.compile(r"@_user_\d+")
AT_TAG_RE = re.compile(r"<at\b[^>]*>(.*?)</at>", re.IGNORECASE | re.DOTALL)
FASTINSIGHT_NAME_RE = re.compile(r"fastinsight(?:$|[\s_\-].*)", re.IGNORECASE)
SKIP_URL_HOSTS = ("open.feishu.cn", "internal-api-drive-stream.feishu.cn")
ACADEMIC_HOST_PARTS = (
    "arxiv.org",
    "doi.org",
    "openreview.net",
    "aclanthology.org",
    "ieee.org",
    "acm.org",
    "nature.com",
    "science.org",
    "springer.com",
    "sciencedirect.com",
    "biorxiv.org",
    "medrxiv.org",
    "ssrn.com",
    "nih.gov",
    "pnas.org",
    "neurips.cc",
    "mlr.press",
    "aaai.org",
    "semanticscholar.org",
    "openalex.org",
    "aps.org",
    "wiley.com",
    "tandfonline.com",
    "frontiersin.org",
    "plos.org",
    "cell.com",
)
WECHAT_HOSTS = {"mp.weixin.qq.com"}
XHS_HOSTS = {"xiaohongshu.com", "xiaohongshu.cn", "xhslink.com"}


def to_dict(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    try:
        import lark_oapi as lark

        raw = lark.JSON.marshal(data)
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def json_body(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def unique(items: Iterable[str]) -> List[str]:
    found: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in found:
            found.append(text)
    return found


def hostname(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url or "").netloc.lower()
    except (TypeError, ValueError):
        return ""
    if host.startswith("www."):
        return host[4:]
    return host


def is_wechat_url(url: str) -> bool:
    host = hostname(url)
    return host in WECHAT_HOSTS or host.endswith(".mp.weixin.qq.com")


def is_xiaohongshu_url(url: str) -> bool:
    return hostname(url) in XHS_HOSTS


def is_social_article_url(url: str) -> bool:
    return is_wechat_url(url) or is_xiaohongshu_url(url)


def is_academic_url(url: str) -> bool:
    host = hostname(url)
    if not host:
        return False
    for part in ACADEMIC_HOST_PARTS:
        if host == part or host.endswith("." + part):
            return True
    return False


def url_priority(url: str) -> int:
    if is_academic_url(url):
        return 0
    if is_wechat_url(url) or (is_xiaohongshu_url(url) and hostname(url) != "xhslink.com"):
        return 1
    if hostname(url) == "xhslink.com":
        return 2
    return 3


def preferred_url(urls: Iterable[str]) -> str:
    """Prefer academic links, then full WeChat/XHS pages, then short links."""
    best = ""
    best_rank = 99
    for url in urls:
        text = str(url or "").strip()
        if not text:
            continue
        rank = url_priority(text)
        if rank < best_rank:
            best = text
            best_rank = rank
    return best


def extract_chat_id(*values: Optional[str]) -> str:
    for value in values:
        match = CHAT_RE.search(value or "")
        if match:
            return match.group(1)
    return ""


def collect_urls(value: Any, found: Optional[List[str]] = None) -> List[str]:
    found = found if found is not None else []
    if isinstance(value, dict):
        for item in value.values():
            collect_urls(item, found)
    elif isinstance(value, list):
        for item in value:
            collect_urls(item, found)
    elif isinstance(value, str):
        for match in URL_RE.findall(value):
            cleaned = html.unescape(match).rstrip(".,;，。】」")
            host = cleaned.split("/")[2] if "://" in cleaned else ""
            if any(skip in host for skip in SKIP_URL_HOSTS):
                continue
            if cleaned not in found:
                found.append(cleaned)
    return found


def collect_image_keys(value: Any, found: Optional[List[str]] = None) -> List[str]:
    found = found if found is not None else []
    if isinstance(value, dict):
        for key in ("image_key", "file_key", "img_key"):
            image_key = str(value.get(key) or "").strip()
            if image_key and image_key not in found:
                found.append(image_key)
        tag = str(value.get("tag") or "").lower()
        msg_type = str(value.get("msg_type") or value.get("message_type") or "").lower()
        if tag in {"img", "image"} or msg_type == "image":
            image_key = str(value.get("image_key") or value.get("file_key") or "").strip()
            if image_key and image_key not in found:
                found.append(image_key)
        for item in value.values():
            collect_image_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            collect_image_keys(item, found)
    return found



def collect_mentions(value: Any, found: Optional[List[tuple]] = None) -> List[tuple]:
    """Collect (key, display_name) from Feishu mentions and rich-text at tags."""
    found = found if found is not None else []
    if isinstance(value, dict):
        key = str(value.get("key") or "").strip()
        name = str(value.get("name") or value.get("user_name") or value.get("username") or "").strip()
        tag = str(value.get("tag") or "").lower()
        if (key.startswith("@_user_") or tag == "at") and (key or name):
            pair = (key, name)
            if pair not in found:
                found.append(pair)
        for item in value.values():
            collect_mentions(item, found)
    elif isinstance(value, list):
        for item in value:
            collect_mentions(item, found)
    return found


def _name_is_fastinsight(name: str) -> bool:
    return bool(FASTINSIGHT_NAME_RE.match((name or "").strip()))


def _visible_parts(value: Any, parts: Optional[List[str]] = None) -> List[str]:
    """Flatten Feishu text and post bodies, rendering at-tags as @name."""
    parts = parts if parts is not None else []
    if isinstance(value, dict):
        tag = str(value.get("tag") or "").lower()
        if tag == "at":
            name = str(value.get("user_name") or value.get("name") or "").strip()
            if name:
                parts.append(f"@{name}")
            return parts
        for key in ("text", "title"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                parts.append(item)
        for key, item in value.items():
            if key in {"text", "title"}:
                continue
            if isinstance(item, (dict, list)):
                _visible_parts(item, parts)
    elif isinstance(value, list):
        for item in value:
            _visible_parts(item, parts)
    return parts


def _expand_at_markup(text: str) -> str:
    def repl(match: re.Match) -> str:
        name = (match.group(1) or "").strip()
        return f"@{name}" if name else " "

    return AT_TAG_RE.sub(repl, text or "")


def detect_fastinsight_command(text: str, mentions: Iterable[tuple]) -> str:
    """Return "fastinsight", "cancel", or "" for an @fastinsight message.

    Feishu rewrites a real mention to @_user_N and puts the display name in
    mentions[]. Literal "@fastinsight" and rich-text at tags count as well.
    """
    pairs = [(str(key or ""), str(name or "")) for key, name in mentions]
    by_key = {}
    for key, name in pairs:
        if key and name and key not in by_key:
            by_key[key] = name

    def repl(match: re.Match) -> str:
        name = by_key.get(match.group(0), "")
        return f"@{name}" if name else " "

    visible = _expand_at_markup(MENTION_RE.sub(repl, text or ""))
    named = any(_name_is_fastinsight(name) for _key, name in pairs)
    if not named and not FASTINSIGHT_RE.search(visible):
        return ""
    if CANCEL_RE.search(visible):
        return "cancel"
    return "fastinsight"


def message_text(content: Any) -> str:
    parsed = json_body(content)
    if isinstance(parsed, dict):
        for key in ("text", "content", "title"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value
        if set(parsed.keys()) <= {"image_key", "file_key", "img_key", "file_name"}:
            return ""
        return json.dumps(parsed, ensure_ascii=False)
    return str(content or "")


def create_time_seconds(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value > 1e12:
        return value / 1000.0
    return value


@dataclass
class ParsedMessage:
    message_id: str = ""
    event_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    msg_type: str = ""
    create_time: str = ""
    sender_id: str = ""
    sender_type: str = ""
    text: str = ""
    image_keys: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)
    has_fastinsight: bool = False
    command: str = ""  # "", "fastinsight", "cancel"

    @property
    def cleaned_text(self) -> str:
        stripped = FASTINSIGHT_RE.sub(" ", self.text or "")
        stripped = MENTION_RE.sub(" ", stripped)
        return re.sub(r"\s+", " ", stripped).strip()


def _sender_id(sender: Dict[str, Any]) -> str:
    for value in (sender.get("id"), sender.get("sender_id")):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("open_id") or value.get("user_id")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def parse_feishu_message(message: Dict[str, Any], *, event_id: str = "") -> ParsedMessage:
    msg_type = str(message.get("msg_type") or message.get("message_type") or "").lower()
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    content = body.get("content", message.get("content", ""))
    parsed = json_body(content)
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    text = message_text(parsed if parsed != "" else content)
    mentions = collect_mentions(message)
    command_text = text
    if isinstance(parsed, (dict, list)):
        mentions = collect_mentions(parsed, mentions)
        rich = " ".join(part.strip() for part in _visible_parts(parsed) if part.strip())
        if rich and rich != text:
            command_text = f"{text}\n{rich}".strip()
    command = detect_fastinsight_command(command_text, mentions)
    has_cmd = bool(command)
    return ParsedMessage(
        message_id=str(message.get("message_id") or message.get("msg_id") or "").strip(),
        event_id=event_id,
        chat_id=str(message.get("chat_id") or "").strip(),
        chat_type=str(message.get("chat_type") or "").strip(),
        msg_type=msg_type,
        create_time=str(message.get("create_time") or "").strip(),
        sender_id=_sender_id(sender),
        sender_type=str(sender.get("sender_type") or sender.get("id_type") or "").strip(),
        text=text,
        image_keys=collect_image_keys(parsed) if parsed != "" else collect_image_keys(content),
        urls=collect_urls(parsed) if parsed != "" else collect_urls(content),
        has_fastinsight=has_cmd,
        command=command,
    )


def parse_event(data: Any) -> ParsedMessage:
    payload = to_dict(data)
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event", payload)
    if not isinstance(event, dict):
        event = {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    merged = dict(message)
    if sender and "sender" not in merged:
        merged["sender"] = sender
    parsed = parse_feishu_message(merged, event_id=str(header.get("event_id") or "").strip())
    if not parsed.chat_id:
        parsed.chat_id = str(message.get("chat_id") or "").strip()
    return parsed


def interesting_payload(parsed: ParsedMessage) -> Optional[Dict[str, Any]]:
    """Next screenshot/link after @fastinsight. The command message itself is ignored."""
    if parsed.has_fastinsight:
        return None
    if parsed.image_keys:
        return {
            "kind": "image",
            "image_key": parsed.image_keys[0],
            "urls": list(parsed.urls),
            "text": parsed.cleaned_text,
        }
    if parsed.urls:
        return {
            "kind": "url",
            "url": preferred_url(parsed.urls) or parsed.urls[0],
            "urls": list(parsed.urls),
            "text": parsed.cleaned_text,
        }
    return None


def batch_payloads(parsed: ParsedMessage, *, include_urls: bool = True) -> List[Dict[str, Any]]:
    """Return all images, plus one preferred URL when the message has no image.

    A mixed image/link message is one image item carrying the link as metadata,
    which avoids publishing the same paper twice.
    """
    payloads: List[Dict[str, Any]] = []
    for index, image_key in enumerate(parsed.image_keys):
        payloads.append(
            {
                "kind": "image",
                "image_key": image_key,
                "payload_id": image_key or f"{parsed.message_id}:image:{index}",
                "urls": list(parsed.urls),
                "text": parsed.cleaned_text,
            }
        )
    if payloads or not include_urls:
        return payloads
    if parsed.urls:
        url = preferred_url(parsed.urls) or parsed.urls[0]
        payloads.append(
            {
                "kind": "url",
                "url": url,
                "urls": list(parsed.urls),
                "payload_id": f"{parsed.message_id}:url:{url}",
                "text": parsed.cleaned_text,
            }
        )
    return payloads


def capture_payload(parsed: ParsedMessage, *, accept_title: bool = False) -> Optional[Dict[str, Any]]:
    """Like interesting_payload, plus optional plain-title follow-up after OCR/vision fail."""
    payload = interesting_payload(parsed)
    if payload:
        return payload
    if not accept_title or parsed.has_fastinsight:
        return None
    text = parsed.cleaned_text
    if len(text) < 8:
        return None
    return {
        "kind": "url",
        "url": "",
        "urls": [],
        "text": text,
    }
