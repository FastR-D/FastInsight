#!/usr/bin/env python3
"""Validate the current Research database roster or an explicit JSON snapshot."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastinsight.config import load_env
from roster_client import fetch_roster, validate_roster


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--roster-json", help="Optional offline JSON snapshot")
    args = parser.parse_args()
    try:
        if args.roster_json:
            with open(args.roster_json, encoding="utf-8") as source:
                roster = validate_roster(json.load(source))
        else:
            roster = fetch_roster(args.endpoint)["roster"]
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    warnings = []
    owners = []
    for direction in roster["directions"]:
        if not direction["keywords"]:
            warnings.append(f'{direction["name"]}: 尚未连接关键词')
        if not direction["owners"]:
            warnings.append(f'{direction["name"]}: 尚未连接成员，命中该方向时不会投递')
        owners.extend(direction["owners"])
    if roster.get("default_owner"):
        owners.append(roster["default_owner"])
    else:
        warnings.append("未设置默认接收人：没有关键词命中时不会投递")
    for owner in owners:
        if not owner.get("receiver_ref", "").strip():
            warnings.append(f'{owner["name"]}: 缺少 Research 收件账号（receiver_ref），不能投递')
    print("配置结构有效")
    for warning in dict.fromkeys(warnings):
        print("警告: " + warning)


if __name__ == "__main__":
    main()
