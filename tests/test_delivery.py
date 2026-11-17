import json
import subprocess
import sys
import unittest
from pathlib import Path

from fastinsight.pipeline import confirmation_text, process_paper
from tests.http_world import HTTPWorld


PAPER = {"title": "Known firmware paper", "url": "https://doi.org/10.1000/known",
         "doi": "10.1000/known", "abstract": "Original firmware abstract"}


class DeliveryScenarios(unittest.TestCase):
    def test_unrelated_fetched_page_preserves_supplied_paper_through_publication(self):
        with HTTPWorld() as world:
            result = process_paper(PAPER)
            self.assertTrue(any("papers.example" in url for url in world.requests))
            self.assertEqual(result["paper"]["title"], PAPER["title"])
            self.assertEqual(result["paper"]["doi"], PAPER["doi"])
            self.assertEqual(result["paper"]["abstract"], PAPER["abstract"])
            self.assertEqual(len(world.published), 2)
            for item in world.published:
                self.assertEqual(item["item"]["url"], PAPER["url"])
                self.assertEqual(item["item"]["title"], PAPER["title"])

    def test_accepted_page_can_still_enrich_a_paper(self):
        with HTTPWorld() as world:
            world.title = PAPER["title"]
            result = process_paper({"title": PAPER["title"]})
            self.assertEqual(result["paper"]["url"], "https://papers.example/discovered")
            self.assertEqual(len(result["delivered"]), 2)

    def test_partial_success_names_only_confirmed_receivers(self):
        with HTTPWorld() as world:
            world.receipts["B"] = {"ok": False, "deliveredTo": []}
            result = process_paper(PAPER)
            self.assertEqual(len(result["delivered"]), 1)
            self.assertEqual(len(result["errors"]), 1)
            text = confirmation_text(result)
            self.assertIn("@A", text)
            self.assertNotIn("@B", text)

    def test_malformed_2xx_receipts_do_not_count_as_success(self):
        bad = [[], None, {"ok": False, "deliveredTo": ["A"]},
               {"ok": 1, "deliveredTo": ["A"]}, {"ok": True},
               {"ok": True, "deliveredTo": []}, {"ok": True, "deliveredTo": "A"},
               {"ok": True, "deliveredTo": [""]}, {"ok": True, "deliveredTo": [{}]}]
        with HTTPWorld() as world:
            for receipt in bad:
                with self.subTest(receipt=receipt):
                    world.receipts = {"A": receipt, "B": receipt}
                    result = process_paper(PAPER)
                    self.assertEqual(result["delivered"], [])
                    self.assertEqual(len(result["errors"]), 2)
                    self.assertNotIn("✅", confirmation_text(result))

    def test_cli_uses_same_receipt_contract(self):
        with HTTPWorld() as world:
            paper = world.root / "paper.json"
            paper.write_text(json.dumps(PAPER), encoding="utf-8")
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "publish_to_research.py"),
                "--paper", str(paper), "--person", "A", "--trend-text", "A fixture trend."]
            world.receipts["A"] = {"ok": False, "deliveredTo": []}
            rejected = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(rejected.returncode, 0)
            world.receipts["A"] = {"ok": True, "deliveredTo": ["A"]}
            accepted = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(json.loads(accepted.stdout)["deliveredTo"], ["A"])
