#!/usr/bin/env python3
"""Identify and enrich a paper from a title, DOI, arXiv ID, or paper URL.

Lookup strategy, in order:

1. Detect identifiers (DOI / arXiv ID) embedded in the query and try
   identifier-first direct lookups. Search indexes often lag newly published
   works, but a registered DOI is usually resolvable immediately through
   Crossref/OpenAlex/Semantic Scholar.
2. Run title searches across Semantic Scholar, OpenAlex, arXiv, and Crossref.
   The selected candidate is ranked by title similarity; identifier-exact
   matches always win.
3. Build canonical URLs from known identifiers so a published paper is never
   left without a link just because one provider's index is stale.
4. A generic web-search fallback (SerpAPI/Google, Bing, and/or Google Custom
   Search) runs when the four academic sources miss entirely OR their best hit
   is a low-confidence lookalike (``match_score`` below 0.6), so a paraphrased
   headline can still reach the real paper/news page.
5. Web hits and explicit paper URLs are enriched best-effort by fetching the
   page and reading ``citation_*`` / Open Graph ``<meta>`` tags, filling
   title, authors, abstract, venue, DOI/arXiv, and the canonical URL.
6. Web-search/page hits are discovery aids, not academic verification. They are
   flagged with ``verified_by_academic_sources: false`` and never silently
   replace screenshot-extracted metadata without review.

No API key is required for the academic lookups. S2_API_KEY,
CROSSREF_MAILTO, and OPENALEX_MAILTO optionally raise rate limits. The generic
web fallback needs at least one of SERPAPI_API_KEY, BING_SEARCH_API_KEY, or
GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_CX. The script also loads the skill's
``.env`` automatically, so the web fallback is not silently disabled when a
caller forgets to export credentials first.
"""

import argparse
import concurrent.futures
import difflib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from text_clip import ABSTRACT_STORE_LIMIT, clip_at_sentence, prefer_abstract


