import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.mini_yaml import load as load_yaml
from scripts.route_paper import build_route_decision


ROOT = Path(__file__).resolve().parents[1]


class RoutePaperTests(unittest.TestCase):
    def route(self, roster, paper):
        with tempfile.TemporaryDirectory(prefix="fastinsight-route-") as temp_dir:
            temp = Path(temp_dir)
            roster_path = temp / "roster.yaml"
            paper_path = temp / "paper.json"
            roster_path.write_text(roster, encoding="utf-8")
            paper_path.write_text(json.dumps(paper), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "route_paper.py"),
                    "--paper",
                    str(paper_path),
                    "--roster",
                    str(roster_path),
                    "--trend-text",
                    "趋势分析",
                    "--dry-run",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_all_matching_directions_are_merged_and_owners_deduplicated(self):
        roster = """
directions:
  - name: "IoT"
    keywords: ["IoT", "firmware"]
    owners:
      - name: "何艺"
  - name: "Patch"
    keywords: ["patch correctness"]
    owners:
      - name: "何艺"
      - name: "谢子洋"
  - name: "Agent"
    keywords: ["prompt injection"]
    owners:
      - name: "温江涛"
default_owner:
  name: "王佳瑶"
"""
        result = self.route(
            roster,
            {
                "title": "Firmware patch correctness for IoT",
                "abstract": "IoT firmware and patch correctness",
                "url": "https://example.com/paper",
            },
        )

        self.assertEqual(
            [item["name"] for item in result["matched_directions"]],
            ["IoT", "Patch"],
        )
        self.assertEqual(
            [owner["name"] for owner in result["owners"]],
            ["何艺", "谢子洋"],
        )
        self.assertFalse(result["fallback"])


class SemanticRouteTests(unittest.TestCase):
    roster = load_yaml("""
directions:
  - name: "IoT，Patch生成，Patch正确性"
    keywords:
      - "IoT"
      - "firmware"
      - "patch correctness"
    owners:
      - name: "何艺"
  - name: "视频审核/理解 安全生态顶会"
    keywords:
      - "Harmful Video"
      - "Video Understanding"
    owners:
      - name: "王佳瑶"
default_owner:
  name: "何艺"
""")
    paper = {
        "title": "Safety Evaluation of Generative Assistants",
        "abstract": "A broad study of safety risks in generative assistants.",
        "url": "https://example.com/paper",
    }

    def test_confident_semantic_match_selects_research_owner(self):
        result = build_route_decision(
            self.roster,
            "safety evaluation of generative assistants",
            self.paper,
            semantic_result={
                "selected": [
                    {
                        "name": "王佳瑶",
                        "directions": ["视频审核/理解 安全生态顶会"],
                        "reason": "安全评测属于该研究方向",
                    }
                ],
                "confidence": 0.92,
                "reason": "明确匹配安全评测方向",
            },
        )

        self.assertEqual(result["routing_mode"], "semantic")
        self.assertEqual([owner["name"] for owner in result["owners"]], ["王佳瑶"])
        self.assertEqual(result["matched_direction"], "视频审核/理解 安全生态顶会")
        self.assertEqual(
            result["fallback_reason"],
            "no_keyword_match_semantic_route",
        )

    def test_low_confidence_semantic_match_uses_default_owner(self):
        result = build_route_decision(
            self.roster,
            "safety evaluation of generative assistants",
            self.paper,
            semantic_result={
                "selected": [{"name": "王佳瑶"}],
                "confidence": 0.6,
                "reason": "方向较宽泛",
            },
        )

        self.assertEqual(result["routing_mode"], "default")
        self.assertEqual([owner["name"] for owner in result["owners"]], ["何艺"])
        self.assertEqual(
            result["fallback_reason"],
            "semantic_confidence_below_threshold",
        )

    def test_semantic_failure_uses_default_owner(self):
        with patch(
            "scripts.route_paper._semantic_route_paper",
            side_effect=RuntimeError("LLM unavailable"),
        ):
            result = build_route_decision(
                self.roster,
                "safety evaluation of generative assistants",
                self.paper,
            )

        self.assertEqual(result["routing_mode"], "default")
        self.assertEqual([owner["name"] for owner in result["owners"]], ["何艺"])
        self.assertEqual(result["fallback_reason"], "semantic_route_unavailable")

    def test_keyword_match_does_not_call_semantic_router(self):
        with patch(
            "scripts.route_paper._semantic_route_paper",
            side_effect=AssertionError("semantic router should not run"),
        ):
            result = build_route_decision(
                self.roster,
                "iot firmware patch correctness",
                self.paper,
            )

        self.assertEqual(result["routing_mode"], "keyword")
        self.assertEqual([owner["name"] for owner in result["owners"]], ["何艺"])


if __name__ == "__main__":
    unittest.main()
