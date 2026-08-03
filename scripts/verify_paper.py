#!/usr/bin/env python3
"""Identify and enrich a paper from a title or search query.

Queries Semantic Scholar, arXiv, then Crossref. The selected candidate is ranked
by title similarity instead of blindly trusting the first API result. The
command remains best-effort: a failed lookup returns structured error details so
the caller can keep using metadata extracted from the screenshot.
"""
import argparse
import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "FastInsight/1.0 (academic lookup; local tooling)"


def _get(url, timeout=15):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if "semanticscholar.org" in url:
        api_key = os.environ.get("S2_API_KEY", "").strip()
        if api_key:
            headers["X-API-KEY"] = api_key
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _normalize_title(value):
    value = str(value or "").casefold()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value).strip()


def _candidate_score(query, candidate):
    query_title = _normalize_title(query)
    candidate_title = _normalize_title(candidate.get("title"))
    if not query_title or not candidate_title:
        return 0.0
    query_tokens = set(query_title.split())
    candidate_tokens = set(candidate_title.split())
    overlap = len(query_tokens & candidate_tokens) / max(len(query_tokens), 1)
    similarity = difflib.SequenceMatcher(
        None, query_title, candidate_title
    ).ratio()
    return round(0.65 * similarity + 0.35 * overlap, 6)


def _rank_candidates(query, candidates):
    ranked = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or not candidate.get("title"):
            continue
        item = dict(candidate)
        item["match_score"] = _candidate_score(query, item)
        ranked.append((item["match_score"], -index, item))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked]


def _search_semantic_scholar(query, limit):
    base = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": limit,
        "fields": "title,year,abstract,authors,venue,externalIds,url,publicationDate",
    }
    url = base + "?" + urllib.parse.urlencode(params)
    data = json.loads(_get(url))
    output = []
    for paper in data.get("data", []):
        authors = ", ".join(
            (author.get("name") or "")
            for author in (paper.get("authors") or [])[:6]
        )
        external_ids = paper.get("externalIds") or {}
        arxiv_id = external_ids.get("Arxiv")
        output.append(
            {
                "title": paper.get("title"),
                "authors": authors,
                "year": paper.get("year"),
                "venue": paper.get("venue"),
                "abstract": (paper.get("abstract") or "")[:1200],
                "url": paper.get("url")
                or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None),
                "doi": external_ids.get("DOI"),
                "arxiv_id": arxiv_id,
            }
        )
    return output


def _search_arxiv(query, limit):
    import xml.etree.ElementTree as ET

    base = "https://export.arxiv.org/api/query"
    params = {"search_query": f"all:{query}", "max_results": limit, "start": 0}
    url = base + "?" + urllib.parse.urlencode(params)
    root = ET.fromstring(_get(url))
    namespace = {"a": "http://www.w3.org/2005/Atom"}
    output = []
    for entry in root.findall("a:entry", namespace):
        title = " ".join(
            entry.findtext("a:title", default="", namespaces=namespace).split()
        )
        abstract = " ".join(
            entry.findtext("a:summary", default="", namespaces=namespace).split()
        )
        authors = ", ".join(
            author.findtext("a:name", default="", namespaces=namespace)
            for author in entry.findall("a:author", namespace)[:6]
        )
        arxiv_url = entry.findtext("a:id", default="", namespaces=namespace)
        published = (
            entry.findtext("a:published", default="", namespaces=namespace) or ""
        )[:4]
        output.append(
            {
                "title": title,
                "authors": authors,
                "year": published or None,
                "venue": "arXiv",
                "abstract": abstract[:1200],
                "url": arxiv_url,
                "doi": None,
                "arxiv_id": arxiv_url.split("/abs/")[-1],
            }
        )
    return output


def _search_crossref(query, limit):
    base = "https://api.crossref.org/works"
    params = {"query.bibliographic": query, "rows": limit}
    mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto
    url = base + "?" + urllib.parse.urlencode(params)
    data = json.loads(_get(url))
    output = []
    for item in data.get("message", {}).get("items", []):
        title = (item.get("title") or [""])[0]
        authors = ", ".join(
            (
                f"{author.get('given', '')} {author.get('family', '')}"
            ).strip()
            for author in item.get("author", [])[:6]
        )
        parts = (
            item.get("issued", {}).get("date-parts", [[""]])[0]
            if item.get("issued")
            else [""]
        )
        year = parts[0] if parts else None
        venue = (item.get("container-title") or [""])[0]
        doi = item.get("DOI")
        output.append(
            {
                "title": title,
                "authors": authors,
                "year": year,
                "venue": venue,
                "abstract": "",
                "url": f"https://doi.org/{doi}" if doi else None,
                "doi": doi,
                "arxiv_id": None,
            }
        )
    return output


def main():
    parser = argparse.ArgumentParser(
        description="Identify and enrich a paper via academic APIs."
    )
    parser.add_argument("--query", required=True, help="paper title or query string")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json-out", help="write JSON to this file instead of stdout")
    args = parser.parse_args()

    query = args.query.strip()
    if not query:
        parser.error("--query must not be empty")
    limit = max(1, min(args.limit, 100))

    result = {
        "best": None,
        "candidates": [],
        "source": "none",
        "error": None,
        "warnings": [],
    }
    sources = [
        ("SemanticScholar", _search_semantic_scholar),
        ("arXiv", _search_arxiv),
        ("Crossref", _search_crossref),
    ]
    errors = []
    for source_name, search in sources:
        try:
            candidates = _rank_candidates(query, search(query, limit))
            if candidates:
                result["candidates"] = candidates
                result["best"] = candidates[0]
                result["source"] = source_name
                break
        except urllib.error.HTTPError as exc:
            errors.append(f"{source_name}: HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError) as exc:
            errors.append(f"{source_name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - best effort across providers
            errors.append(f"{source_name}: {exc}")

    if errors:
        result["warnings"] = errors
    if not result["candidates"]:
        result["error"] = "; ".join(errors) or "all sources returned no results"

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)

    sys.exit(0 if result["candidates"] else 1)


if __name__ == "__main__":
    main()
