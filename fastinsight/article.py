"""Read WeChat / Xiaohongshu articles and identify papers mentioned in them."""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

from .parse import (
    URL_RE,
    is_academic_url,
    is_social_article_url,
    is_wechat_url,
    is_xiaohongshu_url,
    unique,
)

LOGGER = logging.getLogger("fastinsight.article")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
JINA_PREFIX = "https://r.jina.ai/"
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s<>\"']+", re.IGNORECASE)
ARXIV_LABEL_RE = re.compile(
    r"(?:https?://)?arxiv\.org/(?:abs|pdf)/"
    r"([a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?|\d{4}\.\d{4,5}(?!\d)(?:v\d+)?)|"
    r"arXiv:\s*([a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?|\d{4}\.\d{4,5}(?!\d)(?:v\d+)?)",
    re.IGNORECASE,
)
ARXIV_BARE_RE = re.compile(r"(?<![\w./])(\d{4}\.\d{4,5}(?:v\d+)?)(?![\w.])")
WECHAT_BLOCKED = ("环境异常", "当前环境异常", "完成验证后即可继续", "security verify")
SHARE_PATTERNS = (
    re.compile(r"复制后打开【小红书】查看笔记[！!]?", re.I),
    re.compile(r"复制本条信息[，,]*打开【小红书】查看笔记[！!]?", re.I),
    re.compile(r"打开小红书(?:App|APP)?查看", re.I),
    re.compile(r"发布了一篇小红书笔记[，,]?快来看吧[！!]*", re.I),
    re.compile(r"@小红书APP", re.I),
    re.compile(r"点击链接[，,]?再选择浏览器打开", re.I),
)
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}


def clean_share_text(text: str) -> str:
    """Strip WeChat / Xiaohongshu share boilerplate from a pasted caption."""
    cleaned = str(text or "")
    for pattern in SHARE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\n\r-—|·•")
    if "小红书" in cleaned and "笔记" in cleaned and len(cleaned) < 40:
        return ""
    letters = re.sub(r"[\W_]+", "", cleaned, flags=re.UNICODE)
    if len(letters) < 4:
        return ""
    return cleaned


