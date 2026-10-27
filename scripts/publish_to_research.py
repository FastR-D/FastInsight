#!/usr/bin/env python3
"""Publish a FastInsight result to a FastResearch personal-key workspace."""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from text_clip import compose_research_summary, normalize_ws
from service_auth import service_configuration, service_token


def normalize(value):
    return normalize_ws(value)


def main():
    parser = argparse.ArgumentParser(description="Publish FastInsight data to FastResearch")
    parser.add_argument("--paper", required=True, help="paper JSON or verify_paper output")
    parser.add_argument("--trends", help="optional trends JSON")
    parser.add_argument("--trend-text", help="LLM trend summary text (used instead of --trends)")
    parser.add_argument("--person", default="", help="legacy FastResearch member name")
    parser.add_argument("--receiver-ref", default="", help="stable FastResearch account ID for FastCAS service delivery")
    parser.add_argument("--fastcas-issuer", default=os.environ.get("FASTINSIGHT_FASTCAS_ISSUER", ""))
    parser.add_argument("--fastcas-client-id", default=os.environ.get("FASTINSIGHT_FASTCAS_CLIENT_ID", ""))
    parser.add_argument("--endpoint", default=os.environ.get("FASTRESEARCH_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--ingest-key", default=os.environ.get("FASTINSIGHT_INGEST_KEY", ""))
    parser.add_argument("--direction", default="")
    args = parser.parse_args()
    with open(args.paper, "r", encoding="utf-8") as handle:
        paper = json.load(handle)
    if isinstance(paper, dict) and isinstance(paper.get("best"), dict):
        paper = paper["best"]
    if not isinstance(paper, dict):
        parser.error("--paper must contain a JSON object or a best object")
    trends = {}
    if args.trend_text:
        trends["trend_summary"] = args.trend_text
    elif args.trends:
        with open(args.trends, "r", encoding="utf-8") as handle:
            trends = json.load(handle)
    title = normalize(paper.get("title")) or "未命名论文"
    trend_summary = normalize(trends.get("trend_summary"))
    summary = compose_research_summary(paper.get("abstract"), trend_summary)
    url = normalize(paper.get("url"))
    if not url and normalize(paper.get("doi")):
        url = f"https://doi.org/{normalize(paper['doi'])}"
    if not url and normalize(paper.get("arxiv_id")):
        arxiv_id = re.sub(
            r"^arXiv:", "", normalize(paper["arxiv_id"]), flags=re.IGNORECASE
        )
        if arxiv_id:
            url = (
                arxiv_id
                if arxiv_id.startswith(("http://", "https://"))
                else f"https://arxiv.org/abs/{arxiv_id}"
            )
    if not url:
        parser.error(
            "paper has no usable URL; resolve it with verify_paper.py before "
            "publishing (FastInsight cards must never carry an empty link)"
        )
    if not trend_summary:
        parser.error(
            "missing LLM trend summary; pass --trend-text or --trends with "
            "trend_summary before publishing"
        )
    item = {
        "id": normalize(paper.get("doi")) or url or title,
        "title": title,
        "summary": summary,
        "url": url,
        "direction": args.direction,
        "source": "FastInsight",
    }
    try:
        cas_config = service_configuration(
            args.fastcas_issuer, args.fastcas_client_id, args.ingest_key
        )
    except ValueError as exc:
        parser.error(str(exc))
    headers = {"Content-Type": "application/json"}
    if cas_config:
        if not args.receiver_ref or args.person:
            parser.error("FastCAS 服务投递需要 --receiver-ref，不能按姓名广播")
        try:
            token = service_token(cas_config, args.endpoint)
        except ValueError as exc:
            parser.error(str(exc))
        headers["Authorization"] = "Bearer " + token
        payload = {"receiver_ref": args.receiver_ref, "item": item}
    else:
        if not args.ingest_key or not args.person or args.receiver_ref:
            parser.error("原 ingest key 模式需要 --person 和 --ingest-key，不能提供 --receiver-ref")
        headers["X-FastInsight-Key"] = args.ingest_key
        payload = {"person": args.person, "item": item}
    endpoint = args.endpoint.rstrip("/") + "/api/insight/publish"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raise SystemExit(
            f"FastResearch 发布失败 ({error.code}): {error.read().decode('utf-8', 'replace')}"
        )


if __name__ == "__main__":
    main()
