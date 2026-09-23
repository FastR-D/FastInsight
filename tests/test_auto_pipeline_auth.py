import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import auto_pipeline
import route_paper


class AutomaticPublishingAuthTests(unittest.TestCase):
    def test_route_preserves_explicit_receiver_reference(self):
        owners, invalid = route_paper._collect_valid_owners([
            {"name": "Shared", "feishu_open_id": "ou_one", "receiver_ref": "acct-one"},
            {"name": "Shared", "feishu_open_id": "ou_two"},
        ])
        self.assertEqual(invalid, [])
        self.assertEqual(owners[0]["receiver_ref"], "acct-one")
        self.assertNotIn("receiver_ref", owners[1])

    def test_service_mode_requires_ref_and_never_falls_back_to_name(self):
        with patch.dict(os.environ, {
            "FASTINSIGHT_FASTCAS_ISSUER": "http://127.0.0.1:8900",
            "FASTINSIGHT_FASTCAS_CLIENT_ID": "insight-service",
            "FASTINSIGHT_FASTCAS_CLIENT_SECRET": "secret",
            "FASTINSIGHT_INGEST_KEY": "",
        }), patch.object(auto_pipeline, "_run_json", side_effect=[
            ({}, 0), ({}, 0), ({"owners": [
                {"name": "Shared", "feishu_open_id": "ou_one", "receiver_ref": "acct-one"},
                {"name": "Shared", "feishu_open_id": "ou_two"},
            ]}, 0),
        ]), patch.object(auto_pipeline, "_publish", return_value={"ok": True}) as publish:
            result = auto_pipeline.process({"title": "Test paper"})
        self.assertEqual(len(result["delivered"]), 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("receiver_ref", result["errors"][0])
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(publish.call_args.kwargs["receiver_ref"], "acct-one")
        self.assertIsNotNone(publish.call_args.kwargs["cas_config"])

    def test_mixed_credentials_rejected_before_processing(self):
        with patch.dict(os.environ, {
            "FASTINSIGHT_FASTCAS_ISSUER": "http://127.0.0.1:8900",
            "FASTINSIGHT_FASTCAS_CLIENT_ID": "insight-service",
            "FASTINSIGHT_FASTCAS_CLIENT_SECRET": "secret",
            "FASTINSIGHT_INGEST_KEY": "legacy",
        }), patch.object(auto_pipeline, "_run_json") as run:
            with self.assertRaisesRegex(ValueError, "不能与 ingest key 混用"):
                auto_pipeline.process({"title": "Test paper"})
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
