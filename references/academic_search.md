# Academic Search Reference

`scripts/verify_paper.py` queries three public APIs in order: **Semantic Scholar
→ arXiv → Crossref**. It ranks returned candidates by title similarity instead
of blindly selecting the first result. No API key is required for basic use.
This page documents each endpoint, what it returns, and rate-limit notes.

## 1. Semantic Scholar Graph API
- Endpoint: `GET https://api.semanticscholar.org/graph/v1/paper/search`
- Params:
  - `query` — free-text title/author query
  - `limit` — max results (1–100; default 5 here)
  - `fields` — comma-separated, e.g.
    `title,year,abstract,authors,venue,externalIds,url,publicationDate`
- Returns `data[]` with `title`, `year`, `abstract`, `authors[].name`,
  `venue`, `externalIds.{DOI,Arxiv}`, `url`.
- Rate limits: shared unauthenticated pool (~1 req/s, 429 when busy). An API key
  (`X-API-KEY` header) raises limits — optional, set via env if you add one.
- Best for: high-precision paper matching across all of CS/ML.

## 2. arXiv API (Atom XML)
- Endpoint: `GET https://export.arxiv.org/api/query`
- Params: `search_query=all:<query>`, `max_results`, `start`.
- Returns Atom `<entry>` elements: `<title>`, `<summary>` (abstract),
  `<author><name>`, `<id>` (the abs URL, doubles as link),
  `<published>` (yyyy).
- No key, no strict rate limit (be polite: ~1–3 req/s).
- Best for: recent preprints, especially when Semantic Scholar is throttled.

## 3. Crossref REST API
- Endpoint: `GET https://api.crossref.org/works`
- Params: `query.bibliographic=<query>`, `rows`.
- Returns `message.items[]` with `title[0]`, `author[].{given,family}`,
  `issued.date-parts`, `container-title[0]` (venue), `DOI`.
- No key; polite pool with `mailto` param recommended for higher limits.
- Best for: published venue papers / DOIs when the other two miss.

## Failure handling
- If a source raises (network down, 429/403, timeout), the script catches it and
  tries the next source. Provider failures are retained in `warnings`; if all
  sources fail, `best` is `null` and `error` explains why. In that case, fall
  back to the metadata read directly from the screenshot.
- To test offline behavior: `python scripts/verify_paper.py --query "test"` with
  no network returns `source: none`, a non-null `error`, and provider details in
  `warnings`.

## Optional credentials

Set environment variables before running the scripts:

- `S2_API_KEY`: adds the `X-API-KEY` header for Semantic Scholar.
- `CROSSREF_MAILTO`: adds Crossref's polite-pool `mailto` query parameter.

Then re-run the workflow as usual. Credentials are not written to output files.
