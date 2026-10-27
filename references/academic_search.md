# Academic Search Reference

`scripts/verify_paper.py` uses a three-stage strategy:

1. **Identifier-first lookups** for DOI / arXiv IDs. A registered DOI is often
   resolvable through Crossref even when search indexes (Semantic Scholar,
   OpenAlex) have not caught up with a newly published paper.
2. **Parallel title search** across Semantic Scholar, OpenAlex, arXiv, and
   Crossref. Results are merged, deduplicated by DOI/arXiv/normalized title, and
   ranked by title similarity. Crossref is preferred on exact-title ties because
   its DOI metadata is the authoritative registration record.
3. **Generic web-search fallback** (SerpAPI/Google, Bing, or Google
   Programmable Search). It runs when the four academic sources above return no
   candidate at all OR their best hit is a low-confidence lookalike
   (`match_score` below 0.6), so a paraphrased news headline can still reach the
   real paper/article page.

Web hits and explicit paper URLs are then enriched best-effort by fetching the
page and reading `citation_title`, `citation_author`, `citation_abstract`,
`citation_publication_date`, DOI/arXiv Open Graph `<meta>` tags and the `<title>`
tag. A fetched page carries `page_fetched: true` and can fill
title/authors/abstract/venue/year/URL even when the search indexes are stale.

WeChat official-account (`mp.weixin.qq.com`) and Xiaohongshu
(`xiaohongshu.com` / `xhslink.com`) links are handled one step earlier: FastInsight
reads the article body (`#js_content` / `__INITIAL_STATE__`, with a Jina Reader
fallback), extracts DOI / arXiv / publisher URLs, and only then runs the academic
lookup. If the article never cites a paper identifier, an optional LLM pass can
recover the paper title from the write-up.

Academic lookups require no API key. The generic web fallback is a discovery
aid, not academic verification: when it produces `best`, the JSON marks
`verified_by_academic_sources: false`. When no authoritative record is found,
the output includes `search_urls` (academic pages plus Google/Bing) so
downstream cards can offer one-click search links instead of an empty URL.

## 1. Identifier-first direct lookups

When the query contains a DOI, `verify_paper.py` tries, in order:

- `GET https://api.crossref.org/works/{doi}` — official DOI registration record.
- `GET https://api.openalex.org/works?filter=doi:{doi}` — OpenAlex record.
- `GET https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}` — S2 record
  with external identifiers and citations.

An arXiv ID (`2306.03331` or `arXiv:2306.03331`) is resolved directly through
`GET https://export.arxiv.org/api/query?id_list=...`.

Identifier-exact matches receive `match_score: 1.0` and always rank above fuzzy
title matches.

## 2. Semantic Scholar Graph API

- Endpoint: `GET https://api.semanticscholar.org/graph/v1/paper/search`
- Params: `query`, `limit`, `fields=title,year,abstract,authors,venue,externalIds,url,publicationDate`
- Returns: `data[]` with `title`, `year`, `abstract`, `authors[].name`,
  `venue`, `externalIds.{DOI,Arxiv}`, `url`.
- Rate limits: shared unauthenticated pool (~1 req/s, 429 when busy). An API key
  (`X-API-KEY` header via `S2_API_KEY`) raises limits.
- Best for: citation data and broad CS/ML matching when the paper is already
  indexed. Newly published works can lag by days or weeks.

## 3. OpenAlex

- Endpoint: `GET https://api.openalex.org/works`
- Title search: `filter=title.search:<query>`, `per-page=<n>`, optional
  `mailto`.
- Returns: `doi`, `display_name/title`, `authorships[].author.display_name`,
  `publication_date`, `publication_year`, `primary_location.landing_page_url`,
  `best_oa_location.pdf_url`, `abstract_inverted_index`, `cited_by_count`.
- No API key; setting `OPENALEX_MAILTO` is the polite-pool recommendation.
- Best for: a single aggregator that indexes many publisher DOIs plus arXiv and
  PubMed records, and a good source for freshly registered DOIs.
- Caveat: because OpenAlex aggregates many repositories, exact-title duplicates
  can appear. FastInsight therefore ranks Crossref/Semantic Scholar above it on
  exact-title ties and deduplicates by DOI/arXiv.

## 4. arXiv API (Atom XML)

- Endpoint: `GET https://export.arxiv.org/api/query`
- Title search params: `search_query=all:<query>`, `max_results`, `sortBy=relevance`
- ID lookup params: `id_list=<arxiv id>`
- Returns Atom `<entry>` elements with `<title>`, `<summary>`, `<author><name>`,
  `<id>` (the abs URL), `<published>`, and the optional `arxiv:doi` element.
- No key, no strict rate limit (be polite: ~1–3 req/s).
- Best for: same-day preprints and direct arXiv-ID resolution. It does not cover
  publisher-only publications.

