import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.route_paper import build_route_decision

ROOT = Path(__file__).resolve().parents[1]
A = {"id": "a", "name": "同名成员", "receiver_ref": "account-a"}
B = {"id": "b", "name": "同名成员", "receiver_ref": "account-b"}
DEFAULT = {"id": "c", "name": "默认成员", "receiver_ref": "account-c"}


class RoutePaperTests(unittest.TestCase):
    def roster(self):
        return {"directions": [
            {"name": "IoT", "keywords": ["IoT", "firmware"], "owners": [A]},
            {"name": "Patch", "keywords": ["patch correctness"], "owners": [A, B]},
            {"name": "Agent", "keywords": ["prompt injection"], "owners": [B]},
        ], "default_owner": DEFAULT}

    def test_all_matching_directions_merge_and_deduplicate_by_receiver(self):
        result = build_route_decision(self.roster(), "iot firmware patch correctness")
        self.assertEqual([d["name"] for d in result["matched_directions"]], ["IoT", "Patch"])
        self.assertEqual([m["receiver_ref"] for m in result["owners"]], ["account-a", "account-b"])
        self.assertEqual([m["id"] for m in result["owners"]], ["a", "b"])
        self.assertFalse(result["fallback"])
        self.assertEqual(result["routing_mode"], "keyword")

    def test_no_match_routes_directly_to_default(self):
        result = build_route_decision(self.roster(), "a broad study of safety risks")
        self.assertEqual(result["routing_mode"], "default")
        self.assertEqual(result["owners"], [DEFAULT])
        self.assertEqual(result["fallback_reason"], "no_keyword_match")
        self.assertEqual(result["matched_directions"], [])

    def test_unconnected_matching_direction_does_not_add_default_edge(self):
        roster = self.roster()
        roster["directions"][0]["owners"] = []
        result = build_route_decision(roster, "firmware")
        self.assertFalse(result["routable"])
        self.assertFalse(result["fallback"])
        self.assertEqual(result["owners"], [])
        self.assertEqual(result["fallback_reason"], "matched_direction_has_no_valid_owner")

    def test_empty_and_default_only_rosters(self):
        for roster, expected in [({"directions": []}, []), ({"directions": [], "default_owner": DEFAULT}, [DEFAULT])]:
            with self.subTest(roster=roster):
                result = build_route_decision(roster, "anything")
                self.assertEqual(result["owners"], expected)
                self.assertEqual(result["routable"], bool(expected))

    def test_short_keywords_require_token_boundaries(self):
        roster = {"directions": [{"name": "RL", "keywords": ["RL"], "owners": [A]}], "default_owner": DEFAULT}
        self.assertEqual(build_route_decision(roster, "url parsing")["owners"], [DEFAULT])
        self.assertEqual(build_route_decision(roster, "rl for agents")["owners"], [A])

    def test_offline_cli_reads_only_explicit_json(self):
        with tempfile.TemporaryDirectory(prefix="fastinsight-route-") as directory:
            temp = Path(directory)
            roster_path, paper_path = temp / "roster.json", temp / "paper.json"
            roster_path.write_text(json.dumps(self.roster()), encoding="utf-8")
            paper_path.write_text(json.dumps({"title": "Firmware patch correctness for IoT", "url": "https://example.test/paper"}), encoding="utf-8")
            completed = subprocess.run([sys.executable, str(ROOT / "scripts/route_paper.py"), "--paper", str(paper_path), "--roster-json", str(roster_path), "--trend-text", "趋势分析", "--dry-run"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual([m["receiver_ref"] for m in result["owners"]], ["account-a", "account-b"])


if __name__ == "__main__":
    unittest.main()
