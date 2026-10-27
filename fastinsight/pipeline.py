"""Run the FastInsight paper pipeline and optional LLM helpers.

Paper verification / routing / FastResearch publish stay in scripts/, copied
from the OpenClaw FastInsight skill so behavior matches.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .article import clean_share_text, extract_paper_from_article
from .config import ROOT, env
from .parse import URL_RE, is_academic_url, is_social_article_url, preferred_url, unique

LOGGER = logging.getLogger("fastinsight.pipeline")

SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from auto_pipeline import enrich_paper, process as run_auto_pipeline  # noqa: E402


def _looks_like_user_title(text: str) -> bool:
    value = str(text or "").strip()
    if len(value) < 8 or value.startswith("http"):
        return False
    if is_social_article_url(value):
        return False
    if "小红书" in value and "笔记" in value:
        return False
    return True


def _llm_extract_paper(text: str, article_title: str = "") -> Optional[Dict[str, Any]]:
    if not env("OPENAI_API_KEY"):
        return None
    model = env("OPENAI_MODEL") or "gpt-4o-mini"
    prompt = (
        "这是一篇微信公众号、小红书文章或截图 OCR 正文。请识别文中介绍的主要学术论文（只选一篇最主要的）。"
        "只返回 JSON：{\"title\":\"论文原文标题\",\"url\":\"论文官方链接\",\"doi\":\"\","
        "\"arxiv_id\":\"\",\"search_query\":\"用于查找该论文的简短检索词\",\"authors\":\"\",\"year\":\"\",\"venue\":\"\","
        "\"text\":\"文中与该论文相关的关键信息\"}。"
        "没有的字段留空。url 必须是论文/预印本/DOI 链接，不要填公众号或小红书链接。"
        "如果是论文解读、新闻或海报，请根据内容推断论文正式英文标题；"
        "即使无法完全确定，也要给出尽可能接近正式英文标题的 search_query，保留系统名、研究任务和研究对象，"
        "不要只写 firmware、fuzzing 这类宽泛词。若内容不是在介绍某篇论文，返回 {\"title\":\"\"}。"
        f"\n文章标题：{article_title}\n正文：{text[:8000]}"
    )
    result = _chat_json([{"role": "user", "content": prompt}], model=model, timeout=60)
    content = result["content"]
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _merge_article_paper(paper: Dict[str, Any], extracted: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(paper)
    source_url = str(merged.get("source_url") or merged.get("url") or "").strip()
    merged["source_url"] = source_url
    if extracted.get("source_kind"):
        merged["source_kind"] = extracted.get("source_kind")
    for key in ("doi", "arxiv_id", "search_query", "authors", "year", "venue"):
        value = str(extracted.get(key) or "").strip()
        if value:
            merged[key] = value
    academic_url = str(extracted.get("url") or "").strip()
    if academic_url and not is_academic_url(academic_url):
        academic_url = ""
    if not academic_url and merged.get("doi"):
        doi = str(merged["doi"])
        academic_url = doi if doi.startswith("http") else f"https://doi.org/{doi}"
    if not academic_url and merged.get("arxiv_id"):
        arxiv_id = re.sub(r"^arXiv:", "", str(merged["arxiv_id"]), flags=re.IGNORECASE).strip()
        academic_url = (
            arxiv_id
            if arxiv_id.startswith("http")
            else f"https://arxiv.org/abs/{arxiv_id}"
        )
    if academic_url:
        merged["url"] = academic_url
    extracted_title = str(extracted.get("title") or "").strip()
    current_title = str(merged.get("title") or "").strip()
    if extracted_title:
        if extracted.get("paper_found") or not _looks_like_user_title(current_title):
            merged["title"] = extracted_title
    article_text = str(extracted.get("text") or "").strip()
    if article_text:
        merged["text"] = article_text[:8000]
        if not str(merged.get("abstract") or "").strip():
            merged["abstract"] = article_text[:1500]
            merged["abstract_source"] = "search"
    return merged


def paper_from_url(url: str, text: str = "") -> Dict[str, Any]:
    leftover = clean_share_text(URL_RE.sub("", text or ""))
    title = leftover.splitlines()[0][:300] if leftover else ""
    paper = {
        "title": title or url,
        "url": url,
        "text": text or url,
        "source_url": url,
    }
    if not is_social_article_url(url):
        return paper
    try:
        extracted = extract_paper_from_article(url, llm=_llm_extract_paper)
    except Exception as error:  # noqa: BLE001 - keep original URL metadata
        LOGGER.warning("social article extract failed for %s: %s", url, error)
        return paper
    if not extracted:
        return paper
    return _merge_article_paper(paper, extracted)


def paper_from_ocr(texts: List[str], extra_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    blob = "\n".join(texts)
    urls = unique(list(extra_urls or []) + URL_RE.findall(blob))
    chosen = preferred_url(urls) or (urls[0] if urls else "")
    title = ""
    for line in blob.splitlines():
        cleaned = URL_RE.sub("", line).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if len(cleaned) >= 8:
            title = cleaned[:300]
            break
    paper = {
        "title": title or chosen,
        "url": chosen,
        "text": blob,
        "urls": urls,
    }
    if len(blob.strip()) >= 20:
        try:
            extracted = _llm_extract_paper(blob)
        except Exception as error:  # noqa: BLE001 - keep OCR metadata as fallback
            LOGGER.warning("image content extract failed: %s", error)
            extracted = None
        if isinstance(extracted, dict):
            extracted.setdefault(
                "paper_found",
                bool(extracted.get("title") or extracted.get("url")),
            )
            paper = _merge_article_paper(paper, extracted)
    return paper


def _chat_json(messages: List[Dict[str, Any]], *, model: str, timeout: int = 60) -> Dict[str, Any]:
    api_key = env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    base = env("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"LLM HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:400]}"
        ) from error
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("LLM returned no choices")
    content = ((choices[0] or {}).get("message") or {}).get("content") or ""
    return {"content": str(content).strip(), "raw": body}


def vision_paper(image_path: str) -> Optional[Dict[str, Any]]:
    if not env("OPENAI_API_KEY"):
        return None
    model = env("VISION_MODEL") or env("OPENAI_MODEL") or "gpt-4o-mini"
    import base64

    raw = Path(image_path).read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    suffix = Path(image_path).suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")
    prompt = (
        "这是一张论文/学术海报/科研资讯截图。请提取论文标题、作者、年份、DOI/arXiv/URL，"
        "如果是论文解读、新闻或海报，请推断其介绍的论文正式英文标题；"
        "在无法完全确定时生成尽可能接近正式英文标题的检索词。"
        "只返回 JSON：{\"title\":\"\",\"url\":\"\",\"doi\":\"\",\"arxiv_id\":\"\","
        "\"search_query\":\"\",\"text\":\"截图可见的关键文字\"}。"
        "没有的字段留空。url 必须是可访问的 http(s) 链接；search_query 应保留系统名、研究任务和研究对象，"
        "不要只写 firmware、fuzzing 这类宽泛词。"
    )
    result = _chat_json(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            }
        ],
        model=model,
        timeout=90,
    )
    content = result["content"]
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    url = str(data.get("url") or "").strip()
    doi = str(data.get("doi") or "").strip()
    arxiv_id = str(data.get("arxiv_id") or "").strip()
    if not url and doi:
        url = doi if doi.startswith("http") else f"https://doi.org/{doi}"
    if not url and arxiv_id:
        arxiv_id = re.sub(r"^arXiv:", "", arxiv_id, flags=re.IGNORECASE).strip()
        url = arxiv_id if arxiv_id.startswith("http") else f"https://arxiv.org/abs/{arxiv_id}"
    search_query = str(data.get("search_query") or "").strip()
    text = str(data.get("text") or "").strip()
    if not title and not url and not search_query and len(text) < 20:
        return None
    data["title"] = title or url
    data["url"] = url
    return data


def generate_trend(paper: Dict[str, Any]) -> str:
    title = str(paper.get("title") or "").strip()
    abstract = str(paper.get("abstract") or paper.get("text") or "").strip()
    venue = str(paper.get("venue") or "").strip()
    year = str(paper.get("year") or "").strip()
    if env("OPENAI_API_KEY"):
        model = env("OPENAI_MODEL") or "gpt-4o-mini"
        prompt = (
            "你是 FastInsight。根据论文元数据写 2–5 句中文趋势分析，说明它在当前研究方向里为什么值得关注。"
            "不要使用“暂无趋势数据”“API 不可用”这类句子，不要编造引用数。"
            f"\n标题：{title}\n年份：{year}\nvenue：{venue}\n摘要：{abstract[:4000]}"
        )
        result = _chat_json(
            [{"role": "user", "content": prompt}],
            model=model,
            timeout=60,
        )
        text = result["content"].strip()
        if text:
            return text
    raise RuntimeError(
        "缺少 LLM 趋势分析：请设置 OPENAI_API_KEY（及可选 OPENAI_BASE_URL/OPENAI_MODEL）"
    )


def process_paper(paper: Dict[str, Any]) -> Dict[str, Any]:
    paper = dict(paper)
    if not str(paper.get("title") or "").strip() and str(paper.get("url") or "").strip():
        paper["title"] = paper["url"]
    # Skill order: identify → verify/enrich → LLM trend → classify → FastResearch.
    paper = enrich_paper(paper)
    trend = generate_trend(paper)
    result = run_auto_pipeline(paper, trend_text=trend, skip_verify=True)
    result["trend_text"] = trend
    return result


def confirmation_text(result: Dict[str, Any]) -> str:
    paper = result.get("paper") if isinstance(result.get("paper"), dict) else {}
    route = result.get("route") if isinstance(result.get("route"), dict) else {}
    title = str(paper.get("title") or "未命名论文").strip()
    direction_names = []
    for matched in route.get("matched_directions") or []:
        name = str(matched.get("name") or "").strip() if isinstance(matched, dict) else str(matched or "").strip()
        if name and name not in direction_names:
            direction_names.append(name)
    direction = " / ".join(direction_names) or str(route.get("matched_direction") or "").strip() or "未分类"
    delivered = result.get("delivered") or []
    names: List[str] = []
    for owner in route.get("owners") or []:
        if isinstance(owner, dict) and owner.get("name"):
            name = str(owner["name"]).strip()
            if name and name not in names:
                names.append(name)
    for item in delivered:
        if not isinstance(item, dict):
            continue
        for name in item.get("deliveredTo") or []:
            text = str(name).strip()
            if text and text not in names:
                names.append(text)
    owner_part = f"（{'、'.join('@' + name for name in names)}）" if names else ""
    fallback = "（未命中关键词，已走 fallback 分拣，可在 FastResearch 中纠正）" if route.get("fallback") else ""
    trend = str(result.get("trend_text") or "").strip()
    errors = result.get("errors") or []

    if delivered and not errors:
        text = f"✅ 已将《{title}》分拣至【{direction}】，并已发布至 FastResearch{owner_part}。"
    elif delivered and errors:
        text = (
            f"⚠️ 《{title}》已部分发布至 FastResearch（{direction}）{owner_part}。"
            f"失败：{'; '.join(str(e) for e in errors)[:400]}"
        )
    elif errors:
        text = f"❌ FastResearch 发布失败：{'; '.join(str(e) for e in errors)[:500]}"
    else:
        text = f"✅ 已识别《{title}》，方向【{direction}】，但没有可投递的负责人。"
    if fallback and delivered:
        text += fallback
    if trend and delivered:
        text += "\n" + trend[:500]
    return text
