#!/usr/bin/env python3
"""Run the FastInsight pipeline and deliver results to FastResearch.

This module is used by the Feishu bridge and can also be called directly from
an automation platform. Network enrichment is best-effort; the original
metadata is still delivered when academic APIs are unavailable.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from text_clip import compose_research_summary, prefer_abstract
from service_auth import service_configuration, service_token

ROSTER_DEFAULT = ROOT.parent / "assets" / "colleagues.yaml"


def _text(value):
    return "" if value is None else str(value).strip()


def _verification_query(paper, title):
    """Combine identifiers with the best available title or content search query."""
    search_query = _text(paper.get("search_query"))
    pieces = [search_query or _text(title)]
    doi = _text(paper.get("doi"))
    arxiv_id = _text(paper.get("arxiv_id"))
    if doi:
        pieces.insert(
            0,
            doi if doi.startswith(("http://", "https://")) else f"https://doi.org/{doi}",
        )
    elif arxiv_id:
        arxiv_id = re.sub(r"^arXiv:", "", arxiv_id, flags=re.IGNORECASE).strip()
        if arxiv_id.startswith(("http://", "https://")):
            pieces.insert(0, arxiv_id)
        elif arxiv_id:
            pieces.insert(0, f"https://arxiv.org/abs/{arxiv_id}")
    return " ".join(piece for piece in pieces if piece)


def _merge_web_fallback(existing, fallback):
    """Keep screenshot/link metadata and enrich gaps from a web-search hit."""
    merged = dict(existing)
    existing_url = _text(merged.get("url"))
    score = fallback.get("match_score")
    try:
        strong_match = float(score or 0) >= 0.6
    except (TypeError, ValueError):
        strong_match = False
    page_fetched = bool(fallback.get("page_fetched"))
    for key in ("url", "url_kind"):
        value = fallback.get(key)
        if value is None or value == "":
            continue
        if not existing_url or strong_match or page_fetched:
            merged[key] = value
    for key in ("provider", "match_score", "web_search_result"):
        value = fallback.get(key)
        if value is not None and value != "":
            merged[key] = value
    if page_fetched:
        # A successfully fetched page is the source of truth for metadata:
        # use the title/authors/venue/year found on the page. Abstracts still
        # prefer an official paper abstract over a search snippet.
        for key in ("title", "authors", "year", "venue", "doi", "arxiv_id"):
            if _text(fallback.get(key)):
                merged[key] = fallback[key]
        prefer_abstract(merged, fallback)
    else:
        for key in ("title", "authors", "year", "venue"):
            if not _text(merged.get(key)) and _text(fallback.get(key)):
                merged[key] = fallback[key]
        prefer_abstract(merged, fallback)
    merged["verified_by_academic_sources"] = False
    merged["verification_note"] = (
        "web page/search fallback; not verified by academic sources"
    )
    return merged


def _run_json(command):
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
    try:
        return json.loads(completed.stdout), completed.returncode
    except json.JSONDecodeError:
        return {}, completed.returncode


def _publish(endpoint, ingest_key, person, paper, trends, direction, *, cas_config=None, receiver_ref=""):
    trend_summary = _text(trends.get("trend_summary"))
    summary = compose_research_summary(paper.get("abstract"), trend_summary)
    url = _text(paper.get("url"))
    if not url and _text(paper.get("doi")):
        url = f"https://doi.org/{_text(paper['doi'])}"
    if not url and _text(paper.get("arxiv_id")):
        arxiv_id = re.sub(
            r"^arXiv:", "", _text(paper["arxiv_id"]), flags=re.IGNORECASE
        )
        if arxiv_id:
            url = (
                arxiv_id
                if arxiv_id.startswith(("http://", "https://"))
                else f"https://arxiv.org/abs/{arxiv_id}"
            )
    item = {
        "id": _text(paper.get("doi")) or url or _text(paper.get("title")),
        "title": _text(paper.get("title")) or "未命名论文",
        "summary": summary,
        "url": url,
        "direction": direction,
        "source": "FastInsight",
    }
    if cas_config:
        if not receiver_ref:
            raise ValueError("FastCAS 服务投递需要名册中的 receiver_ref")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + service_token(cas_config, endpoint),
        }
        payload = {"receiver_ref": receiver_ref, "item": item}
    else:
        headers = {"Content-Type": "application/json", "X-FastInsight-Key": ingest_key}
        payload = {"person": person, "item": item}
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/api/insight/publish",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _normalize_paper(paper):
    if not isinstance(paper, dict):
        raise ValueError("paper must be an object")
    paper = dict(paper.get("best") if isinstance(paper.get("best"), dict) else paper)
    title = _text(paper.get("title"))
    if not title:
        title = _text(paper.get("text"))
        title = re.sub(r"https?://\S+", "", title).strip().splitlines()[0][:300] if title else "未命名论文"
        paper["title"] = title
    return paper


def _apply_verification(paper, verified):
    if not isinstance(verified, dict) or not isinstance(verified.get("best"), dict):
        return paper
    best = verified["best"]
    try:
        score = float(best.get("match_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    academic_confirmed = bool(
        verified.get("verified_by_academic_sources")
        and (best.get("exact") or score >= 0.6)
    )
    if academic_confirmed:
        return best
    return _merge_web_fallback(paper, best)


def enrich_paper(paper):
    """Verify/enrich metadata via academic APIs, with web/page fallback."""
    source_title = _text(paper.get("title"))
    search_query = _text(paper.get("search_query"))
    content_query = _text(paper.get("text"))[:500]
    paper = _normalize_paper(paper)
    query = search_query or source_title or content_query
    if not query:
        return paper
    with tempfile.TemporaryDirectory(prefix="fastinsight-verify-") as temp_dir:
        verified_path = Path(temp_dir) / "verified.json"
        verified, _ = _run_json(
            [
                sys.executable,
                str(ROOT / "verify_paper.py"),
                "--query",
                _verification_query(paper, query),
                "--json-out",
                str(verified_path),
            ]
        )
        if not verified and verified_path.exists():
            verified = json.loads(verified_path.read_text(encoding="utf-8"))
        return _apply_verification(paper, verified)


def process(paper, *, roster_path=None, endpoint=None, ingest_key=None, trend_text=None, skip_verify=False):
    """Verify, enrich, route, and publish one paper. Returns an audit object."""
    paper = _normalize_paper(paper)
    endpoint = endpoint or os.environ.get("FASTRESEARCH_URL", "http://127.0.0.1:8787")
    ingest_key = ingest_key if ingest_key is not None else os.environ.get("FASTINSIGHT_INGEST_KEY", "")
    cas_config = service_configuration(ingest_key=ingest_key)
    roster_path = str(roster_path or os.environ.get("FASTINSIGHT_ROSTER", ROSTER_DEFAULT))
    if not skip_verify:
        paper = enrich_paper(paper)
    with tempfile.TemporaryDirectory(prefix="fastinsight-") as temp_dir:
        temp = Path(temp_dir)
        paper_path = temp / "paper.json"
        paper_path.write_text(json.dumps(paper, ensure_ascii=False), encoding="utf-8")
        trends_path = temp / "trends.json"
        llm_trend = _text(trend_text)
        if llm_trend:
            trends = {
                "source": "llm",
                "trend_summary": llm_trend,
                "paper_url": _text(paper.get("url")),
            }
            trends_path.write_text(
                json.dumps(trends, ensure_ascii=False), encoding="utf-8"
            )
        else:
            trends, _ = _run_json(
                [
                    sys.executable,
                    str(ROOT / "analyze_trends.py"),
                    "--paper",
                    str(paper_path),
                    "--json-out",
                    str(trends_path),
                ]
            )
            if not trends and trends_path.exists():
                trends = json.loads(trends_path.read_text(encoding="utf-8"))
        route, route_code = _run_json([sys.executable, str(ROOT / "route_paper.py"), "--paper", str(paper_path), "--roster", roster_path, "--trends", str(trends_path), "--dry-run"])
    if route_code != 0 or not isinstance(route, dict):
        raise RuntimeError(
            "route_paper refused to render an incomplete card: "
            + json.dumps(route, ensure_ascii=False)[:600]
        )
    direction_names = []
    for matched in route.get("matched_directions", []) or []:
        name = _text(matched.get("name")) if isinstance(matched, dict) else _text(matched)
        if name and name not in direction_names:
            direction_names.append(name)
    direction = " / ".join(direction_names) or _text(route.get("matched_direction"))
    delivered = []
    errors = []
    for owner in route.get("owners", []) or []:
        person = _text(owner.get("name")) if isinstance(owner, dict) else _text(owner)
        if not person:
            continue
        receiver_ref = _text(owner.get("receiver_ref")) if isinstance(owner, dict) else ""
        if cas_config and not receiver_ref:
            errors.append(f"{person}: FastCAS 服务投递需要名册中的 receiver_ref")
            continue
        try:
            delivered.append(
                _publish(
                    endpoint,
                    ingest_key,
                    person,
                    paper,
                    trends,
                    direction,
                    cas_config=cas_config,
                    receiver_ref=receiver_ref,
                )
            )
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            errors.append(f"{person}: {exc}")
    return {"paper": paper, "route": route, "route_code": route_code, "delivered": delivered, "errors": errors}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Automatically process and publish a FastInsight paper")
    parser.add_argument("--paper", required=True, help="JSON paper object or file")
    parser.add_argument("--roster", default=str(ROSTER_DEFAULT))
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--ingest-key", default=None)
    parser.add_argument(
        "--trend-text",
        default=None,
        help="LLM-generated trend summary to use instead of API trend analysis",
    )
    args = parser.parse_args()
    if not _text(args.trend_text):
        parser.error(
            "--trend-text is required: FastInsight cards must carry an "
            "LLM-generated 2-5 sentence trend summary"
        )
    source = Path(args.paper)
    paper = json.loads(source.read_text(encoding="utf-8")) if source.exists() else json.loads(args.paper)
    print(
        json.dumps(
            process(
                paper,
                roster_path=args.roster,
                endpoint=args.endpoint,
                ingest_key=args.ingest_key,
                trend_text=args.trend_text,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
