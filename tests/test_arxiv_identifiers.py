from __future__ import annotations

import unittest

from fastinsight.article import extract_identifiers
from scripts.verify_paper import _find_identifiers


class ArxivIdentifierTests(unittest.TestCase):
    def assert_identifiers(self, text: str, expected: str) -> None:
        for extractor in (extract_identifiers, _find_identifiers):
            with self.subTest(extractor=extractor.__name__, text=text):
                self.assertEqual(extractor(text).get("arxiv_id", ""), expected)

    def assert_modern_forms(self, arxiv_id: str) -> None:
        for text in (
            arxiv_id,
            f"see ({arxiv_id})",
            f"arXiv: {arxiv_id}",
            f"https://arxiv.org/abs/{arxiv_id}",
            f"https://arxiv.org/pdf/{arxiv_id}",
            f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            f"http://arxiv.org/abs/{arxiv_id}",
            f"arxiv.org/abs/{arxiv_id}",
        ):
            self.assert_identifiers(text, arxiv_id)

    def test_four_digit_identifiers(self) -> None:
        self.assert_modern_forms("1409.0473")

    def test_four_digit_identifiers_preserve_version(self) -> None:
        self.assert_modern_forms("1409.0473v12")

    def test_five_digit_identifiers(self) -> None:
        self.assert_modern_forms("1706.03762")

    def test_five_digit_identifiers_preserve_version(self) -> None:
        self.assert_modern_forms("1706.03762v12")

    def test_long_numeric_identifiers_are_not_truncated(self) -> None:
        for arxiv_id in (
            "1409.047312",
            "1706.0376201",
            "1409.04731234567890123456",
            "11409.0473",
            "11706.03762",
        ):
            for version in ("", "v12"):
                for template in (
                    "{}",
                    "arXiv: {}",
                    "https://arxiv.org/abs/{}",
                    "https://arxiv.org/pdf/{}",
                    "https://arxiv.org/pdf/{}.pdf",
                ):
                    self.assert_identifiers(template.format(arxiv_id + version), "")

    def test_bare_identifiers_keep_existing_boundaries(self) -> None:
        for arxiv_id in ("1409.0473", "1706.03762"):
            for template in ("prefix{}", "folder/{}", "1.{}", "{}suffix", "{}.1"):
                self.assert_identifiers(template.format(arxiv_id), "")

    def test_legacy_identifiers_are_unchanged(self) -> None:
        for arxiv_id in ("hep-th/9901001", "math.GT/0309136v2"):
            for template in (
                "arXiv: {}",
                "https://arxiv.org/abs/{}",
                "https://arxiv.org/pdf/{}.pdf",
            ):
                self.assert_identifiers(template.format(arxiv_id), arxiv_id)

    def test_labeled_identifiers_still_take_priority_over_bare(self) -> None:
        self.assert_identifiers("1706.03762 arXiv:1409.0473v2", "1409.0473v2")

    def test_doi_normalization_and_first_match_are_unchanged(self) -> None:
        text = "DOI: 10.1000/ABC; arXiv:1409.0473v2 DOI: 10.1000/DEF"
        for extractor in (extract_identifiers, _find_identifiers):
            with self.subTest(extractor=extractor.__name__):
                found = extractor(text)
                self.assertEqual(found["doi"], "10.1000/abc")
                self.assertEqual(found.get("arxiv_id", ""), "1409.0473v2")


if __name__ == "__main__":
    unittest.main()
