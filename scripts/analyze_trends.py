#!/usr/bin/env python3
"""analyze_trends.py — Analyze citation trends and related work for a paper.

Reads a paper JSON (same format as verify_paper.py output, uses "best" if present)
and queries public APIs for citation data, field of study, and related papers.
Outputs JSON with a trend summary suitable for inclusion in a Feishu card.

Usage:
    python scripts/analyze_trends.py --paper paper.json --json-out trends.json

If Semantic Scholar is rate-limited, falls back to arXiv for related papers.
No API key required.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "FastInsight/1.0 (trend analysis; local tooling)"


def _get(url, timeout=15):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    api_key = os.environ.get("S2_API_KEY", "").strip()
    if api_key and "semanticscholar.org" in url:
        headers["X-API-KEY"] = api_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def normalize_paper(p):
    if isinstance(p, dict) and isinstance(p.get("best"), dict):
        return p["best"]
    return p


def _lookup_semantic_scholar(paper):
    """Look up paper on Semantic Scholar by title, return citation data + related."""
    title = paper.get("title") or ""
    if not title:
        return None

    # Search by title
    base = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": title[:200],
        "limit": 1,
        "fields": "title,year,citationCount,influentialCitationCount,"
        "fieldsOfStudy,references.title,references.year,references.url,"
        "url,venue",
    }
    url = base + "?" + urllib.parse.urlencode(params)
    data = json.loads(_get(url))
    results = data.get("data", [])
    if not results:
        return None

    p = results[0]
    # Gather related papers from references (pick top 5 most relevant)
    related = []
    for ref in (p.get("references") or [])[:10]:
        if ref.get("title"):
            related.append(
                {
                    "title": ref.get("title"),
                    "year": ref.get("year"),
                    "url": ref.get("url") or "",
                    "citation_count": None,
                }
            )
    related = related[:5]

    return {
        "citation_count": p.get("citationCount"),
        "influential_citation_count": p.get("influentialCitationCount"),
        "fields_of_study": p.get("fieldsOfStudy") or [],
        "venue": p.get("venue") or "",
        "url": p.get("url") or "",
        "related_papers": related,
    }


def _search_arxiv_related(query, limit=5):
    """Fallback: search arXiv for related papers by keyword."""
    import xml.etree.ElementTree as ET

    base = "https://export.arxiv.org/api/query"
    params = {
        "search_query": f"all:{query[:200]}",
        "max_results": max(1, min(limit, 20)),
        "sortBy": "relevance",
    }
    url = base + "?" + urllib.parse.urlencode(params)
    raw = _get(url)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    related = []
    for e in root.findall("a:entry", ns):
        t = " ".join(e.findtext("a:title", default="", namespaces=ns).split())
        u = e.findtext("a:id", default="", namespaces=ns)
        y = (e.findtext("a:published", default="", namespaces=ns) or "")[:4]
        related.append(
            {"title": t, "year": y or None, "url": u, "citation_count": None}
        )
    return related


def build_summary(paper, trends, source):
    """Build a human-readable trend summary string."""
    parts = []

    cit = trends.get("citation_count")
    inf = trends.get("influential_citation_count")
    if cit is not None:
        cit_str = f"引用 {cit} 次"
        if inf is not None and inf > 0:
            cit_str += f"（高影响力 {inf} 次）"
        parts.append(cit_str)

    fields = trends.get("fields_of_study") or []
    if fields:
        parts.append("研究领域：" + "、".join(fields[:3]))

    related = trends.get("related_papers") or []
    if related:
        parts.append(f"相关论文 {len(related)} 篇")

    year = paper.get("year") or ""
    venue = paper.get("venue") or ""
    if year or venue:
        parts.append(f"发表于 {year} {venue}".strip())

    if not parts:
        if source == "none":
            return "暂无引用数据（API 不可达），建议手动查看原文了解趋势。"
        return "趋势数据有限，建议查看原文获取更多上下文。"

    summary = " | ".join(parts)

    # Add a brief qualitative note
    if cit is not None:
        if cit > 100:
            summary += "。该论文影响力较高，属领域内重要工作。"
        elif cit > 10:
            summary += "。该论文受到一定关注，值得持续跟踪。"
        else:
            summary += "。该论文较新或引用尚少，属前沿探索阶段。"

    return summary


def main():
    ap = argparse.ArgumentParser(
        description="Analyze citation trends and related work for a paper."
    )
    ap.add_argument("--paper", required=True, help="paper JSON file")
    ap.add_argument("--json-out", help="write JSON to this file")
    ap.add_argument(
        "--limit", type=int, default=5, help="max related papers (default 5)"
    )
    args = ap.parse_args()

    with open(args.paper, "r", encoding="utf-8") as f:
        paper = normalize_paper(json.load(f))
    if not isinstance(paper, dict):
        ap.error("--paper must contain a JSON object or a `best` object")

    limit = max(1, min(args.limit, 20))
    title = paper.get("title") or ""
    abstract = (paper.get("abstract") or "")[:300]
    url = paper.get("url") or ""

    result = {
        "paper_url": url,
        "citation_count": None,
        "influential_citation_count": None,
        "fields_of_study": [],
        "related_papers": [],
        "trend_summary": "",
        "source": "none",
        "error": None,
    }

    # Try Semantic Scholar first
    try:
        ss_data = _lookup_semantic_scholar(paper)
        if ss_data:
            result.update(ss_data)
            result["source"] = "SemanticScholar"
            # Use SS URL if paper had none
            if not result["paper_url"]:
                result["paper_url"] = ss_data.get("url") or url
    except urllib.error.HTTPError as ex:
        result["error"] = f"SemanticScholar: HTTP {ex.code}"
    except Exception as ex:
        result["error"] = f"SemanticScholar: {ex}"

    # Fallback: arXiv for related papers if SS failed or returned none
    if not result["related_papers"]:
        try:
            query = title or abstract
            if query:
                related = _search_arxiv_related(query, limit)
                if related:
                    result["related_papers"] = related
                    if result["source"] == "none":
                        result["source"] = "arXiv"
        except Exception as ex:
            if result["error"]:
                result["error"] += f"; arXiv: {ex}"
            else:
                result["error"] = f"arXiv: {ex}"

    result["trend_summary"] = build_summary(paper, result, result["source"])

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)

    sys.exit(0)


if __name__ == "__main__":
    main()
