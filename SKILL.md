---
name: fastinsight
description: "Process a paper screenshot or link from Feishu: identify, verify, summarize, classify against the FastResearch database configuration, and publish to explicit Research recipient accounts. Also configure or validate routing through the Research settings page."
---

# FastInsight

## Purpose

Use Feishu as an input channel. Identify the paper, verify and enrich its metadata,
generate a trend summary, match the configured directions, publish into FastResearch,
and confirm successful delivery in the source conversation. Do not send owner cards
or direct messages. The standalone bot entry point is `feishu_bot.py`.

## Environment

1. Run `uv sync --locked` to create the project `.venv` using `.python-version`.
   Dependencies, including FastCAS, are declared in `pyproject.toml` and locked in `uv.lock`.
2. Configure `.env` from `.env.example`: Feishu application credentials, Research URL,
   and either a full FastCAS service identity or an explicitly enabled legacy ingest key.
3. Configure keywords, directions, members and the default receiver in the FastResearch
   routing settings page. Each recipient needs its explicit `receiver_ref` account ID.
4. Start with `./run.sh` or `uv run --locked python feishu_bot.py`. Do not start a second
   long connection for an already-running Feishu application.

## Operating workflow

### Identify and verify

Read the screenshot or link and identify title, authors, venue/year, DOI/arXiv ID,
and paper URL. Ask for clarification when multiple papers are plausible; do not
invent metadata. Treat news items as non-paper material when appropriate.

```bash
uv run --locked python scripts/verify_paper.py --query "Attention Is All You Need" --json-out paper.json
```

Use the `best` object when verification succeeds. Failed verification must not be
presented as confirmed identity. Preserve supplied metadata when a fetched page is unrelated.

### Summarize

```bash
uv run --locked python scripts/analyze_trends.py --paper paper.json --json-out trends.json
```

Prepare a substantive LLM trend summary before delivery. Do not send an incomplete
card with a missing URL or a placeholder trend. Citation statistics are supplementary.

### Preview routing

```bash
uv run --locked python scripts/route_paper.py --paper paper.json --trends trends.json --dry-run
```

The default source is `GET /api/fastinsight/roster`, authenticated with the same service
identity as publishing. It returns the current Research database snapshot and revision.
`--roster-json snapshot.json` is only for explicit offline previews; normal operation
never reads a YAML roster or `FASTINSIGHT_ROSTER`.

The router matches `title + abstract + venue` case-insensitively. ASCII keywords use
word boundaries. Every matched direction contributes its connected members; recipients
are deduplicated by `receiver_ref`, not by display name. Scores, priority and roster
order determine the primary direction. No keyword match goes directly to the default
receiver without semantic rerouting. Matched directions with no members do not create
an implicit default relationship. Missing default receivers mean no fallback delivery.

### Publish and confirm

```bash
uv run --locked python scripts/auto_pipeline.py --paper paper.json --trend-text "A substantive trend summary."
```

The pipeline reads one fresh database snapshot per paper and records `roster_revision`.
A failed configuration read stops delivery. Every selected member must have `receiver_ref`;
never guess identity from a name. Check `delivered` and `errors` before confirming success.
Only recipients acknowledged by successful Research receipts may be mentioned in the
source-chat confirmation. Partial delivery must be reported as partial delivery.

## Maintaining configuration

Use the Research settings page's three independent keyword/direction/member columns.
Connect nodes by dragging ports or edit relationships with the tag checkboxes in dialogs.
The default rule is visible in the graph. Save changes before expecting the next paper to
use them. `receiver_ref` is the stable Research account ID; Feishu Open ID is optional metadata.

```bash
uv run --locked python scripts/validate_roster.py
```

For a one-time legacy import into an empty database, provide an administrator session
via `FASTRESEARCH_ADMIN_SESSION`, then run:

```bash
uv run --locked python scripts/import_roster.py assets/colleagues.yaml
```

The importer preserves the source file, refuses to overwrite existing configuration,
and uses a revision check. Runtime service identity never grants configuration writes.
After importing, fill any missing recipient account references in the settings page.

## Resources

- `scripts/roster_client.py`: authenticated database snapshot reads.
- `scripts/import_roster.py`: explicit one-time YAML/JSON migration.
- `scripts/route_paper.py`: keyword/default routing and card previews.
- `scripts/auto_pipeline.py`: enrichment, routing and Research publication.
- `references/research_taxonomy.md`: starter directions and keywords.
- `references/academic_search.md`: academic verification and enrichment.
- `references/feishu_integration.md`: Feishu input and connection configuration.

## Validation

```bash
uv run --locked python -m unittest discover -s tests -v
```

Use isolated fixtures for tests. Never send live test messages or papers to real users
unless the user has requested that action.
