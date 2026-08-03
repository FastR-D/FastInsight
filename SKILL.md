---
name: fastinsight
description: "Use FastInsight to process a paper, poster, or research-news screenshot or link from a Feishu/Lark group: extract metadata, verify it with academic sources, summarize citation and related-work signals, classify it against colleagues.yaml, generate a safe interactive card, and route it to valid Feishu open_id owners. Also use it to configure or validate the research-direction roster. Trigger for paper screenshots, article links, 飞书机器人自动分拣论文, research-news triage, or requests to forward a paper to the right teammate."
---

# FastInsight

## Purpose

Use WorkBuddy as the Feishu/Lark bot for a complete paper-triage loop:

`image or link → identify → verify → summarize → classify → preview → forward → confirm`

WorkBuddy itself is the bot connected through the Feishu connector; no separate
server is required.

## Prerequisites

1. Connect the Feishu connector so WorkBuddy can receive images and send cards.
2. Copy `assets/colleagues.yaml.example` to `colleagues.yaml`, fill in real
   `feishu_open_id` values, and run:
   `python scripts/validate_roster.py colleagues.yaml`.
3. Provide outbound network access only when academic verification or trend
   enrichment is needed. Basic lookups use Semantic Scholar, arXiv, and Crossref
   without API keys; see `references/academic_search.md`.

## Operating workflow

### 1. Extract the source

- Read the screenshot and caption multimodally. Extract the title, authors,
  venue/year, DOI/arXiv ID, URL, and any topic supplied by the sender.
- If the title or link is ambiguous, ask the sender in the group. Do not invent
  metadata or route an unreadable image.
- Treat a news or industry item as a non-paper item: use its headline, caption,
  and URL; skip academic verification and citation analysis unless requested.

### 2. Verify and enrich

For papers, run:

```bash
python scripts/verify_paper.py --query "Attention Is All You Need" --json-out paper.json
```

Use the `best` object when verification succeeds. If all APIs fail, retain the
metadata extracted from the image and clearly mark verification as unavailable.
Never present an API fallback as confirmed identity.

### 3. Add optional insight

For papers, run:

```bash
python scripts/analyze_trends.py --paper paper.json --json-out trends.json
```

Treat trends as supplementary context. If the API is unavailable, keep the
route pipeline running and state that citation data needs manual review.

### 4. Classify and preview

Run the roster-driven route in dry-run mode first:

```bash
python scripts/route_paper.py --paper paper.json --roster colleagues.yaml --trends trends.json --dry-run
```

The router matches normalized `title + abstract + venue` text against each
direction's keywords. ASCII keywords use token boundaries, while Chinese or
mixed-language phrases use case-insensitive phrase matching. Higher scores win;
`priority` breaks ties when configured, then roster order does.

Only real, non-placeholder Feishu IDs are routable. A missing match or invalid
direction owner falls back to a valid `default_owner` when available. Inspect
`matched_direction`, `hits`, `fallback`, `owners`, and `routable` before sending.

### 5. Forward and confirm

When `routable` is true, send the generated interactive card to every returned
owner through the connector. For a persisted payload, omit `--dry-run`; the
default output is `feishu_card_payload.json`.

Then confirm in the source group, for example:

`✅ 已将《标题》分拣至【方向】，转发给 @张三、@李四。`

Include the short trend summary when available. Mention fallback routing so the
sender can correct the classification.

## Roster contract

Use this shape:

```yaml
directions:
  - name: "方向名称"
    priority: 10
    keywords: ["distinctive phrase", "中文关键词"]
    owners:
      - name: "同事"
        feishu_open_id: "ou_real_id"
default_owner:
  name: "组长"
  feishu_open_id: "ou_real_id"
```

Keep keywords distinctive and maintain the roster with
`python scripts/validate_roster.py colleagues.yaml` after every edit. See
`references/research_taxonomy.md` for starter directions.

## Resources

- `scripts/verify_paper.py`: verify and enrich a paper through public APIs.
- `scripts/analyze_trends.py`: collect citation, field, and related-work signals.
- `scripts/route_paper.py`: classify, validate owners, and render the card.
- `scripts/validate_roster.py`: lint roster structure, IDs, duplicates, and
  placeholders.
- `scripts/mini_yaml.py`: dependency-free fallback parser for the roster subset.
- `references/feishu_integration.md`: connector and raw API send/receive notes.
- `references/research_taxonomy.md`: starter research directions and keywords.
- `references/academic_search.md`: endpoints, optional credentials, and fallback
  behavior.
- `assets/colleagues.yaml.example`: roster template.
- `assets/feishu_card.json`: interactive card template.

## Guardrails

- Never send to a missing, placeholder, or unvalidated `feishu_open_id`.
- Ask for clarification when the image contains multiple plausible papers.
- Do not claim a paper is verified when APIs failed or the match is uncertain.
- Forward paper metadata and link only; omit sender identity unless required.
- Preview every newly configured route before enabling live forwarding.
