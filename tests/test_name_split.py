import csv
import tempfile
import unittest
from pathlib import Path

from voter_pdf_to_csv import CSV_FIELDS, Occurrence, csv_row, normalized, split_name, verify_written_csv


class NameSplitTests(unittest.TestCase):
    def test_single_word_is_first_name_and_requires_review(self):
        parts = split_name("पूनम")
        self.assertEqual((parts.first, parts.middle, parts.last), ("पूनम", "", ""))
        self.assertTrue(parts.needs_review)

    def test_two_words_use_first_and_last_and_require_review(self):
        parts = split_name("दरिया बाई")
        self.assertEqual((parts.first, parts.middle, parts.last), ("दरिया", "", "बाई"))
        self.assertTrue(parts.needs_review)

    def test_three_words_use_first_middle_last(self):
        parts = split_name("शंकर लाल खटीक")
        self.assertEqual((parts.first, parts.middle, parts.last), ("शंकर", "लाल", "खटीक"))
        self.assertFalse(parts.needs_review)

    def test_compound_name_preserves_all_middle_words_and_requires_review(self):
        parts = split_name("राजेंद्र प्रसाद कुमार मिश्रा")
        self.assertEqual(
            (parts.first, parts.middle, parts.last),
            ("राजेंद्र", "प्रसाद कुमार", "मिश्रा"),
        )
        self.assertTrue(parts.needs_review)

    def test_repeated_and_abbreviated_tokens_require_review(self):
        self.assertTrue(split_name("देवेंद्र भोई भोई").needs_review)
        self.assertTrue(split_name("पी० राजन नायर").needs_review)

    def test_csv_row_splits_voter_and_relative_names_independently(self):
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
        row = csv_row(record, {"ward_number": 80, "part_number": 6}, Path("roll.pdf"))
        self.assertEqual(
            (row["first_name"], row["middle_name"], row["last_name"]),
            ("शंकर", "लाल", "खटीक"),
        )
        self.assertEqual(
            (
                row["relative_first_name"],
                row["relative_middle_name"],
                row["relative_last_name"],
            ),
            ("राजेंद्र", "प्रसाद", "मिश्रा"),
        )
        self.assertEqual(row["name_split_needs_review"], "No")
        self.assertEqual(row["relative_name_split_needs_review"], "No")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "test.csv"
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
                writer.writeheader()
                writer.writerow(row)
            verify_written_csv(output, [record])
            with output.open(encoding="utf-8-sig", newline="") as handle:
                written = next(csv.DictReader(handle))
            reconstructed = normalized(
                " ".join([written["first_name"], written["middle_name"], written["last_name"]])
            )
            self.assertEqual(reconstructed, written["name_hindi"])


if __name__ == "__main__":
    unittest.main()
