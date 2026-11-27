#!/usr/bin/env python3
"""One-time import of a legacy YAML/JSON roster into an empty Research database.

Uses an administrator session in FASTRESEARCH_ADMIN_SESSION. Runtime routing
never calls this utility or reads the legacy file. The source stays untouched.
"""
import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roster_client import MAX_RESPONSE_BYTES, request_json, research_endpoint, validate_snapshot


def read_legacy(path):
    with open(path, "rb") as source:
        raw = source.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("旧配置超过 1 MiB")
    text = raw.decode("utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
            value = yaml.safe_load(text)
        except ImportError:
            from mini_yaml import load
            value = load(text)
    if not isinstance(value, dict) or not isinstance(value.get("directions", []), list):
        raise ValueError("旧配置必须是包含 directions 的对象")
    value.setdefault("directions", [])
    return value


def import_roster(source, endpoint, session):
    if not session.strip():
        raise ValueError("请通过 FASTRESEARCH_ADMIN_SESSION 提供 Research 管理员会话")
    roster = read_legacy(source)
    endpoint = research_endpoint(endpoint)
    headers = {"Authorization": "Bearer " + session.strip()}
    path = "/api/admin/fastinsight/roster"
    current = validate_snapshot(request_json(endpoint, path, headers))
    existing = current["roster"]
    if any(existing.get(key) for key in ("directions", "members", "keywords", "default_owner")):
        raise ValueError("数据库已有分发配置，已停止导入；请在设置页合并修改")
    # The backend validates IDs, references, duplicates and the current revision.
    return validate_snapshot(request_json(endpoint, path, headers, method="PUT", payload={"roster": roster, "revision": current["revision"]}))


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fastinsight.config import load_env
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Legacy YAML or JSON file; read once and never modified")
    parser.add_argument("--endpoint", default=None)
    args = parser.parse_args()
    try:
        saved = import_roster(args.source, args.endpoint, os.environ.get("FASTRESEARCH_ADMIN_SESSION", ""))
    except (OSError, ValueError, urllib.error.URLError) as error:
        parser.error(str(error))
    print(json.dumps({"imported": True, "storage": saved["storage"], "revision": saved["revision"], "directions": len(saved["roster"]["directions"]), "members": len(saved["roster"]["members"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
