#!/usr/bin/env python3
"""Publish a FastInsight result to a FastResearch personal-key workspace."""
import argparse
import json
import os
import urllib.error
import urllib.request


def normalize(value):
    return "" if value is None else str(value).strip()


def main():
    parser = argparse.ArgumentParser(description="Publish FastInsight data to FastResearch")
    parser.add_argument("--paper", required=True, help="paper JSON or verify_paper output")
    parser.add_argument("--trends", help="optional trends JSON")
    parser.add_argument("--person", required=True, help="FastResearch member name")
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
    if args.trends:
        with open(args.trends, "r", encoding="utf-8") as handle:
            trends = json.load(handle)
    title = normalize(paper.get("title")) or "未命名论文"
    summary = normalize(paper.get("abstract"))[:900]
    trend_summary = normalize(trends.get("trend_summary"))
    if trend_summary:
        summary = f"{summary}\n\n趋势：{trend_summary}" if summary else trend_summary
    url = normalize(paper.get("url"))
    if not url and normalize(paper.get("doi")):
        url = f"https://doi.org/{normalize(paper['doi'])}"
    payload = {"person": args.person, "item": {"id": normalize(paper.get("doi")) or url or title, "title": title, "summary": summary, "url": url, "direction": args.direction, "source": "FastInsight"}}
    if not args.ingest_key:
        parser.error("请提供 --ingest-key 或 FASTINSIGHT_INGEST_KEY")
    endpoint = args.endpoint.rstrip("/") + "/api/insight/publish"
    request = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "X-FastInsight-Key": args.ingest_key}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raise SystemExit(f"FastResearch 发布失败 ({error.code}): {error.read().decode('utf-8', 'replace')}")


if __name__ == "__main__":
    main()