def _load_env_file(path):
    """Best-effort load of simple ``KEY=VALUE`` lines into the environment.

    Existing environment variables are never overridden. This keeps the generic
    web-search fallback working (e.g. SERPAPI_API_KEY) even when a caller runs
    ``verify_paper.py`` directly without sourcing the skill's ``.env`` first.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in ("'", '"')
                ):
                    value = value[1:-1]
                # Prefer .env over a blank/stale env placeholder so the
                # web fallback cannot be disabled by an unset-looking key.
                if key and not os.environ.get(key, "").strip():
                    os.environ[key] = value
    except OSError:
        pass


# The skill keeps credentials in FastInsight/.env. Load it before any provider
# inspects os.environ so web search is actually configured when academic APIs
# miss or only return a low-confidence lookalike.
for _env_path in (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
    os.path.join(os.getcwd(), ".env"),
):
    _load_env_file(_env_path)


UA = "FastInsight/1.1 (academic lookup; local tooling)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
CONFIDENCE_THRESHOLD = 0.6
DOI_HOSTS = {"doi.org", "www.doi.org", "dx.doi.org"}
S2_HOSTS = {"semanticscholar.org", "www.semanticscholar.org", "api.semanticscholar.org"}
OPENALEX_HOSTS = {"openalex.org", "www.openalex.org", "api.openalex.org"}
SOURCE_RANK = {
    "Crossref": 0,
    "SemanticScholar": 1,
    "arXiv": 2,
    "OpenAlex": 3,
    "unknown": 99,
}
ARXIV_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s<>\"']+", re.IGNORECASE)
ARXIV_LABEL_RE = re.compile(
    r"(?:https?://)?arxiv\.org/(?:abs|pdf)/"
    r"([a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?|\d{4}\.\d{4,5}(?!\d)(?:v\d+)?)|"
    r"arXiv:\s*([a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?|\d{4}\.\d{4,5}(?!\d)(?:v\d+)?)",
    re.IGNORECASE,
)
ARXIV_BARE_RE = re.compile(
    r"(?<![\w./])(\d{4}\.\d{4,5}(?:v\d+)?)(?![\w.])"
)


def _get(url, timeout=12, extra_headers=None):
    headers = {
        "User-Agent": UA,
        "Accept": (
            "application/json, application/atom+xml, application/xml, "
            "text/html;q=0.9"
        ),
    }
    if "semanticscholar.org" in url:
        api_key = os.environ.get("S2_API_KEY", "").strip()
        if api_key:
            headers["X-API-KEY"] = api_key
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _get_json(url, timeout=12, extra_headers=None):
    return json.loads(
        _get(url, timeout=timeout, extra_headers=extra_headers)
    )


def _hostname(url):
    if not url:
        return ""
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except (TypeError, ValueError):
        return ""


def _clean_doi(value):
    if not value:
        return ""
    value = str(value).strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    while value and value[-1] in ".,;:!?":
        value = value[:-1]
    if value.endswith(")") and value.count("(") < value.count(")"):
        value = value[:-1]
    return value.lower().strip()


def _doi_url(doi):
    return f"https://doi.org/{_clean_doi(doi)}" if _clean_doi(doi) else ""


def _arxiv_abs_url(arxiv_id):
    value = str(arxiv_id or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    value = re.sub(r"^arXiv:", "", value, flags=re.IGNORECASE).strip()
    return f"https://arxiv.org/abs/{value}"


def _arxiv_dedupe_key(arxiv_id):
    value = _arxiv_abs_url(arxiv_id).rsplit("/abs/", 1)[-1]
    return re.sub(r"v\d+$", "", value, flags=re.IGNORECASE).casefold()


def _normalize_title(value):
    value = str(value or "").casefold()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value).strip()


def _strip_xml(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(html.unescape(value).split())


def _decode_inverted_index(index):
    if not isinstance(index, dict) or not index:
        return ""
    positions = {}
    for word, indexes in index.items():
        for position in indexes:
            positions[int(position)] = word
    text = " ".join(positions[i] for i in sorted(positions))
    return " ".join(text.split())


def _find_identifiers(text):
    """Extract DOI / arXiv IDs from a query string without mutating it."""
    identifiers = {}
    source = str(text or "")

    doi_matches = [m.group(0) for m in DOI_RE.finditer(source)]
    if doi_matches:
        identifiers["doi"] = _clean_doi(doi_matches[0])

    arxiv_id = ""
    label_match = ARXIV_LABEL_RE.search(source)
    if label_match:
        arxiv_id = (label_match.group(1) or label_match.group(2) or "").strip()
    else:
        bare_match = ARXIV_BARE_RE.search(source)
        if bare_match:
            arxiv_id = bare_match.group(1).strip()
    if arxiv_id:
        identifiers["arxiv_id"] = arxiv_id

    return identifiers


def _searchable_query(text):
    """Return the query without URLs/identifiers so title search stays clean."""
    cleaned = re.sub(
        r"https?://(?:dx\.)?doi\.org/[^\s]+", " ", text or "", flags=re.IGNORECASE
    )
    cleaned = re.sub(r"\b10\.\d{4,9}/[^\s]+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"https?://arxiv\.org/(?:abs|pdf)/[^\s]+", " ", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"arXiv:[^\s]+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d{4}\.\d{5}(?:v\d+)?\b", " ", cleaned)
    return " ".join(cleaned.split()).strip()


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
        exact = bool(item.get("exact"))
        score = 1.0 if exact else _candidate_score(query, item)
        item["match_score"] = score
        item["exact"] = exact
        source_rank = SOURCE_RANK.get(
            str(item.get("provider") or ""), SOURCE_RANK["unknown"]
        )
        ranked.append(
            (score, 1 if exact else 0, -source_rank, -index, item)
        )
    ranked.sort(key=lambda row: (row[0], row[1], row[2], row[3]), reverse=True)
    return [row[4] for row in ranked]


def _dedupe_candidates(candidates):
    """Remove exact DOI / arXiv duplicates, keeping the highest-ranked record."""
    deduped = []
    seen_doi = set()
    seen_arxiv = set()
    seen_titles = set()
    for candidate in candidates:
        doi = _clean_doi(candidate.get("doi"))
        arxiv_id = str(candidate.get("arxiv_id") or "").strip()
        arxiv_key = _arxiv_dedupe_key(arxiv_id) if arxiv_id else ""
        normalized = _normalize_title(candidate.get("title"))
        title_key = tuple(sorted(set(normalized.split()))) if normalized else ()

        duplicate = bool(
            (doi and doi in seen_doi)
            or (arxiv_key and arxiv_key in seen_arxiv)
            or (title_key and title_key in seen_titles)
        )
        if duplicate:
            continue
        if doi:
            seen_doi.add(doi)
        if arxiv_key:
            seen_arxiv.add(arxiv_key)
        if title_key:
            seen_titles.add(title_key)
        deduped.append(candidate)
    return deduped


def _canonicalize_candidate(candidate):
    """Ensure every candidate carries a canonical, clickable URL."""
    raw_url = str(candidate.get("url") or "").strip()
    host = _hostname(raw_url)
    doi = _clean_doi(candidate.get("doi"))
    arxiv_id = str(candidate.get("arxiv_id") or "").strip()

    if candidate.get("web_search_result") and raw_url and not doi and not arxiv_id:
        candidate["url"] = raw_url
        candidate["url_kind"] = "web"
        return candidate
    if doi and (not raw_url or host in S2_HOSTS | OPENALEX_HOSTS):
        candidate["url"] = _doi_url(doi)
        candidate["url_kind"] = "doi"
    elif arxiv_id and (not raw_url or host in S2_HOSTS):
        candidate["url"] = _arxiv_abs_url(arxiv_id)
        candidate["url_kind"] = "arxiv"
    elif raw_url and host in DOI_HOSTS:
        candidate["url"] = _doi_url(doi) if doi else raw_url
        candidate["url_kind"] = "doi"
    elif raw_url:
        if host in S2_HOSTS:
            candidate["url_kind"] = "semanticscholar"
        elif host in OPENALEX_HOSTS:
            candidate["url_kind"] = "openalex"
        elif "arxiv.org" in host:
            candidate["url_kind"] = "arxiv"
        else:
            candidate["url_kind"] = "publisher"
    else:
        candidate["url"] = ""
        candidate["url_kind"] = "none"
    return candidate



def _set_abstract(candidate, text, source):
    """Store an official or search abstract without mid-sentence truncation."""
    abstract = clip_at_sentence(text, ABSTRACT_STORE_LIMIT)
    candidate["abstract"] = abstract
    if abstract:
        candidate["abstract_source"] = source
    elif "abstract_source" not in candidate:
        candidate["abstract_source"] = ""
    return abstract


def _crossref_item_to_candidate(item, provider="Crossref", exact=False):
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
    doi = _clean_doi(item.get("DOI"))

    landing = ""
    resource_url = (item.get("resource") or {}).get("primary", {}).get("URL")
    for raw_url in (item.get("URL"), resource_url):
        if raw_url and _hostname(raw_url) not in DOI_HOSTS:
            landing = str(raw_url)
            break

    pdf_url = ""
    for link in item.get("link") or []:
        link_url = (link or {}).get("URL") or ""
        content_type = str((link or {}).get("content-type") or "").casefold()
        if link_url and "pdf" in content_type and not pdf_url:
            pdf_url = link_url

    url = landing or (_doi_url(doi) if doi else "")
    candidate = {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "url": url,
        "url_kind": "publisher" if landing else ("doi" if doi else "none"),
        "pdf_url": pdf_url,
        "doi": doi or None,
        "arxiv_id": None,
        "provider": provider,
        "exact": exact,
    }
    _set_abstract(candidate, _strip_xml(item.get("abstract")), "paper")
    return candidate


def _openalex_item_to_candidate(item, provider="OpenAlex", exact=False):
    title = item.get("title") or item.get("display_name") or ""
    authors = ", ".join(
        (authorship.get("author") or {}).get("display_name")
        for authorship in (item.get("authorships") or [])[:6]
        if (authorship.get("author") or {}).get("display_name")
    )
    year = item.get("publication_year")
    if not year and item.get("publication_date"):
        year = str(item.get("publication_date"))[:4]
    primary = item.get("primary_location") or {}
    source = primary.get("source") or {}
    landing = str(primary.get("landing_page_url") or "").strip()
    if _hostname(landing) in OPENALEX_HOSTS:
        landing = ""
    best_oa = item.get("best_oa_location") or {}
    pdf_url = str(best_oa.get("pdf_url") or "").strip()
    doi = _clean_doi(item.get("doi"))
    candidate = {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": (source.get("display_name") or ""),
        "url": landing or (_doi_url(doi) if doi else ""),
        "url_kind": "publisher" if landing else ("doi" if doi else "openalex"),
        "pdf_url": pdf_url,
        "doi": doi or None,
        "arxiv_id": None,
        "provider": provider,
        "exact": exact,
    }
    _set_abstract(
        candidate,
        _strip_xml(item.get("abstract"))
        or _decode_inverted_index(item.get("abstract_inverted_index")),
        "paper",
    )
    return candidate


def _s2_paper_to_candidate(paper, provider="SemanticScholar", exact=False):
    external_ids = paper.get("externalIds") or {}
    arxiv_id = external_ids.get("Arxiv")
    doi = _clean_doi(external_ids.get("DOI"))
    candidate = {
        "title": paper.get("title"),
        "authors": ", ".join(
            (author.get("name") or "")
            for author in (paper.get("authors") or [])[:6]
        ),
        "year": paper.get("year"),
        "venue": paper.get("venue"),
        "url": paper.get("url"),
        "url_kind": "semanticscholar",
        "pdf_url": "",
        "doi": doi or None,
        "arxiv_id": arxiv_id,
        "provider": provider,
        "exact": exact,
    }
    _set_abstract(candidate, paper.get("abstract"), "paper")
    return candidate


def _lookup_crossref_by_doi(doi):
    path = urllib.parse.quote(_clean_doi(doi), safe="/")
    data = _get_json(f"https://api.crossref.org/works/{path}")
    item = data.get("message") or {}
    if not item.get("DOI"):
        return None
    return _crossref_item_to_candidate(item, provider="Crossref", exact=True)


def _lookup_openalex_by_doi(doi):
    params = {"filter": f"doi:{_clean_doi(doi)}", "per-page": "1"}
    data = _get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    for item in data.get("results", []) or []:
        return _openalex_item_to_candidate(item, provider="OpenAlex", exact=True)
    return None


def _lookup_s2_by_doi(doi):
    cleaned = _clean_doi(doi)
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{cleaned}"
    fields = "title,year,abstract,authors,venue,externalIds,url,publicationDate"
    data = _get_json(f"{url}?fields={urllib.parse.quote(fields)}")
    return _s2_paper_to_candidate(data, provider="SemanticScholar", exact=True)


def _arxiv_entries(root):
    output = []
    for entry in root.findall("a:entry", ARXIV_ATOM_NS):
        title = " ".join(
            entry.findtext("a:title", default="", namespaces=ARXIV_ATOM_NS).split()
        )
        abstract = " ".join(
            entry.findtext("a:summary", default="", namespaces=ARXIV_ATOM_NS).split()
        )
        authors = ", ".join(
            author.findtext("a:name", default="", namespaces=ARXIV_ATOM_NS)
            for author in entry.findall("a:author", ARXIV_ATOM_NS)[:6]
        )
        arxiv_url = entry.findtext("a:id", default="", namespaces=ARXIV_ATOM_NS)
        published = (
            entry.findtext("a:published", default="", namespaces=ARXIV_ATOM_NS) or ""
        )[:4]
        arxiv_id = arxiv_url.split("/abs/")[-1] if arxiv_url else ""
        item = {
            "title": title,
            "authors": authors,
            "year": published or None,
            "venue": "arXiv",
            "url": arxiv_url,
            "url_kind": "arxiv",
            "pdf_url": "",
            "doi": None,
            "arxiv_id": arxiv_id,
            "provider": "arXiv",
            "exact": False,
        }
        _set_abstract(item, abstract, "paper")
        output.append(item)
    return output


def _lookup_arxiv_by_id(arxiv_id):
    params = {"id_list": str(arxiv_id).strip(), "max_results": 1}
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    root = ET.fromstring(_get(url))
    entries = _arxiv_entries(root)
    if entries:
        entries[0]["exact"] = True
        entries[0]["url_kind"] = "arxiv"
    return entries


def _direct_lookups(identifiers):
    """Identifier-first lookups for DOIs and arXiv IDs."""
    candidates = []
    errors = []
    doi = _clean_doi(identifiers.get("doi"))
    if doi:
        for provider_name, lookup in (
            ("Crossref", _lookup_crossref_by_doi),
            ("OpenAlex", _lookup_openalex_by_doi),
            ("SemanticScholar", _lookup_s2_by_doi),
        ):
            try:
                candidate = lookup(doi)
                if candidate:
                    candidates.append(candidate)
            except urllib.error.HTTPError as exc:
                if exc.code not in (404, 400):
                    errors.append(f"{provider_name} DOI direct: HTTP {exc.code}")
            except (urllib.error.URLError, TimeoutError) as exc:
                errors.append(f"{provider_name} DOI direct: {exc}")
            except Exception as exc:  # noqa: BLE001 - best-effort providers
                errors.append(f"{provider_name} DOI direct: {exc}")

    arxiv_id = str(identifiers.get("arxiv_id") or "").strip()
    if arxiv_id:
        try:
            candidates.extend(_lookup_arxiv_by_id(arxiv_id))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            errors.append(f"arXiv ID direct: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"arXiv ID direct: {exc}")

    return candidates, errors


def _search_semantic_scholar(query, limit):
    base = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": max(1, min(limit, 100)),
        "fields": "title,year,abstract,authors,venue,externalIds,url,publicationDate",
    }
    data = _get_json(base + "?" + urllib.parse.urlencode(params))
    return [
        _s2_paper_to_candidate(paper, provider="SemanticScholar")
        for paper in (data.get("data") or [])
    ]


def _search_openalex(query, limit):
    params = {
        "filter": f"title.search:{query}",
        "per-page": str(max(1, min(limit, 100))),
    }
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto
    data = _get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    return [
        _openalex_item_to_candidate(item, provider="OpenAlex")
        for item in (data.get("results") or [])
    ]


def _search_arxiv(query, limit):
    base = "https://export.arxiv.org/api/query"
    params = {
        "search_query": f"all:{query}",
        "max_results": max(1, min(limit, 100)),
        "sortBy": "relevance",
    }
    root = ET.fromstring(_get(base + "?" + urllib.parse.urlencode(params)))
    return _arxiv_entries(root)


def _search_crossref(query, limit):
    base = "https://api.crossref.org/works"
    params = {
        "query.bibliographic": query,
        "rows": max(1, min(limit, 100)),
        "select": (
            "DOI,title,author,issued,container-title,URL,link,resource,abstract"
        ),
    }
    mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto
    data = _get_json(base + "?" + urllib.parse.urlencode(params))
    return [
        _crossref_item_to_candidate(item, provider="Crossref")
        for item in (data.get("message", {}).get("items") or [])
    ]


def _search_providers(query, limit):
    """Run the four title-search providers concurrently and merge candidates."""
    providers = [
        ("SemanticScholar", _search_semantic_scholar),
        ("OpenAlex", _search_openalex),
        ("arXiv", _search_arxiv),
        ("Crossref", _search_crossref),
    ]
    candidates = []
    errors = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(providers)
    ) as executor:
        futures = {
            executor.submit(search, query, limit): name
            for name, search in providers
        }
        for future in concurrent.futures.as_completed(futures):
            provider_name = futures[future]
            try:
                candidates.extend(future.result())
            except urllib.error.HTTPError as exc:
                errors.append(f"{provider_name}: HTTP {exc.code}")
            except (urllib.error.URLError, TimeoutError) as exc:
                errors.append(f"{provider_name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - best-effort providers
                errors.append(f"{provider_name}: {exc}")
    return candidates, errors


# ---------------------------------------------------------------------------
# Generic web-search fallback (used only when all academic sources miss)
# ---------------------------------------------------------------------------


def _web_item_to_candidate(item, provider):
    """Convert a generic search-result item into a lightweight candidate."""
    title = " ".join(str(item.get("title") or item.get("name") or "").split())
    url = str(item.get("link") or item.get("url") or "").strip()

    year = None
    date_text = str(item.get("datePublished") or item.get("date") or "")
    year_match = re.search(r"\b(19|20)\d{2}\b", date_text)
    if year_match:
        year = year_match.group(0)

    candidate = {
        "title": title,
        "authors": "",
        "year": year,
        "venue": "",
        "url": url,
        "url_kind": "web",
        "pdf_url": "",
        "doi": None,
        "arxiv_id": None,
        "provider": provider,
        "exact": False,
        "web_search_result": True,
        "verified_by_academic_sources": False,
    }
    _set_abstract(candidate, item.get("snippet"), "search")
    return candidate


class _PageMetadataParser(HTMLParser):
    """Collect <title> and common paper/news <meta> tags from an HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.meta = {}
        self.authors = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        name = str(
            attrs_dict.get("name") or attrs_dict.get("property") or ""
        ).strip().lower()
        content = str(attrs_dict.get("content") or "").strip()
        if not name or not content:
            return
        if name == "citation_author":
            self.authors.append(content)
        else:
            self.meta[name] = content

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    @property
    def title(self):
        return " ".join("".join(self.title_parts).split())