def _clean_doi(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    while value and value[-1] in ".,;:!?)]}>\"'”’":
        value = value[:-1]
    return value.lower().strip()


def _arxiv_id(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"^arXiv:", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"\.pdf$", "", value, flags=re.IGNORECASE)
    return value


class _ArticleHTMLParser(HTMLParser):
    """Collect title, meta, links, scripts, and text keyed by element id/class."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self.meta: Dict[str, str] = {}
        self.links: List[str] = []
        self.scripts: List[str] = []
        self.body_parts: List[str] = []
        self.id_bufs: Dict[str, List[str]] = {}
        self.class_bufs: Dict[str, List[str]] = {}
        self._skip = 0
        self._in_title = False
        self._in_script = False
        self._script_buf: List[str] = []
        self._active_ids: List[Optional[str]] = []
        self._active_classes: List[List[str]] = []

    def _enter(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = {str(k).lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = str(attrs_dict.get("name") or attrs_dict.get("property") or "").strip().lower()
            content = str(attrs_dict.get("content") or "").strip()
            if name and content:
                self.meta[name] = content
        href = str(attrs_dict.get("href") or attrs_dict.get("data-src") or "").strip()
        if href:
            self.links.append(href)
        if tag in VOID_TAGS:
            return
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
            if tag == "script":
                self._in_script = True
                self._script_buf = []
        if tag == "title":
            self._in_title = True
        el_id = str(attrs_dict.get("id") or "").strip()
        classes = str(attrs_dict.get("class") or "").split()
        self._active_ids.append(el_id or None)
        self._active_classes.append(classes)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self._enter(tag.lower(), attrs)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        self._enter(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style", "noscript", "svg"}:
            if self._skip:
                self._skip -= 1
            if tag == "script":
                script = "".join(self._script_buf)
                if script.strip():
                    self.scripts.append(script)
                self._in_script = False
                self._script_buf = []
        if self._active_ids:
            self._active_ids.pop()
        if self._active_classes:
            self._active_classes.pop()

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_buf.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self.title_parts.append(data)
        text = " ".join(str(data or "").split())
        if not text:
            return
        self.body_parts.append(text)
        for el_id in self._active_ids:
            if el_id:
                self.id_bufs.setdefault(el_id, []).append(text)
        for classes in self._active_classes:
            for cls in classes:
                self.class_bufs.setdefault(cls, []).append(text)

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())


def _join(parts: Optional[List[str]]) -> str:
    return " ".join(part for part in (parts or []) if part).strip()


def _looks_blocked(html: str, url: str) -> bool:
    if not html:
        return True
    if is_wechat_url(url) and any(marker in html for marker in WECHAT_BLOCKED):
        return True
    return False


def _http_get(url: str, timeout: int = 15, extra_headers: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    if is_wechat_url(url):
        headers["Referer"] = "https://mp.weixin.qq.com/"
    elif is_xiaohongshu_url(url):
        headers["Referer"] = "https://www.xiaohongshu.com/"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        final_url = str(response.geturl() or url)
        ctype = str(response.headers.get("Content-Type") or "")
    match = re.search(r"charset=([\w-]+)", ctype, re.I)
    encoding = (match.group(1) if match else "utf-8").lower()
    if encoding in {"gb2312", "gbk", "gb18030"}:
        encoding = "gb18030"
    try:
        html = raw.decode(encoding, "replace")
    except LookupError:
        html = raw.decode("utf-8", "replace")
    return html, final_url


def fetch_html(url: str, timeout: int = 15) -> Tuple[str, str]:
    """Fetch a page. Returns ``(html, final_url)`` after redirects."""
    return _http_get(url, timeout=timeout)


def fetch_via_jina(url: str, timeout: int = 15) -> str:
    """Readable-text fallback for anti-bot article pages."""
    target = url if url.startswith("http") else "https://" + url
    jina_url = JINA_PREFIX + target
    html, _final = _http_get(
        jina_url,
        timeout=timeout,
        extra_headers={"Accept": "text/plain, text/markdown;q=0.9, */*;q=0.8"},
    )
    return html


def _parse_jina_markdown(raw: str) -> Dict[str, str]:
    title = ""
    source = ""
    lines = str(raw or "").splitlines()
    body_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith("title:"):
            title = stripped.split(":", 1)[1].strip()
        elif stripped.lower().startswith("url source:"):
            source = stripped.split(":", 1)[1].strip()
        elif stripped.lower().startswith("markdown content:"):
            body_start = index + 1
            break
    body = "\n".join(lines[body_start:] if body_start else lines).strip()
    return {"title": title, "url": source, "text": body}


def _extract_json_object(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = (
        text.replace("undefined", "null")
        .replace("void 0", "null")
        .replace("\n", " ")
    )
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def _xhs_note_from_state(state: Any) -> Dict[str, str]:
    found: List[Dict[str, str]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 12 or node is None:
            return
        if isinstance(node, dict):
            title = node.get("title") or node.get("displayTitle") or node.get("name")
            desc = (
                node.get("desc")
                or node.get("description")
                or node.get("content")
                or node.get("text")
            )
            title_text = title if isinstance(title, str) else ""
            desc_text = desc if isinstance(desc, str) else ""
            if title_text or desc_text:
                found.append({"title": title_text.strip(), "desc": desc_text.strip()})
            note = node.get("note")
            if isinstance(note, dict):
                walk(note, depth + 1)
            detail = node.get("noteDetailMap") or node.get("noteDetail")
            if isinstance(detail, dict):
                for item in detail.values():
                    walk(item, depth + 1)
            if depth < 4:
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:30]:
                walk(item, depth + 1)

    walk(state)
    if not found:
        return {}
    found.sort(key=lambda item: len(item.get("desc") or "") + len(item.get("title") or ""), reverse=True)
    return found[0]


def _initial_state_note(scripts: List[str]) -> Dict[str, str]:
    for script in scripts:
        match = re.search(
            r"window\.__INITIAL_STATE__\s*=\s*JSON\.parse\((['\"])(.*?)\1\)",
            script,
            re.DOTALL,
        )
        raw = ""
        if match:
            raw = match.group(2).encode("utf-8").decode("unicode_escape")
        else:
            match = re.search(
                r"window\.__INITIAL_STATE__\s*=\s*(\{.*\})\s*;?\s*$",
                script.strip(),
                re.DOTALL,
            )
            if match:
                raw = match.group(1)
        if not raw:
            if "__INITIAL_STATE__" not in script:
                continue
            start = script.find("{")
            end = script.rfind("}")
            if start >= 0 and end > start:
                raw = script[start : end + 1]
        state = _extract_json_object(raw) if raw else None
        if state is None:
            continue
        note = _xhs_note_from_state(state)
        if note:
            return note
    return {}


def parse_article_html(url: str, html: str, final_url: str = "") -> Dict[str, Any]:
    """Parse a WeChat / Xiaohongshu / generic HTML article into title + text."""
    parser = _ArticleHTMLParser()
    try:
        parser.feed(html or "")
    except Exception:  # noqa: BLE001 - best-effort HTML
        pass
    meta = parser.meta or {}
    source_url = final_url or url
    source_kind = "web"
    if is_wechat_url(source_url) or is_wechat_url(url):
        source_kind = "wechat"
    elif is_xiaohongshu_url(source_url) or is_xiaohongshu_url(url):
        source_kind = "xiaohongshu"

    title = (
        _join(parser.id_bufs.get("activity-name"))
        or _join(parser.class_bufs.get("rich_media_title"))
        or meta.get("og:title")
        or meta.get("twitter:title")
        or parser.title
        or ""
    )
    msg_title = re.search(r"var\s+msg_title\s*=\s*['\"](.*?)['\"]", html or "")
    if not title and msg_title:
        title = msg_title.group(1).strip()

    body = (
        _join(parser.id_bufs.get("js_content"))
        or _join(parser.class_bufs.get("rich_media_content"))
        or ""
    )
    description = (meta.get("og:description") or meta.get("description") or "").strip()
    note = _initial_state_note(parser.scripts) if source_kind == "xiaohongshu" or parser.scripts else {}
    if note:
        title = note.get("title") or title
        body = " ".join(part for part in (note.get("desc"), body) if part).strip() or body
    if not body:
        body = " ".join(parser.body_parts)
    text_parts = [title, description, body]
    text = "\n".join(part for part in text_parts if part).strip()
    links = unique(list(parser.links) + URL_RE.findall(html or "") + URL_RE.findall(text))
    return {
        "title": " ".join(title.split()),
        "description": description,
        "text": text,
        "links": links,
        "source_url": source_url,
        "source_kind": source_kind,
        "html": html or "",
    }


def extract_identifiers(*texts: str) -> Dict[str, Any]:
    blob = "\n".join(str(item or "") for item in texts)
    doi = ""
    doi_match = DOI_RE.search(blob)
    if doi_match:
        doi = _clean_doi(doi_match.group(0))
    arxiv_id = ""
    label = ARXIV_LABEL_RE.search(blob)
    if label:
        arxiv_id = _arxiv_id(label.group(1) or label.group(2) or "")
    elif ARXIV_BARE_RE.search(blob):
        arxiv_id = _arxiv_id(ARXIV_BARE_RE.search(blob).group(1))  # type: ignore[union-attr]
    paper_urls = [item for item in URL_RE.findall(blob) if is_academic_url(item)]
    return {
        "doi": doi,
        "arxiv_id": arxiv_id,
        "paper_urls": unique(paper_urls),
    }


def _academic_url_from_ids(identifiers: Dict[str, Any]) -> str:
    paper_urls = identifiers.get("paper_urls") or []
    if paper_urls:
        return str(paper_urls[0])
    doi = str(identifiers.get("doi") or "").strip()
    if doi:
        return doi if doi.startswith("http") else f"https://doi.org/{doi}"
    arxiv_id = _arxiv_id(str(identifiers.get("arxiv_id") or ""))
    if arxiv_id:
        return arxiv_id if arxiv_id.startswith("http") else f"https://arxiv.org/abs/{arxiv_id}"
    return ""


def fetch_article(url: str) -> Dict[str, Any]:
    """Fetch and parse a social article, with a readable-text fallback."""
    html = ""
    final_url = url
    try:
        html, final_url = fetch_html(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
        LOGGER.warning("article fetch failed %s: %s", url, error)
        html = ""
    article = parse_article_html(url, html, final_url=final_url) if html else {
        "title": "",
        "description": "",
        "text": "",
        "links": [],
        "source_url": url,
        "source_kind": (
            "wechat" if is_wechat_url(url) else "xiaohongshu" if is_xiaohongshu_url(url) else "web"
        ),
        "html": "",
    }
    thin = len(article.get("text") or "") < 40 and not extract_identifiers(
        article.get("text") or "", " ".join(article.get("links") or [])
    ).get("arxiv_id") and not extract_identifiers(
        article.get("text") or "", " ".join(article.get("links") or [])
    ).get("doi")
    if _looks_blocked(html, url) or thin:
        try:
            raw = fetch_via_jina(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
            LOGGER.warning("jina fallback failed %s: %s", url, error)
            raw = ""
        if raw.strip():
            parsed = _parse_jina_markdown(raw)
            if parsed.get("text") or parsed.get("title"):
                kind = article.get("source_kind") or "web"
                title = parsed.get("title") or article.get("title") or ""
                text = parsed.get("text") or ""
                links = unique(list(article.get("links") or []) + URL_RE.findall(text))
                article = {
                    "title": title,
                    "description": "",
                    "text": "\n".join(part for part in (title, text) if part),
                    "links": links,
                    "source_url": parsed.get("url") or final_url or url,
                    "source_kind": kind,
                    "html": raw,
                }
    return article


def extract_paper_from_article(
    url: str,
    *,
    html: Optional[str] = None,
    llm: Optional[Callable[[str, str], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Identify the main paper discussed in a WeChat / Xiaohongshu article."""
    if html is not None:
        article = parse_article_html(url, html, final_url=url)
    else:
        article = fetch_article(url)
    blob = "\n".join(
        part
        for part in (
            article.get("title"),
            article.get("description"),
            article.get("text"),
            " ".join(article.get("links") or []),
        )
        if part
    )
    identifiers = extract_identifiers(blob)
    academic_url = _academic_url_from_ids(identifiers)
    paper: Dict[str, Any] = {
        "title": str(article.get("title") or "").strip(),
        "url": academic_url,
        "doi": identifiers.get("doi") or "",
        "arxiv_id": identifiers.get("arxiv_id") or "",
        "authors": "",
        "year": "",
        "venue": "",
        "text": str(article.get("text") or "").strip(),
        "source_url": article.get("source_url") or url,
        "source_kind": article.get("source_kind") or "",
        "paper_found": bool(academic_url),
        "article_title": str(article.get("title") or "").strip(),
    }
    if academic_url or not llm:
        return paper
    snippet = (paper.get("text") or "")[:8000]
    if len(snippet) < 20:
        return paper
    try:
        extracted = llm(snippet, paper.get("article_title") or "")
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("article LLM extract failed: %s", error)
        extracted = None
    if not isinstance(extracted, dict):
        return paper
    title = str(extracted.get("title") or "").strip()
    extracted_url = str(extracted.get("url") or "").strip()
    if extracted_url and is_social_article_url(extracted_url):
        extracted_url = ""
    llm_ids = extract_identifiers(
        title,
        extracted_url,
        str(extracted.get("doi") or ""),
        str(extracted.get("arxiv_id") or ""),
        str(extracted.get("text") or ""),
    )
    if title:
        paper["title"] = title
        paper["paper_found"] = True
    for key in ("authors", "year", "venue"):
        value = str(extracted.get(key) or "").strip()
        if value:
            paper[key] = value
    if llm_ids.get("doi"):
        paper["doi"] = llm_ids["doi"]
    if llm_ids.get("arxiv_id"):
        paper["arxiv_id"] = llm_ids["arxiv_id"]
    paper["url"] = _academic_url_from_ids({**identifiers, **llm_ids, "paper_urls": unique([extracted_url] + (llm_ids.get("paper_urls") or []) + (identifiers.get("paper_urls") or []))}) or extracted_url or paper.get("url") or ""
    extra = str(extracted.get("text") or "").strip()
    if extra and extra not in (paper.get("text") or ""):
        paper["text"] = (extra + "\n" + (paper.get("text") or "")).strip()
    if paper.get("url") or paper.get("doi") or paper.get("arxiv_id") or title:
        paper["paper_found"] = True
    return paper
