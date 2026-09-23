#!/usr/bin/env python3
"""Classify a paper, select valid owners, and render a Feishu card.

Inputs:
  --paper   flat paper JSON or verify_paper.py output containing ``best``
  --roster  colleagues.yaml (or JSON) with directions and optional default_owner
  --trends  optional analyze_trends.py output

Matching is case-insensitive. ASCII keywords use token boundaries so short
terms such as ``RL`` do not match inside ``URL``; Chinese and mixed-language
keywords use phrase matching. The highest score wins, followed by optional
direction priority, then roster order.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_ASCII_KEYWORD = re.compile(r"^[a-z0-9_]+(?:[ \t]+[a-z0-9_]+)*$")


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
    seen_ids = set()
    for owner in _owner_list(value):
        if not isinstance(owner, dict):
            invalid.append(_as_text(owner) or "(invalid owner)")
            continue
        name = _as_text(owner.get("name")).strip()
        open_id = _as_text(owner.get("feishu_open_id")).strip()
        if not name or _is_placeholder(open_id):
            invalid.append(name or open_id or "(unnamed owner)")
            continue
        if open_id in seen_ids:
            continue
        seen_ids.add(open_id)
        entry = {"name": name, "feishu_open_id": open_id}
        receiver_ref = _as_text(owner.get("receiver_ref")).strip()
        if receiver_ref and not _is_placeholder(receiver_ref):
            entry["receiver_ref"] = receiver_ref
        valid.append(entry)
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
    abstract = _as_text(paper.get("abstract")).strip()[:400] or "暂无摘要"
    url = _as_text(paper.get("url")).strip()
    if not url and paper.get("doi"):
        url = f"https://doi.org/{_as_text(paper['doi']).strip()}"
    url_text = _as_text(url_text).strip() or url or "暂无链接"
    trends_text = _as_text(trends_text).strip() or "暂无趋势数据"

    replacements = {
        "{{TITLE}}": title,
        "{{AUTHORS}}": authors or "暂无作者",
        "{{YEAR_VENUE}}": year_venue,
        "{{ABSTRACT}}": abstract,
        "{{URL}}": url or "#",
        "{{URL_TEXT}}": url_text,
        "{{TRENDS}}": trends_text[:600],
        "{{DIRECTION}}": _as_text(direction_name).strip() or "未分类",
    }
    return _replace_placeholders(template, replacements)


def main():
    ap = argparse.ArgumentParser(
        description="Classify a paper and render a Feishu forwarding card."
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
        "--dry-run",
        action="store_true",
        help="print the decision and card without writing a card file",
    )
    ap.add_argument("--card-out", default="feishu_card_payload.json")
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

    best_dir = None
    best_score = 0
    best_hits = []
    best_key = None
    for index, direction in enumerate(roster.get("directions", []) or []):
        if not isinstance(direction, dict):
            continue
        score, hits = score_direction(direction, text)
        candidate_key = (score, _priority(direction), -index)
        if best_key is None or candidate_key > best_key:
            best_key = candidate_key
            best_score, best_dir, best_hits = score, direction, hits

    direction_name = (
        _as_text(best_dir.get("name")).strip()
        if best_dir and best_score > 0
        else None
    )
    selected_owners = []
    invalid_owners = []
    fallback = False
    fallback_reason = ""

    if best_dir and best_score > 0:
        selected_owners, invalid_owners = _collect_valid_owners(
            best_dir.get("owners")
        )
        if not selected_owners:
            fallback = True
            fallback_reason = "matched_direction_has_no_valid_owner"
    else:
        fallback = True
        fallback_reason = "no_direction_match"

    if not selected_owners:
        default_owners, invalid_default = _collect_valid_owners(
            roster.get("default_owner")
        )
        selected_owners = default_owners
        invalid_owners.extend(invalid_default)

    routable = bool(selected_owners)

    effective_url = _as_text(paper.get("url")).strip()
    if not effective_url and paper.get("doi"):
        effective_url = f"https://doi.org/{_as_text(paper['doi']).strip()}"

    trends_text = ""
    url_text = effective_url
    if args.trends and os.path.exists(args.trends):
        with open(args.trends, "r", encoding="utf-8") as f:
            trends_data = json.load(f)
        if isinstance(trends_data, dict):
            trends_text = _as_text(trends_data.get("trend_summary")).strip()
            url_text = (
                _as_text(trends_data.get("paper_url")).strip() or effective_url
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
        "score": best_score,
        "hits": best_hits,
        "fallback": fallback,
        "fallback_reason": fallback_reason,
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
