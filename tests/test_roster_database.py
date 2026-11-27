import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts.auto_pipeline import process
from scripts.roster_client import fetch_roster, request_json, research_endpoint, validate_snapshot
from scripts.import_roster import import_roster
from tests.http_world import HTTPWorld

ROOT = Path(__file__).resolve().parents[1]
PAPER = {"title": "Firmware safety", "url": "https://example.test/paper", "abstract": "Firmware study"}


class DatabaseRosterTests(unittest.TestCase):
    def test_pipeline_reads_fresh_snapshot_once_per_paper(self):
        with HTTPWorld() as world:
            result = process(PAPER, trend_text="A trend.", skip_verify=True)
            self.assertEqual(result["roster_revision"], "revision-1")
            self.assertEqual(world.roster_reads, 1)
            self.assertEqual([p["receiver_ref"] for p in world.published], ["account-a", "account-b"])
            world.roster["directions"][0]["owners"] = [world.roster["default_owner"]]
            world.roster_revision = "revision-2"
            world.published.clear()
            second = process(PAPER, trend_text="A trend.", skip_verify=True)
            self.assertEqual(second["roster_revision"], "revision-2")
            self.assertEqual(world.roster_reads, 2)
            self.assertEqual([p["receiver_ref"] for p in world.published], ["account-a"])

    def test_config_failure_stops_before_enrichment_or_publish(self):
        with HTTPWorld() as world:
            for status in (401, 403, 404, 500):
                with self.subTest(status=status):
                    world.roster_status = status
                    with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                        process(PAPER, trend_text="A trend.")
            self.assertEqual(world.published, [])
            self.assertTrue(all(url.endswith("/api/fastinsight/roster") for url in world.requests))

    def test_no_keyword_match_uses_default_without_semantic_request(self):
        with HTTPWorld() as world:
            result = process({**PAPER, "title": "Astronomy", "abstract": "Stars"}, trend_text="A trend.", skip_verify=True)
            self.assertEqual(result["route"]["routing_mode"], "default")
            self.assertEqual([p["receiver_ref"] for p in world.published], ["account-a"])
            self.assertFalse(any("chat/completions" in url for url in world.requests))

    def test_missing_receiver_fails_instead_of_matching_by_name(self):
        with HTTPWorld() as world:
            world.roster["directions"][0]["owners"] = [{"id": "unbound", "name": "A"}]
            result = process(PAPER, trend_text="A trend.", skip_verify=True)
            self.assertEqual(world.published, [])
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("receiver_ref", result["errors"][0])

    def test_cli_defaults_to_database_even_with_obsolete_roster_env(self):
        with HTTPWorld() as world:
            paper = world.root / "paper.json"
            paper.write_text(json.dumps(PAPER), encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/route_paper.py"), "--paper", str(paper), "--trend-text", "A trend.", "--dry-run"], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["roster_revision"], "revision-1")
            self.assertEqual(world.roster_reads, 1)

    def test_service_identity_is_used_for_reading(self):
        with patch.dict(os.environ, {"FASTINSIGHT_FASTCAS_ISSUER": "https://cas.test", "FASTINSIGHT_FASTCAS_CLIENT_ID": "insight", "FASTINSIGHT_FASTCAS_CLIENT_SECRET": "secret", "FASTINSIGHT_INGEST_KEY": ""}), patch("scripts.roster_client.service_token", return_value="service-token"), patch("scripts.roster_client.request_json", return_value={"storage": "database", "revision": "v1", "roster": {"directions": []}}) as request:
            fetch_roster("https://research.test")
            self.assertEqual(request.call_args.args[2], {"Authorization": "Bearer service-token"})

    def test_invalid_snapshots_do_not_become_empty_rosters(self):
        for value in [None, {}, {"storage": "file", "revision": "v1", "roster": {"directions": []}}, {"storage": "database", "revision": "v1", "roster": {"directions": [{"name": "A", "keywords": [None], "owners": []}]}}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_snapshot(value)

    def test_unsafe_endpoints_are_rejected_before_sending_credentials(self):
        for endpoint in ["http://remote.test", "https://user:pass@research.test", "https://research.test?q=1", "https://research.test/#fragment"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                research_endpoint(endpoint)


class LegacyImportTests(unittest.TestCase):
    def test_import_preserves_source_and_uses_revision_without_overwrite(self):
        initial = {"storage": "database", "revision": "v1", "roster": {"directions": []}}
        saved = {"storage": "database", "revision": "v2", "roster": {"directions": [], "default_owner": {"name": "成员", "receiver_ref": "account-a"}}}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "legacy.yaml"
            raw = 'directions: []\ndefault_owner:\n  name: "成员"\n  receiver_ref: "account-a"\n'
            source.write_text(raw, encoding="utf-8")
            with patch("scripts.import_roster.request_json", side_effect=[initial, saved]) as request:
                result = import_roster(source, "https://research.test", "admin-session")
                self.assertEqual(result["revision"], "v2")
                self.assertEqual(request.call_args.kwargs["payload"]["revision"], "v1")
                self.assertEqual(request.call_args.kwargs["payload"]["roster"]["default_owner"]["receiver_ref"], "account-a")
            self.assertEqual(source.read_text(encoding="utf-8"), raw)
            with patch("scripts.import_roster.request_json", return_value=saved) as request:
                with self.assertRaisesRegex(ValueError, "已有分发配置"):
                    import_roster(source, "https://research.test", "admin-session")
                self.assertEqual(request.call_count, 1)
            with patch("scripts.import_roster.request_json", side_effect=[initial, urllib.error.HTTPError("https://research.test", 409, "conflict", {}, None)]) as request:
                with self.assertRaises(urllib.error.HTTPError):
                    import_roster(source, "https://research.test", "admin-session")
                self.assertEqual(request.call_count, 2)  # no unsafe retry on conflict


if __name__ == "__main__":
    unittest.main()