def _fetch_page_candidate(url, timeout=12):
    """Best-effort page fetch; extract title/authors/abstract/venue/DOI/arXiv.

    Returns a ``WebPage`` candidate with ``page_fetched: true`` when the page
    exposes any usable metadata, otherwise ``None``.
    """
    try:
        raw = _get(
            url,
            timeout=timeout,
            extra_headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None
    except Exception:
        return None
    if not raw:
        return None

    parser = _PageMetadataParser()
    try:
        parser.feed(raw)
    except Exception:
        pass
    meta = parser.meta or {}

    title = (
        meta.get("citation_title")
        or meta.get("og:title")
        or meta.get("twitter:title")
        or parser.title
        or ""
    ).strip()
    citation_abstract = (meta.get("citation_abstract") or "").strip()
    search_abstract = (
        meta.get("og:description")
        or meta.get("description")
        or ""
    ).strip()
    abstract = citation_abstract or search_abstract
    abstract_source = "paper" if citation_abstract else "search"
    authors = ", ".join(
        author for author in parser.authors if str(author).strip()
    )
    if not authors and meta.get("citation_author"):
        authors = str(meta.get("citation_author")).strip()
    if not authors and meta.get("author"):
        authors = str(meta.get("author")).strip()

    doi = _clean_doi(meta.get("citation_doi") or "")
    if not doi:
        doi_match = DOI_RE.search(raw)
        if doi_match:
            doi = _clean_doi(doi_match.group(0))
    arxiv_id = ""
    arxiv_label = ARXIV_LABEL_RE.search(raw)
    if arxiv_label:
        arxiv_id = (arxiv_label.group(1) or arxiv_label.group(2) or "").strip()
    elif meta.get("citation_arxiv_id"):
        arxiv_id = str(meta.get("citation_arxiv_id")).strip()

    year = ""
    for key in (
        "citation_publication_date",
        "citation_online_date",
        "citation_date",
        "citation_publication_year",
        "article:published_time",
        "og:published_time",
        "date",
    ):
        year_match = re.search(r"(19|20)\d{2}", str(meta.get(key) or ""))
        if year_match:
            year = year_match.group(0)
            break

    venue = (
        meta.get("citation_conference_title")
        or meta.get("citation_journal_title")
        or meta.get("og:site_name")
        or ""
    ).strip()
    canonical_url = (meta.get("og:url") or url).strip()
    if not (title or abstract or authors or doi or arxiv_id):
        return None

    candidate = {
        "title": " ".join(title.split()),
        "authors": ", ".join(part for part in authors.split(",") if part.strip()) if authors else "",
        "year": year or None,
        "venue": venue,
        "url": canonical_url,
        "url_kind": "web",
        "pdf_url": "",
        "doi": doi or None,
        "arxiv_id": arxiv_id or None,
        "provider": "WebPage",
        "exact": False,
        "web_search_result": True,
        "page_fetched": True,
        "verified_by_academic_sources": False,
    }
    _set_abstract(candidate, abstract, abstract_source)
    return candidate


def _enrich_web_candidates(candidates, errors, limit=2):
    """Fill candidate title/authors/abstract/DOI by fetching its page."""
    enriched = []
    for index, candidate in enumerate(candidates):
        if index >= limit or candidate.get("page_fetched"):
            enriched.append(candidate)
            continue
        url = str(candidate.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            enriched.append(candidate)
            continue
        try:
            page = _fetch_page_candidate(url)
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment
            errors.append(f"WebPage/{candidate.get('provider')}: {exc}")
            enriched.append(candidate)
            continue
        if not page:
            enriched.append(candidate)
            continue
        copy = dict(candidate)
        for key in ("title", "authors", "year", "venue", "doi", "arxiv_id"):
            if not str(copy.get(key) or "").strip() and str(page.get(key) or "").strip():
                copy[key] = page[key]
        prefer_abstract(copy, page)
        copy["page_fetched"] = True
        copy["provider"] = "WebPage"
        copy["web_search_result"] = True
        copy["verified_by_academic_sources"] = False
        enriched.append(copy)
    return enriched


def _merge_page_into_academic(page, candidates):
    """Fold fetched page metadata into a matching academic candidate."""
    page_doi = _clean_doi(page.get("doi"))
    page_arxiv = str(page.get("arxiv_id") or "").strip()
    page_tokens = set(_normalize_title(page.get("title")).split())
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_doi = _clean_doi(candidate.get("doi"))
        candidate_arxiv = str(candidate.get("arxiv_id") or "").strip()
        candidate_tokens = set(
            _normalize_title(candidate.get("title")).split()
        )
        overlap = 0.0
        if page_tokens and candidate_tokens:
            overlap = len(page_tokens & candidate_tokens) / len(page_tokens)
        same_paper = bool(
            (page_doi and page_doi == candidate_doi)
            or (page_arxiv and _arxiv_dedupe_key(page_arxiv) == _arxiv_dedupe_key(candidate_arxiv))
            or overlap >= 0.6
        )
        if not same_paper:
            continue
        for key in ("title", "authors", "year", "venue", "doi", "arxiv_id"):
            if not str(candidate.get(key) or "").strip() and str(page.get(key) or "").strip():
                candidate[key] = page[key]
        prefer_abstract(candidate, page)
        candidate["page_enriched"] = True
        return True
    return False


def _search_serpapi_web(query, limit):
    """Google search through SerpAPI (SERPAPI_API_KEY)."""
    api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not api_key:
        return []
    params = {
        "engine": "google",
        "q": query,
        "num": str(max(1, min(limit, 10))),
        "api_key": api_key,
    }
    data = _get_json(
        "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    )
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return (data.get("organic_results") or [])[:limit]


def _search_bing_web(query, limit):
    """Bing Web Search v7 (BING_SEARCH_API_KEY)."""
    api_key = os.environ.get("BING_SEARCH_API_KEY", "").strip()
    if not api_key:
        return []
    params = {
        "q": query,
        "count": str(max(1, min(limit, 10))),
        "responseFilter": "Webpages",
        "textDecorations": "false",
    }
    data = _get_json(
        "https://api.bing.microsoft.com/v7.0/search?"
        + urllib.parse.urlencode(params),
        extra_headers={"Ocp-Apim-Subscription-Key": api_key},
    )
    return ((data.get("webPages") or {}).get("value") or [])[:limit]


def _search_google_cse_web(query, limit):
    """Google Programmable Search (GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_CX)."""
    api_key = os.environ.get("GOOGLE_SEARCH_API_KEY", "").strip()
    cx = os.environ.get("GOOGLE_SEARCH_CX", "").strip()
    if not api_key or not cx:
        return []
    params = {
        "key": api_key,
        "cx": cx,
        "q": query,
        "num": str(max(1, min(limit, 10))),
    }
    data = _get_json(
        "https://www.googleapis.com/customsearch/v1?"
        + urllib.parse.urlencode(params)
    )
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return (data.get("items") or [])[:limit]


def _configured_web_providers():
    """List web-search providers whose credentials are present in env."""
    providers = []
    if os.environ.get("SERPAPI_API_KEY", "").strip():
        providers.append(("SerpAPI-Google", _search_serpapi_web))
    if os.environ.get("BING_SEARCH_API_KEY", "").strip():
        providers.append(("Bing", _search_bing_web))
    if (
        os.environ.get("GOOGLE_SEARCH_API_KEY", "").strip()
        and os.environ.get("GOOGLE_SEARCH_CX", "").strip()
    ):
        providers.append(("Google Custom Search", _search_google_cse_web))
    return providers


def _search_web_fallback(query, limit):
    """Run configured generic web providers and return normalized candidates.

    Returns ``(candidates, used_providers, errors, configured)``.
    """
    providers = _configured_web_providers()
    if not providers:
        return [], [], [], False

    raw_candidates = []
    errors = []
    used = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(providers)
    ) as executor:
        futures = {
            executor.submit(search, query, limit): name
            for name, search in providers
        }
        for future in concurrent.futures.as_completed(futures):
            provider_name = futures[future]
            used.append(provider_name)
            try:
                for item in future.result():
                    candidate = _web_item_to_candidate(item, provider_name)
                    if candidate["title"] and candidate["url"]:
                        raw_candidates.append(candidate)
            except urllib.error.HTTPError as exc:
                errors.append(f"WebSearch/{provider_name}: HTTP {exc.code}")
            except (urllib.error.URLError, TimeoutError) as exc:
                errors.append(f"WebSearch/{provider_name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - best-effort provider
                errors.append(f"WebSearch/{provider_name}: {exc}")
    return raw_candidates, used, errors, True


def _web_search_query(searchable, query):
    """Keep a useful query for web search when title text was not available."""
    if searchable:
        return searchable
    cleaned = str(query or "")
    cleaned = re.sub(
        r"^https?://(?:www\.)?(?:dx\.)?doi\.org/",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^https?://arxiv\.org/(?:abs|pdf)/",
        "arXiv:",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _build_search_urls(query):
    """One-click search pages for cases where no canonical record exists."""
    q = urllib.parse.quote_plus(query)
    return [
        {"name": "Crossref", "url": f"https://search.crossref.org/?q={q}"},
        {"name": "OpenAlex", "url": f"https://openalex.org/search?q={q}"},
        {
            "name": "Semantic Scholar",
            "url": f"https://www.semanticscholar.org/search?q={q}",
        },
        {"name": "Google Scholar", "url": f"https://scholar.google.com/scholar?q={q}"},
        {"name": "Google", "url": f"https://www.google.com/search?q={q}"},
        {"name": "Bing", "url": f"https://www.bing.com/search?q={q}"},
    ]


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Identify and enrich a paper via academic APIs. "
            "Accepts titles, DOIs, arXiv IDs, and paper URLs."
        )
    )
    parser.add_argument("--query", required=True, help="paper title, DOI, arXiv ID, or URL")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json-out", help="write JSON to this file instead of stdout")
    args = parser.parse_args()

    query = args.query.strip()
    if not query:
        parser.error("--query must not be empty")
    limit = max(1, min(args.limit, 100))

    result = {
        "query": query,
        "detected_ids": {},
        "best": None,
        "candidates": [],
        "source": "none",
        "url": None,
        "url_kind": None,
        "search_urls": [],
        "error": None,
        "warnings": [],
    }

    identifiers = _find_identifiers(query)
    result["detected_ids"] = identifiers
    errors = []
    all_candidates = []

    if identifiers:
        direct_candidates, direct_errors = _direct_lookups(identifiers)
        all_candidates.extend(direct_candidates)
        errors.extend(direct_errors)

    searchable = _searchable_query(query)
    if searchable:
        searched, search_errors = _search_providers(searchable, limit)
        all_candidates.extend(searched)
        errors.extend(search_errors)

    for candidate in all_candidates:
        _canonicalize_candidate(candidate)

    # If the query itself is an ordinary web URL (IEEE/CISPA/news page), load
    # its <meta>/<title> metadata directly before running title searches.
    if not identifiers and re.match(r"^https?://\S+$", query, re.IGNORECASE):
        try:
            page_candidate = _fetch_page_candidate(query)
            if page_candidate:
                _canonicalize_candidate(page_candidate)
                if not _merge_page_into_academic(page_candidate, all_candidates):
                    all_candidates.append(page_candidate)
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment
            errors.append(f"WebPage: {exc}")

    ranked_all = _dedupe_candidates(
        _rank_candidates(searchable or query, all_candidates)
    )
    academic_candidates = [
        candidate
        for candidate in ranked_all
        if not candidate.get("web_search_result")
    ]
    candidates = list(ranked_all)
    result["candidates"] = candidates

    def _score(candidate):
        try:
            return float(candidate.get("match_score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    best_academic = academic_candidates[0] if academic_candidates else None
    academic_confident = bool(
        best_academic
        and (
            best_academic.get("exact")
            or _score(best_academic) >= CONFIDENCE_THRESHOLD
        )
        and best_academic.get("url")
    )
    if best_academic and not academic_confident:
        errors.append(
            "top academic candidate has match_score "
            f"{_score(best_academic):.3f} (< {CONFIDENCE_THRESHOLD}); "
            "treating it as unverified and continuing to web search"
        )

    web_configured = False
    web_used = []
    web_errors = []
    web_candidates = []
    already_has_page = any(
        candidate.get("url")
        and (
            candidate.get("page_fetched")
            or candidate.get("web_search_result")
        )
        for candidate in candidates
    )
    if not academic_confident and not already_has_page:
        web_query = _web_search_query(searchable, query)
        if web_query:
            web_candidates, web_used, web_errors, web_configured = (
                _search_web_fallback(web_query, limit)
            )
            if web_candidates:
                web_candidates = _enrich_web_candidates(
                    web_candidates, web_errors
                )
                for candidate in web_candidates:
                    _canonicalize_candidate(candidate)
                web_candidates = _dedupe_candidates(
                    _rank_candidates(web_query, web_candidates)
                )
                candidates.extend(web_candidates)
                result["candidates"] = candidates
            if not web_configured:
                errors.append(
                    "generic web search fallback not configured; set "
                    "SERPAPI_API_KEY, BING_SEARCH_API_KEY, or "
                    "GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_CX"
                )
    if web_errors:
        errors.extend(web_errors)

    web_best = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("web_search_result")
            and candidate.get("page_fetched")
            and candidate.get("url")
        ),
        None,
    )
    if web_best is None:
        web_best = next(
            (
                candidate
                for candidate in candidates
                if candidate.get("web_search_result") and candidate.get("url")
            ),
            None,
        )
    # A low-confidence academic lookalike must never be returned as ``best``
    # just because it happens to carry a URL: that URL can point at an
    # unrelated paper and the caller would merge it into the card. Prefer a
    # web/page discovery hit (marked unverified) or nothing at all.
    if academic_confident and best_academic:
        best = best_academic
    elif web_best:
        best = web_best
    else:
        best = None

    web_provider = None
    if web_best:
        web_provider = web_best.get("provider") or None
    elif web_used and web_candidates:
        web_provider = web_used[0]
    elif candidates and candidates[0].get("web_search_result"):
        web_provider = candidates[0].get("provider") or None

    result["verified_by_academic_sources"] = bool(academic_confident)
    result["web_search_fallback"] = {
        "attempted": not academic_confident,
        "configured": web_configured,
        "provider": web_provider,
        "providers": web_used,
        "result_count": len(web_candidates),
    }

    if best:
        result["best"] = best
        result["source"] = best.get("provider") or "unknown"
        result["url"] = best.get("url") or ""
        result["url_kind"] = best.get("url_kind") or "none"
        if best.get("web_search_result") or best.get("page_fetched"):
            result["warnings"].append(
                "best came from a web page/search hit; it is NOT verified by "
                "academic sources and needs manual confirmation before use"
            )
        elif not academic_confident and best_academic:
            result["warnings"].append(
                "best is a low-confidence academic lookalike; do not publish "
                "it as the confirmed paper"
            )
    else:
        result["error"] = "; ".join(errors) or "all sources returned no results"
        result["search_urls"] = _build_search_urls(_web_search_query(searchable, query))

    if result["best"] and not result["url"]:
        result["search_urls"] = _build_search_urls(_web_search_query(searchable, query))

    if errors:
        result["warnings"].extend(errors)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)

    sys.exit(0 if result["best"] else 1)


if __name__ == "__main__":
    main()