## 5. Crossref REST API

- Endpoint: `GET https://api.crossref.org/works`
- Search params: `query.bibliographic=<query>`, `rows`, optional
  `select=DOI,title,author,issued,container-title,URL,link,resource,abstract`
  and `mailto`.
- Direct DOI lookup: `GET https://api.crossref.org/works/{doi}`
- Returns `message.items[]` with `title[0]`, `author[].{given,family}`,
  `issued.date-parts`, `container-title[0]`, `DOI`, plus the publisher landing
  URL in `URL`, full-text links in `link[]`, and `resource.primary.URL`.
- FastInsight prefers Crossref `URL`/`resource`/`link` over DOI-only URLs, and
  stores any PDF full-text link separately as `pdf_url`.
- No key; `CROSSREF_MAILTO` opts into the polite pool.
- Best for: published-version metadata and DOIs minutes-to-hours after
  registration, even before search engines re-index.

## 6. Generic web-search fallback (final layer)

When direct lookups and the four academic searches above return no candidates
or only low-confidence lookalikes, `verify_paper.py` runs a generic web search
before giving up. Hits are normalized into lightweight candidates with the page
title, URL, and search snippet, and are labeled with `web_search_result: true` plus
`verified_by_academic_sources: false`. A web hit is a discovery aid — treat it
as unverified and merge it with metadata read from the image/link instead of
replacing that metadata.

Providers (configure at least one):

- SerpAPI/Google: `SERPAPI_API_KEY`
  - `GET https://serpapi.com/search.json?engine=google&q=<query>&num=<n>&api_key=<key>`
  - Reads `organic_results[].{title,link,snippet}`.
- Bing Web Search v7: `BING_SEARCH_API_KEY`
  - `GET https://api.bing.microsoft.com/v7.0/search?q=<query>&count=<n>&responseFilter=Webpages`
  - Header: `Ocp-Apim-Subscription-Key: <key>`
  - Reads `webPages.value[].{name,url,snippet}`.
- Google Programmable Search: `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_CX`
  - `GET https://www.googleapis.com/customsearch/v1?key=<key>&cx=<cx>&q=<query>&num=<n>`
  - Reads `items[].{title,link,snippet}`.

Multiple configured providers run in parallel and are ranked together by title
similarity, deduplicated, and capped at the `--limit` requested. When no web
credential is configured, the fallback is skipped with an explanatory warning
and `search_urls` still includes Google/Bing one-click links.

## Canonical URL rules

After a record is found, `verify_paper.py` normalizes the link so a paper never
depends on whichever provider happened to return first:

- Publisher landing URL from Crossref/OpenAlex → kept as the primary `url`.
- DOI known, no landing URL → `https://doi.org/<doi>`.
- arXiv ID known, no DOI/landing URL → `https://arxiv.org/abs/<id>`.
- Semantic Scholar page used only when no DOI/arXiv/publisher link exists.
- Generic web-search fallback hits keep the page found by the web engine as
  `url_kind: web`; they never masquerade as publisher/DOI/arXiv records.

Each candidate also carries `url_kind` (`publisher`, `doi`, `arxiv`,
`semanticscholar`, `openalex`, `web`, or `none`) so downstream code can label
the link.

## Failure handling

- Provider failures are retained in `warnings`; a failing provider never stops
  the others.
- If a DOI lookup returns 404/400 (not yet in that provider), FastInsight simply
  moves to the next provider instead of treating it as a hard failure.
- If all academic sources fail (or only return weak lookalikes), the generic web
  search runs and its top hits are fetched for metadata. If that also produces
  nothing, `best` is `null`, `error` explains why, and `search_urls` provides
  clickable academic and general web search pages. In that case fall back to
  metadata read directly from the screenshot.
- A web-search `best` must keep `verified_by_academic_sources: false`; never
  publish it as if the paper were confirmed by academic sources, and do not let
  it replace screenshot-extracted metadata.

## Optional credentials

Set environment variables before running the scripts:

- `S2_API_KEY`: adds the `X-API-KEY` header for Semantic Scholar.
- `CROSSREF_MAILTO`: adds Crossref's polite-pool `mailto` parameter.
- `OPENALEX_MAILTO`: adds OpenAlex's polite-pool `mailto` parameter.
- `SERPAPI_API_KEY`: enables Google web search (via SerpAPI) when academic
  sources miss or only return low-confidence lookalikes.
- `BING_SEARCH_API_KEY`: enables Bing Web Search for the same fallback.
- `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_CX`: enables Google Programmable
  Search for the same fallback.

`verify_paper.py` auto-loads `FastInsight/.env` at startup, so the
providers configured above are active even when the caller forgets to source
the file first. Credentials are never written to output files.
