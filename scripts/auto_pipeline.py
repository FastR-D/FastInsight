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

from service_auth import service_configuration, service_token

ROOT = Path(__file__).resolve().parent
ROSTER_DEFAULT = ROOT.parent / "assets" / "colleagues.yaml"


def _text(value):
    return "" if value is None else str(value).strip()


def _run_json(command):
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
    try:
        return json.loads(completed.stdout), completed.returncode
    except json.JSONDecodeError:
        return {}, completed.returncode


def _publish(endpoint, ingest_key, person, paper, trends, direction, *, cas_config=None, receiver_ref=""):
    summary = _text(paper.get("abstract"))[:900]
    trend_summary = _text(trends.get("trend_summary"))
    if trend_summary:
        summary = f"{summary}\n\n趋势：{trend_summary}" if summary else trend_summary
    url = _text(paper.get("url"))
    if not url and _text(paper.get("doi")):
        url = f"https://doi.org/{_text(paper['doi'])}"
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
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + service_token(cas_config, endpoint)}
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


def process(paper, *, roster_path=None, endpoint=None, ingest_key=None):
    """Verify, enrich, route, and publish one paper. Returns an audit object."""
    if not isinstance(paper, dict):
        raise ValueError("paper must be an object")
    paper = dict(paper.get("best") if isinstance(paper.get("best"), dict) else paper)
    title = _text(paper.get("title"))
    if not title:
        title = _text(paper.get("text"))
        title = re.sub(r"https?://\S+", "", title).strip().splitlines()[0][:300] if title else "未命名论文"
        paper["title"] = title
    endpoint = endpoint or os.environ.get("FASTRESEARCH_URL", "http://127.0.0.1:8787")
    ingest_key = ingest_key if ingest_key is not None else os.environ.get("FASTINSIGHT_INGEST_KEY", "")
    cas_config = service_configuration(ingest_key=ingest_key)
    roster_path = str(roster_path or os.environ.get("FASTINSIGHT_ROSTER", ROSTER_DEFAULT))
    with tempfile.TemporaryDirectory(prefix="fastinsight-") as temp_dir:
        temp = Path(temp_dir)
        paper_path = temp / "paper.json"
        paper_path.write_text(json.dumps(paper, ensure_ascii=False), encoding="utf-8")
        verified = {}
        if title:
            verified_path = temp / "verified.json"
            verified, _ = _run_json([sys.executable, str(ROOT / "verify_paper.py"), "--query", title, "--json-out", str(verified_path)])
            if not verified and verified_path.exists():
                verified = json.loads(verified_path.read_text(encoding="utf-8"))
            if isinstance(verified.get("best"), dict):
                paper = verified["best"]
                paper_path.write_text(json.dumps(paper, ensure_ascii=False), encoding="utf-8")
        trends_path = temp / "trends.json"
        trends, _ = _run_json([sys.executable, str(ROOT / "analyze_trends.py"), "--paper", str(paper_path), "--json-out", str(trends_path)])
        if not trends and trends_path.exists():
            trends = json.loads(trends_path.read_text(encoding="utf-8"))
        route, route_code = _run_json([sys.executable, str(ROOT / "route_paper.py"), "--paper", str(paper_path), "--roster", roster_path, "--trends", str(trends_path), "--dry-run"])
    direction = _text(route.get("matched_direction"))
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
            delivered.append(_publish(endpoint, ingest_key, person, paper, trends, direction,
                                      cas_config=cas_config, receiver_ref=receiver_ref))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"{person}: {exc}")
    return {"paper": paper, "route": route, "route_code": route_code, "delivered": delivered, "errors": errors}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Automatically process and publish a FastInsight paper")
    parser.add_argument("--paper", required=True, help="JSON paper object or file")
    parser.add_argument("--roster", default=str(ROSTER_DEFAULT))
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--ingest-key", default=None)
    args = parser.parse_args()
    source = Path(args.paper)
    paper = json.loads(source.read_text(encoding="utf-8")) if source.exists() else json.loads(args.paper)
    print(json.dumps(process(paper, roster_path=args.roster, endpoint=args.endpoint, ingest_key=args.ingest_key), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
