import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastinsight.pipeline import paper_from_ocr, vision_paper
from scripts.auto_pipeline import _verification_query


class ImageContentSearchTests(unittest.TestCase):
    def test_ocr_content_can_produce_a_search_query(self) -> None:
        with patch(
            "fastinsight.pipeline._llm_extract_paper",
            return_value={
                "title": "",
                "url": "",
                "search_query": "FirmAgent firmware fuzzing PoC",
                "text": "FirmAgent combines fuzzing and LLM agents for firmware vulnerability discovery.",
            },
        ):
            paper = paper_from_ocr(
                ["FirmAgent: firmware vulnerability discovery with fuzzing and LLM agents"]
            )

        self.assertEqual(paper["search_query"], "FirmAgent firmware fuzzing PoC")
        self.assertIn("FirmAgent", paper["text"])

    def test_vision_content_without_title_is_kept_for_search(self) -> None:
        response = {
            "title": "",
            "url": "",
            "search_query": "FirmAgent firmware fuzzing",
            "text": "NDSS 2026 FirmAgent combines fuzzing and LLM agents.",
        }
        with tempfile.TemporaryDirectory(prefix="fastinsight-vision-") as temp_dir:
            image_path = Path(temp_dir) / "paper.jpg"
            image_path.write_bytes(b"image")
            with patch(
                "fastinsight.pipeline.env",
                side_effect=lambda name, default="": {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_MODEL": "test-model",
                }.get(name, default),
            ), patch(
                "fastinsight.pipeline._chat_json",
                return_value={"content": json.dumps(response, ensure_ascii=False)},
            ):
                paper = vision_paper(str(image_path))

        self.assertEqual(paper["search_query"], "FirmAgent firmware fuzzing")
        self.assertIn("FirmAgent", paper["text"])

    def test_search_query_is_prioritized_for_verification(self) -> None:
        query = _verification_query(
            {"search_query": "FirmAgent firmware fuzzing", "title": "OCR first line"},
            "OCR first line",
        )
        self.assertEqual(query, "FirmAgent firmware fuzzing")

    def test_verification_query_keeps_existing_url(self) -> None:
        query = _verification_query(
            {"url": "https://arxiv.org/abs/1706.03762", "title": "A description"},
            "A description",
        )
        self.assertEqual(query, "https://arxiv.org/abs/1706.03762 A description")

    def test_url_only_paper_reaches_verifier(self) -> None:
        with patch("scripts.auto_pipeline._run_json", return_value=({}, 0)) as run:
            from scripts.auto_pipeline import enrich_paper

            enrich_paper({"url": "https://example.test/paper"})
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--query") + 1], "https://example.test/paper")


if __name__ == "__main__":
    unittest.main()
