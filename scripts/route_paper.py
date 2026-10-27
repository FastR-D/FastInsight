#!/usr/bin/env python3
"""Classify a paper and select FastResearch owners.

Feishu card rendering is kept for compatibility but is not part of the
delivery path. Owners are valid by ``name`` even without a Feishu open_id.

Inputs:
  --paper   flat paper JSON or verify_paper.py output containing ``best``
  --roster  colleagues.yaml (or JSON) with directions and optional default_owner
  --trends  optional analyze_trends.py output

Matching is case-insensitive. ASCII keywords use token boundaries so short
terms such as ``RL`` do not match inside ``URL``; Chinese and mixed-language
keywords use phrase matching. Every direction with at least one matching
keyword is selected, and its owners are deduplicated across directions. The
highest-scoring direction remains the primary classification for compatibility.
When no keyword matches, an LLM performs conservative semantic routing from
the roster's research directions; low-confidence decisions use default_owner.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from text_clip import ABSTRACT_STORE_LIMIT, clip_at_sentence
from fastinsight.config import load_env

_ASCII_KEYWORD = re.compile(r"^[a-z0-9_]+(?:[ \t]+[a-z0-9_]+)*$")
DEFAULT_SEMANTIC_THRESHOLD = 0.75


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_as_text(item) for item in value if item is not None)
    return str(value)


def _normalize_text(value):
    return re.sub(r"\s+", " ", _as_text(value)).strip().casefold()


def _is_placeholder(value):
    normalized = _as_text(value).strip().casefold()
    return (
        not normalized
        or normalized.startswith(("ou_xxxx", "<", "{{", "your_", "replace_"))
        or "xxxxxxxx" in normalized
    )


def load_roster(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        import yaml

        return yaml.safe_load(text)
    except ImportError:
        from mini_yaml import load

        return load(text)


def normalize_paper(paper):
    if isinstance(paper, dict) and isinstance(paper.get("best"), dict):
        return paper["best"]
    return paper


def _keyword_matches(keyword, text):
    keyword = _normalize_text(keyword)
    if not keyword:
        return False
    if _ASCII_KEYWORD.fullmatch(keyword):
        escaped = re.escape(keyword).replace(r"\ ", r"\s+")
        pattern = rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    return keyword in text


def score_direction(direction, text):
    hits = []
    seen = set()
    for raw_keyword in direction.get("keywords", []) or []:
        keyword = _as_text(raw_keyword).strip()
        normalized = keyword.casefold()
        if not keyword or normalized in seen:
            continue
        seen.add(normalized)
        if _keyword_matches(keyword, text):
            hits.append(keyword)
    return len(hits), hits


def _priority(direction):
    value = direction.get("priority", 0)
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _owner_list(value):
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    return []


def _collect_valid_owners(value):
    valid = []
    invalid = []
    seen_owner_keys = set()
    for owner in _owner_list(value):
        if not isinstance(owner, dict):
            invalid.append(_as_text(owner) or "(invalid owner)")
            continue
        name = _as_text(owner.get("name")).strip()
        open_id = _as_text(owner.get("feishu_open_id")).strip()
        if not name:
            invalid.append(open_id or "(unnamed owner)")
            continue
        has_open_id = bool(open_id and not _is_placeholder(open_id))
        owner_key = ("open_id", open_id) if has_open_id else ("name", name.casefold())
        if owner_key in seen_owner_keys:
            continue
        seen_owner_keys.add(owner_key)
        owner_out = {"name": name}
        if has_open_id:
            owner_out["feishu_open_id"] = open_id
        receiver_ref = _as_text(owner.get("receiver_ref")).strip()
        if receiver_ref and not _is_placeholder(receiver_ref):
            owner_out["receiver_ref"] = receiver_ref
        valid.append(owner_out)
    return valid, invalid


def _replace_placeholders(value, replacements):
    if isinstance(value, dict):
        return {
            key: _replace_placeholders(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, str):
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, replacement)
    return value


def _clip_abstract(value):
    """Keep the official abstract intact; clip only at a sentence boundary."""
    text = clip_at_sentence(value, ABSTRACT_STORE_LIMIT)
    return text or "暂无摘要"


def _semantic_threshold():
    raw = os.environ.get("FASTINSIGHT_SEMANTIC_ROUTING_THRESHOLD", "").strip()
    try:
        return min(1.0, max(0.0, float(raw))) if raw else DEFAULT_SEMANTIC_THRESHOLD
    except ValueError:
        return DEFAULT_SEMANTIC_THRESHOLD


def _semantic_profiles(roster):
    profiles = {}
    for direction in roster.get("directions", []) or []:
        if not isinstance(direction, dict):
            continue
        direction_name = _as_text(direction.get("name")).strip()
        keywords = [
            _as_text(keyword).strip()
            for keyword in direction.get("keywords", []) or []
            if _as_text(keyword).strip()
        ]
        for owner in _owner_list(direction.get("owners")):
            if not isinstance(owner, dict):
                continue
            name = _as_text(owner.get("name")).strip()
            if not name:
                continue
            key = name.casefold()
            profile = profiles.setdefault(
                key,
                {"name": name, "directions": [], "keywords": [], "owner": owner},
            )
            if direction_name and direction_name not in profile["directions"]:
                profile["directions"].append(direction_name)
            for keyword in keywords:
                if keyword not in profile["keywords"]:
                    profile["keywords"].append(keyword)
    return list(profiles.values())


def _parse_json_object(content):
    match = re.search(r"\{[\s\S]*\}", _as_text(content))
    if not match:
        raise RuntimeError("LLM did not return a JSON object")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise RuntimeError("LLM returned invalid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError("LLM JSON must be an object")
    return data


def _semantic_route_paper(roster, paper):
    load_env()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    profiles = _semantic_profiles(roster)
    if not profiles:
        raise RuntimeError("roster has no semantic owner profiles")
    model = os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4o-mini"
    base = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    prompt_profiles = [
        {
            "name": profile["name"],
            "directions": profile["directions"],
            "keywords": profile["keywords"],
        }
        for profile in profiles
    ]
    paper_context = {
        "title": _as_text(paper.get("title")),
        "abstract": _as_text(paper.get("abstract") or paper.get("summary"))[:6000],
        "venue": _as_text(paper.get("venue")),
        "year": _as_text(paper.get("year")),
    }
    prompt = (
        "你是论文分发助手。关键词匹配未命中，请仅根据候选人的研究方向判断论文应分发给谁。"
        "可以选择一人或多人；只有在论文与研究方向明确相关时才选择。"
        "如果方向宽泛、证据不足、论文跨领域难以判断，selected 必须为空。"
        "confidence 是整次判断的置信度，范围 0 到 1；宁可保守，也不要为了分发而强行匹配。"
        "只返回 JSON：{\"selected\":[{\"name\":\"负责人姓名\",\"directions\":[\"命中的研究方向\"],"
        "\"reason\":\"简短理由\"}],\"confidence\":0.0,\"reason\":\"整体判断\"}。"
        f"\n候选人研究方向：{json.dumps(prompt_profiles, ensure_ascii=False)}"
        f"\n论文信息：{json.dumps(paper_context, ensure_ascii=False)}"
    )
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"semantic routing LLM HTTP {error.code}: {detail}") from error
    choices = body.get("choices") or []
    content = ((choices[0] or {}).get("message") or {}).get("content") if choices else ""
    return _parse_json_object(content)


def _normalize_semantic_result(result, roster, threshold):
    normalized = {
        "attempted": True,
        "confidence": 0.0,
        "threshold": threshold,
        "selected": [],
        "reason": "",
        "error": "",
    }
    if not isinstance(result, dict):
        normalized["error"] = "semantic routing returned no object"
        return normalized
    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    normalized["confidence"] = confidence
    normalized["reason"] = _as_text(result.get("reason")).strip()
    normalized["error"] = _as_text(result.get("error")).strip()
    profiles = {
        profile["name"].casefold(): profile for profile in _semantic_profiles(roster)
    }
    raw_selected = result.get("selected") or result.get("owners") or []
    if isinstance(raw_selected, dict):
        raw_selected = [raw_selected]
    if not isinstance(raw_selected, list):
        normalized["error"] = "semantic routing selected must be a list"
        return normalized
    seen = set()
    for item in raw_selected:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _as_text(item.get("name")).strip()
        profile = profiles.get(name.casefold())
        if not name or not profile or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        requested = item.get("directions", item.get("direction", []))
        if isinstance(requested, str):
            requested = [requested]
        requested = [
            _as_text(direction).strip()
            for direction in requested or []
            if _as_text(direction).strip()
        ] if isinstance(requested, list) else []
        directions = [
            direction
            for direction in requested
            if direction in profile["directions"]
        ] or list(profile["directions"])
        normalized["selected"].append(
            {
                "name": profile["name"],
                "directions": directions,
                "reason": _as_text(item.get("reason")).strip(),
            }
        )
    return normalized


def build_route_decision(roster, text, paper, semantic_result=None):
    matched_directions = []
    for index, direction in enumerate(roster.get("directions", []) or []):
        if not isinstance(direction, dict):
            continue
        score, hits = score_direction(direction, text)
        if score <= 0:
            continue
        matched_directions.append(
            {
                "name": _as_text(direction.get("name")).strip(),
                "score": score,
                "hits": hits,
                "priority": _priority(direction),
                "index": index,
                "owners": direction.get("owners"),
                "source": "keyword",
            }
        )

    selected_owners = []
    invalid_owners = []
    fallback = False
    fallback_reason = ""
    routing_mode = "keyword"
    semantic = {
        "attempted": False,
        "confidence": 0.0,
        "threshold": _semantic_threshold(),
        "selected": [],
        "reason": "",
        "error": "",
    }

    if matched_directions:
        matched_owners = []
        for matched in matched_directions:
            matched_owners.extend(_owner_list(matched.get("owners")))
        selected_owners, invalid_owners = _collect_valid_owners(matched_owners)
        if not selected_owners:
            fallback = True
            fallback_reason = "matched_direction_has_no_valid_owner"
    else:
        threshold = _semantic_threshold()
        routing_mode = "semantic"
        fallback = True
        fallback_reason = "no_keyword_match"
        if semantic_result is None:
            try:
                semantic_result = _semantic_route_paper(roster, paper)
            except Exception as error:  # noqa: BLE001 - default safely without LLM
                semantic_result = {"error": str(error)}
        semantic = _normalize_semantic_result(semantic_result, roster, threshold)
        if semantic["selected"] and semantic["confidence"] >= threshold:
            direction_lookup = {
                _as_text(direction.get("name")).strip(): direction
                for direction in roster.get("directions", []) or []
                if isinstance(direction, dict)
            }
            semantic_directions = []
            owner_values = []
            for selected in semantic["selected"]:
                for direction_name in selected["directions"]:
                    if direction_name not in semantic_directions:
                        semantic_directions.append(direction_name)
                profile = next(
                    (
                        profile
                        for profile in _semantic_profiles(roster)
                        if profile["name"] == selected["name"]
                    ),
                    None,
                )
                if profile:
                    owner_values.append(profile["owner"])
            matched_directions = [
                {
                    "name": direction_name,
                    "score": 0,
                    "hits": [],
                    "priority": _priority(direction_lookup.get(direction_name, {})),
                    "index": index,
                    "owners": (direction_lookup.get(direction_name) or {}).get("owners"),
                    "source": "semantic",
                }
                for index, direction_name in enumerate(semantic_directions)
            ]
            selected_owners, semantic_invalid = _collect_valid_owners(owner_values)
            invalid_owners.extend(semantic_invalid)
            fallback_reason = "no_keyword_match_semantic_route"
        else:
            if not semantic["selected"]:
                fallback_reason = (
                    "semantic_route_unavailable"
                    if semantic.get("error")
                    else "semantic_route_no_confident_owner"
                )
            else:
                fallback_reason = "semantic_confidence_below_threshold"

    if not selected_owners:
        routing_mode = "default"
        default_owners, invalid_default = _collect_valid_owners(
            roster.get("default_owner")
        )
        selected_owners = default_owners
        invalid_owners.extend(invalid_default)
        if not fallback_reason:
            fallback_reason = "no_direction_match"
        fallback = True

    primary_direction = max(
        matched_directions,
        key=lambda item: (item["score"], item["priority"], -item["index"]),
        default=None,
    )
    direction_name = primary_direction["name"] if primary_direction else None
    return {
        "matched_direction": direction_name,
        "matched_directions": [
            {
                "name": matched["name"],
                "score": matched["score"],
                "hits": matched["hits"],
                "source": matched["source"],
            }
            for matched in matched_directions
        ],
        "score": primary_direction["score"] if primary_direction else 0,
        "hits": primary_direction["hits"] if primary_direction else [],
        "fallback": fallback,
        "fallback_reason": fallback_reason,
        "routing_mode": routing_mode,
        "semantic_route": semantic,
        "invalid_owners": invalid_owners,
        "owners": selected_owners,
        "routable": bool(selected_owners),
    }


def fill_template(template, paper, direction_name, trends_text="", url_text=""):
    title = _as_text(paper.get("title")).strip() or "(未知标题)"
    authors = _as_text(paper.get("authors")).strip()
    year_venue = " / ".join(
        item
        for item in (
            _as_text(paper.get("year")).strip(),
            _as_text(paper.get("venue")).strip(),
        )
        if item
    ) or "—"
    abstract = _clip_abstract(paper.get("abstract") or paper.get("summary") or "")
    url = _as_text(paper.get("url")).strip()
    if not url and paper.get("doi"):
        url = f"https://doi.org/{_as_text(paper['doi']).strip()}"
    if not url and paper.get("arxiv_id"):
        arxiv_id = re.sub(r"^arXiv:", "", _as_text(paper["arxiv_id"]).strip(), flags=re.IGNORECASE)
        if arxiv_id:
            url = (
                arxiv_id
                if arxiv_id.startswith(("http://", "https://"))
                else f"https://arxiv.org/abs/{arxiv_id}"
            )
    url_text = _as_text(url_text).strip() or url or "暂无链接"
    trends_text = _as_text(trends_text).strip() or "暂无趋势数据"

    replacements = {
        "{{TITLE}}": title,
        "{{AUTHORS}}": authors or "暂无作者",
        "{{YEAR_VENUE}}": year_venue,
        "{{ABSTRACT}}": abstract,
        "{{URL}}": url or "#",
        "{{URL_TEXT}}": url_text,
        "{{TRENDS}}": clip_at_sentence(trends_text, ABSTRACT_STORE_LIMIT) or "暂无趋势数据",
        "{{DIRECTION}}": _as_text(direction_name).strip() or "未分类",
    }
    return _replace_placeholders(template, replacements)


def _write_blocked_card(card_out):
    """Overwrite the output card with a sentinel so stale payloads cannot be sent."""
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "卡片未生成 · 缺少 URL 或 LLM 趋势"},
                "template": "grey",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "FASTINSIGHT_CARD_BLOCKED_MISSING_URL_OR_TREND"
                    }
                }
            ],
        },
    }
    with open(card_out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(
        description="Classify a paper and select FastResearch owners."
    )
    ap.add_argument("--paper", required=True)
    ap.add_argument("--roster", required=True)
    ap.add_argument("--template", default=None)
    ap.add_argument(
        "--trends",
        default=None,
        help="trends JSON file from analyze_trends.py",
    )
    ap.add_argument(
        "--trend-text",
        default=None,
        help="LLM-generated trend summary used instead of a trends JSON file",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the decision and card without writing a card file",
    )
    ap.add_argument("--card-out", default="feishu_card_payload.json")
    ap.add_argument(
        "--allow-missing-url",
        action="store_true",
        help="permit a card without a usable URL (not recommended)",
    )
    ap.add_argument(
        "--allow-no-trend",
        action="store_true",
        help="permit a card without an LLM trend summary (not recommended)",
    )
    args = ap.parse_args()

    with open(args.paper, "r", encoding="utf-8") as f:
        paper = normalize_paper(json.load(f))
    if not isinstance(paper, dict):
        ap.error("--paper must contain a JSON object or a `best` object")

    roster = load_roster(args.roster)
    if not isinstance(roster, dict):
        ap.error("--roster must contain a mapping")

    text = _normalize_text(
        " ".join(
            [
                _as_text(paper.get("title")),
                _as_text(paper.get("abstract")),
                _as_text(paper.get("venue")),
            ]
        )
    )

    decision = build_route_decision(roster, text, paper)
    direction_name = decision["matched_direction"]
    selected_owners = decision["owners"]
    invalid_owners = decision["invalid_owners"]
    fallback = decision["fallback"]
    fallback_reason = decision["fallback_reason"]
    routable = decision["routable"]

    effective_url = _as_text(paper.get("url")).strip()
    if not effective_url and paper.get("doi"):
        effective_url = f"https://doi.org/{_as_text(paper['doi']).strip()}"
    if not effective_url and paper.get("arxiv_id"):
        arxiv_id = re.sub(r"^arXiv:", "", _as_text(paper["arxiv_id"]).strip(), flags=re.IGNORECASE)
        if arxiv_id:
            effective_url = (
                arxiv_id
                if arxiv_id.startswith(("http://", "https://"))
                else f"https://arxiv.org/abs/{arxiv_id}"
            )

    trends_text = ""
    url_text = effective_url
    if args.trend_text:
        trends_text = _as_text(args.trend_text).strip()
    elif args.trends and os.path.exists(args.trends):
        with open(args.trends, "r", encoding="utf-8") as f:
            trends_data = json.load(f)
        if isinstance(trends_data, dict):
            trends_text = _as_text(trends_data.get("trend_summary")).strip()
            url_text = (
                _as_text(trends_data.get("paper_url")).strip() or effective_url
            )

    if not effective_url and not args.allow_missing_url:
        if not args.dry_run:
            _write_blocked_card(args.card_out)
        ap.error(
            "paper has no usable URL; run verify_paper.py first (web/page "
            "fallback is mandatory) or ask the sender for a link"
        )
    if not trends_text and not args.allow_no_trend:
        if not args.dry_run:
            _write_blocked_card(args.card_out)
        ap.error(
            "missing LLM trend summary; pass --trend-text or a --trends JSON "
            "with trend_summary before generating a card"
        )

    card = None
    if routable or args.dry_run:
        template_path = args.template or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "assets",
            "feishu_card.json",
        )
        if os.path.exists(template_path):
            with open(template_path, "r", encoding="utf-8") as f:
                template = json.load(f)
            card = fill_template(
                template, paper, direction_name, trends_text, url_text
            )
        if not args.dry_run and card is not None and routable:
            with open(args.card_out, "w", encoding="utf-8") as f:
                json.dump(card, f, ensure_ascii=False, indent=2)

    out = {
        "matched_direction": direction_name,
        "matched_directions": decision["matched_directions"],
        "score": decision["score"],
        "hits": decision["hits"],
        "fallback": fallback,
        "fallback_reason": fallback_reason,
        "routing_mode": decision["routing_mode"],
        "semantic_route": decision["semantic_route"],
        "invalid_owners": invalid_owners,
        "owners": selected_owners,
        "routable": routable,
        "paper_url": url_text or effective_url,
        "trend_summary": trends_text,
        "card": card if args.dry_run else (args.card_out if card else None),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if args.dry_run or routable else 2)


if __name__ == "__main__":
    main()
