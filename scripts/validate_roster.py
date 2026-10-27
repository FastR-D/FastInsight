#!/usr/bin/env python3
"""Validate the colleagues.yaml roster used by FastInsight.

Checks structure, required names, duplicate direction names, duplicate
keywords, and duplicate owners within a direction. Missing Feishu open_id
values are warnings because FastInsight publishes by person name to
FastResearch and no longer forwards Feishu cards internally.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load_roster(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        import yaml

        return yaml.safe_load(text)
    except ImportError:
        from mini_yaml import load

        return load(text)


def _text(value):
    return str(value).strip() if value is not None else ""


def _is_placeholder(value):
    normalized = _text(value).casefold()
    return (
        not normalized
        or normalized.startswith(("ou_xxxx", "<", "{{", "your_", "replace_"))
        or "xxxxxxxx" in normalized
    )


def _validate_owner(owner, tag, errors, warnings, seen_ids=None):
    if not isinstance(owner, dict):
        errors.append(f"{tag}: 必须是 mapping")
        return
    name = _text(owner.get("name"))
    open_id = _text(owner.get("feishu_open_id"))
    if not name:
        errors.append(f"{tag}: 缺少 name")
    if _is_placeholder(open_id):
        warnings.append(
            f"{tag} ({name or '未命名'}): feishu_open_id 缺失或为占位符；"
            "FastResearch 仅按姓名投递，可忽略"
        )
    elif seen_ids is not None:
        if open_id in seen_ids:
            seen_ids[open_id] += 1
        else:
            seen_ids[open_id] = 1


def main():
    parser = argparse.ArgumentParser(
        description="Validate a FastInsight colleagues.yaml roster."
    )
    parser.add_argument("roster")
    args = parser.parse_args()

    if not os.path.exists(args.roster):
        print(f"ERROR: file not found: {args.roster}")
        sys.exit(1)

    errors = []
    warnings = []
    try:
        data = load_roster(args.roster)
    except Exception as exc:  # noqa: BLE001 - present a CLI-friendly diagnostic
        print(f"❌ ERROR: 无法解析 roster: {exc}")
        sys.exit(1)

    if not isinstance(data, dict):
        print("❌ ERROR: 顶层必须是 mapping（包含 directions / default_owner）")
        sys.exit(1)

    directions = data.get("directions")
    if not isinstance(directions, list) or not directions:
        errors.append("缺少 directions 列表，或为空")
    else:
        direction_names = {}
        for index, direction in enumerate(directions):
            tag = f"directions[{index}]"
            if not isinstance(direction, dict):
                errors.append(f"{tag}: 必须是 mapping")
                continue

            name = _text(direction.get("name"))
            if not name:
                errors.append(f"{tag}: 缺少 name")
            elif name in direction_names:
                errors.append(
                    f"{tag}: name 与 directions[{direction_names[name]}] 重复：{name}"
                )
            else:
                direction_names[name] = index

            keywords = direction.get("keywords")
            if not isinstance(keywords, list) or not keywords:
                warnings.append(
                    f"{tag} ({name or '未命名'}): 没有配置 keywords，将无法自动匹配"
                )
            else:
                normalized_keywords = set()
                for keyword_index, keyword in enumerate(keywords):
                    keyword_text = _text(keyword)
                    if not keyword_text:
                        errors.append(f"{tag}.keywords[{keyword_index}]: 不能为空")
                        continue
                    normalized = keyword_text.casefold()
                    if normalized in normalized_keywords:
                        warnings.append(
                            f"{tag} ({name or '未命名'}): keyword 重复：{keyword_text}"
                        )
                    normalized_keywords.add(normalized)

            owners = direction.get("owners")
            if not isinstance(owners, list) or not owners:
                warnings.append(
                    f"{tag} ({name or '未命名'}): 没有配置 owners，将尝试 default_owner"
                )
            else:
                seen_direction_ids = {}
                for owner_index, owner in enumerate(owners):
                    _validate_owner(
                        owner,
                        f"{tag}.owners[{owner_index}]",
                        errors,
                        warnings,
                        seen_direction_ids,
                    )
                for open_id, count in seen_direction_ids.items():
                    if count > 1:
                        warnings.append(
                            f"{tag} ({name or '未命名'}): 同一 open_id 重复 {count} 次：{open_id}"
                        )

    default_owner = data.get("default_owner")
    if default_owner is None:
        warnings.append("未配置 default_owner：没有方向命中时将无法自动兜底")
    else:
        _validate_owner(default_owner, "default_owner", errors, warnings)

    if errors:
        print("❌ ERRORS:")
        for error in errors:
            print("  -", error)
    if warnings:
        print("⚠️  WARNINGS:")
        for warning in warnings:
            print("  -", warning)
    if not errors and not warnings:
        print("✅ roster 校验通过")

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
