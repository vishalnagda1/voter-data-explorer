import json
import tempfile
import unittest
from pathlib import Path

from voter_pdf_to_csv import (
    ConversionError,
    Occurrence,
    csv_row,
    english_name,
    load_transliteration_overrides,
    transliterate_text,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TransliterationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.overrides = load_transliteration_overrides(
            PROJECT_ROOT / "transliteration_overrides.json"
        )

    def test_preferred_spellings_do_not_require_review(self):
        result = english_name("सुरेश कुमार शर्मा", self.overrides)
        self.assertEqual(result.full, "Suresh Kumar Sharma")
        self.assertEqual((result.first, result.middle, result.last), ("Suresh", "Kumar", "Sharma"))
        self.assertFalse(result.needs_review)

    def test_unknown_word_uses_deterministic_fallback_and_requires_review(self):
        first, first_review = transliterate_text("अद्विक", {})
        second, second_review = transliterate_text("अद्विक", {})
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertTrue(first.isascii())
        self.assertTrue(first_review)
        self.assertTrue(second_review)

    def test_rule_engine_improves_common_unknown_names_but_keeps_review_flag(self):
        expected = {
            "राजेश": "Rajesh",
            "महेंद्र": "Mahendra",
            "धर्मेंद्र": "Dharmendra",
            "रवीन्द्र": "Ravindra",
            "ओमप्रकाश": "Omaprakash",
            "पंकज": "Pankaj",
            "डांगी": "Dangi",
        }
        for hindi, english in expected.items():
            with self.subTest(hindi=hindi):
                result, needs_review = transliterate_text(hindi, {})
                self.assertEqual(result, english)
                self.assertTrue(needs_review)

    def test_preferred_spellings_fix_ambiguous_schwas_and_v_or_w(self):
        expected = {
            "पंकज": "Pankaj",
            "डांगी": "Dangi",
            "भंवर लाल": "Bhanwar Lal",
            "प्रेमलता": "Premlata",
        }
        for hindi, english in expected.items():
            with self.subTest(hindi=hindi):
                result, needs_review = transliterate_text(hindi, self.overrides)
                self.assertEqual(result, english)
                self.assertFalse(needs_review)

    def test_preferred_spellings_preserve_conventional_long_vowels(self):
        expected = {
            "प्रदीप": "Pradeep",
            "प्रवीण": "Praveen",
            "संदीप": "Sandeep",
            "मीरा": "Meera",
            "मान": "Maan",
            "शीतल": "Sheetal",
            "नवीन": "Naveen",
            "शीनू": "Sheenu",
            "गिरधारी": "Girdhari",
            "भगवान": "Bhagwan",
            "तिवारी": "Tiwari",
        }
        for hindi, english in expected.items():
            with self.subTest(hindi=hindi):
                result, needs_review = transliterate_text(hindi, self.overrides)
                self.assertEqual(result, english)
                self.assertFalse(needs_review)

    def test_pra_conjunct_is_not_dropped_from_generated_spelling(self):
        result, needs_review = transliterate_text("प्रदीप", {})
        self.assertEqual(result, "Pradip")
        self.assertTrue(needs_review)

    def test_anusvara_uses_n_except_before_labial_consonants(self):
        expected = {"पंकज": "Pankaj", "संजय": "Sanjay", "संपत": "Sampat"}
        for hindi, english in expected.items():
            with self.subTest(hindi=hindi):
                result, needs_review = transliterate_text(hindi, {})
                self.assertEqual(result, english)
                self.assertTrue(needs_review)

    def test_v_or_w_is_selected_from_devanagari_context(self):
        expected = {
            "कुंवर": "Kunwar",
            "स्वर": "Swar",
            "राजेश्वरी": "Rajeshwari",
            "गोदावत": "Godawat",
            "राणावत": "Ranawat",
            "रावत": "Rawat",
            "विनोद": "Vinod",
            "रवीन्द्र": "Ravindra",
        }
        for hindi, english in expected.items():
            with self.subTest(hindi=hindi):
                result, needs_review = transliterate_text(hindi, {})
                self.assertEqual(result, english)
                self.assertTrue(needs_review)

    def test_decomposed_nukta_letters_are_transliterated(self):
        result, needs_review = transliterate_text("फ़ैज़", {})
        self.assertEqual(result, "Faiz")
        self.assertTrue(needs_review)

    def test_full_name_override_is_supported(self):
        result, needs_review = transliterate_text(
            "राम लाल", {"राम लाल": "Ramlal"}
        )
        self.assertEqual(result, "Ramlal")
        self.assertFalse(needs_review)

    def test_full_name_override_is_used_by_csv_name_path(self):
        result = english_name("राम लाल", {"राम लाल": "Ramlal"})
        self.assertEqual(result.full, "Ramlal")
        self.assertEqual((result.first, result.middle, result.last), ("Ramlal", "", ""))
        self.assertFalse(result.needs_review)

    def test_zero_width_characters_do_not_bypass_an_override(self):
        result, needs_review = transliterate_text("सु\u200dरेश", {"सुरेश": "Suresh"})
        self.assertEqual(result, "Suresh")
        self.assertFalse(needs_review)

    def test_csv_row_transliterates_voter_and_relative_names(self):
        record = Occurrence(
            serial_number=1,
            voter_id="AFC1467737",
            name_raw="",
            relative_name_raw="",
            relation_type="पिता",
            house_raw="1",
            age=30,
            gender="पुरुष",
            deletion_code="",
            page=3,
            order=1,
            cell_left_pt=37.6,
            id_y_pt=-160.8,
            name_y_pt=-175.1,
            relative_y_pt=-188.0,
            house_y_pt=-202.9,
            name="शंकर लाल खटीक",
            relative_name="राजेंद्र प्रसाद मिश्रा",
            house_number="1",
        )
        row = csv_row(
            record,
            {"ward_number": 80, "part_number": 6},
            Path("roll.pdf"),
            self.overrides,
        )
        self.assertEqual(row["name_english"], "Shankar Lal Khatik")
        self.assertEqual(row["relative_name_english"], "Rajendra Prasad Mishra")
        self.assertEqual(row["english_name_needs_review"], "No")
        self.assertEqual(row["relative_english_name_needs_review"], "No")
        self.assertEqual(
            " ".join(
                filter(
                    None,
                    [
                        row["english_first_name"],
                        row["english_middle_name"],
                        row["english_last_name"],
                    ],
                )
            ),
            row["name_english"],
        )

    def test_override_file_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
            with self.assertRaises(ConversionError):
                load_transliteration_overrides(path)

    def test_override_file_rejects_non_roman_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"राम": "राम"}), encoding="utf-8")
            with self.assertRaises(ConversionError):
                load_transliteration_overrides(path)


if __name__ == "__main__":
    unittest.main()
