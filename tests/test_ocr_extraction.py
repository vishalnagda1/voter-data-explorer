import tempfile
import unittest
from pathlib import Path

from voter_pdf_to_csv import (
    extract_ocr_line,
    infer_document_metadata,
    infer_filename_metadata,
    load_tsv,
    merge_detected_metadata,
)


def ocr_word(text, left, top, width, height, confidence, line):
    return {
        "text": text,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "conf": confidence,
        "line": line,
    }


def ocr_line(texts, left, top, line):
    words = []
    cursor = left
    for text in texts:
        width = max(20, len(text) * 18)
        words.append(ocr_word(text, cursor, top, width, 35, 95.0, line))
        cursor += width + 12
    return words


class OcrExtractionTests(unittest.TestCase):
    def test_filename_metadata_supports_photo_and_non_photo_roll_names(self):
        photo = infer_filename_metadata(
            Path("WithPhoto_UDAIPUR NAGAR NIGAM-Ward No-080-Part No-001.pdf")
        )
        non_photo = infer_filename_metadata(Path("KARANPUR-Ward No-003.pdf"))

        self.assertEqual(
            photo,
            {
                "ward_number": 80,
                "part_number": 1,
                "municipality": "UDAIPUR NAGAR NIGAM",
            },
        )
        self.assertEqual(
            non_photo,
            {"ward_number": 3, "part_number": "", "municipality": "KARANPUR"},
        )

    def test_panchayat_metadata_comes_from_pdf_header(self):
        words = []
        words += ocr_line(
            ["पंचायत", "चुनाव", "निर्वाचक", "नामावली,", "2026"],
            970,
            181,
            ("1", "2", "1"),
        )
        words += ocr_line(
            ["ग्रामपंचायत", ":", "करणपुर"], 157, 445, ("2", "1", "1")
        )
        words += ocr_line(
            ["वार्ड", "क्रमांक", ":", "3"], 1260, 442, ("3", "1", "1")
        )
        embedded = [{"text": ": 3", "x": 302.6, "y": -115.2, "order": 1}]

        metadata = infer_document_metadata(words, embedded, 300, {})

        self.assertEqual(metadata["roll_type"], "panchayat")
        self.assertEqual(metadata["municipality_hindi"], "करणपुर")
        self.assertEqual(metadata["municipality"], "Karanapur")
        self.assertEqual(metadata["ward_number"], 3)
        self.assertEqual(metadata["part_number"], "")

    def test_municipal_metadata_uses_embedded_digits_when_ocr_misreads_part(self):
        words = []
        words += ocr_line(
            ["नगरपालिका", "चुनाव", "निर्वाचक", "नामावली,", "2026"],
            970,
            177,
            ("1", "2", "1"),
        )
        words += ocr_line(
            ["नगरनिगम", "/", "नगरपरिषद", "/", "नगरपालिका", "का", "नाम", ":", "उदयपुर"],
            157,
            279,
            ("2", "1", "1"),
        )
        words += ocr_line(
            ["वार्ड", "संख्या", ":", "80"], 157, 471, ("3", "1", "1")
        )
        words += ocr_line(
            ["भाग", "संख्या", ":", "7"], 1220, 473, ("4", "1", "1")
        )
        embedded = [
            {"text": ": 80", "x": 37.8, "y": -122.9, "order": 1},
            {"text": ": 1", "x": 292.8, "y": -122.9, "order": 2},
        ]

        metadata = infer_document_metadata(words, embedded, 300, {})

        self.assertEqual(metadata["roll_type"], "municipal")
        self.assertEqual(metadata["municipality_hindi"], "उदयपुर")
        self.assertEqual(metadata["ward_number"], 80)
        self.assertEqual(metadata["part_number"], 1)

    def test_filename_only_enriches_pdf_verified_location(self):
        document = {
            "roll_type": "municipal",
            "municipality_hindi": "उदयपुर",
            "municipality": "Udayapur",
            "ward_number": 80,
            "part_number": 1,
            "metadata_source": "pdf_header_ocr_and_text_layer",
        }
        matching = infer_filename_metadata(
            Path("WithPhoto_UDAIPUR NAGAR NIGAM-Ward No-999-Part No-999.pdf")
        )
        arbitrary = infer_filename_metadata(Path("customer-upload-48391.pdf"))

        enriched = merge_detected_metadata(document, matching)
        renamed = merge_detected_metadata(document, arbitrary)

        self.assertEqual(enriched["municipality"], "UDAIPUR NAGAR NIGAM")
        self.assertEqual(enriched["ward_number"], 80)
        self.assertEqual(enriched["part_number"], 1)
        self.assertEqual(renamed["municipality"], "Udayapur")
        self.assertEqual(renamed["ward_number"], 80)
        self.assertEqual(renamed["part_number"], 1)

    def test_tesseract_tsv_quotes_are_literal_text(self):
        header = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
            "width\theight\tconf\ttext\n"
        )
        rows = (
            '5\t1\t1\t1\t1\t1\t100\t200\t40\t20\t10.0\t"8896\n'
            "5\t1\t1\t1\t1\t2\t150\t200\t50\t20\t95.0\tमदन\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.tsv"
            path.write_text(header + rows, encoding="utf-8")
            words = load_tsv(path)

        self.assertEqual([word["text"] for word in words], ['"8896', "मदन"])

    def test_adjacent_photo_placeholder_is_not_part_of_name(self):
        words = [
            ocr_word("नाम:", 156, 710, 65, 26, 96.0, ("1", "1", "1")),
            ocr_word("ईश्वरलाल", 232, 710, 120, 26, 94.0, ("1", "1", "1")),
            ocr_word("शि0ए0", 613, 695, 90, 60, 5.0, ("1", "1", "1")),
        ]

        text, confidence = extract_ocr_line(words, 37.6, -175.2, "name", 300)

        self.assertEqual(text, "ईश्वरलाल")
        self.assertEqual(confidence, 95.0)

    def test_value_on_split_ocr_line_beats_empty_label_line(self):
        words = [
            ocr_word("पति", 162, 1623, 58, 37, 95.0, ("1", "9", "2")),
            ocr_word("का", 229, 1607, 33, 57, 96.0, ("1", "9", "2")),
            ocr_word("नाम:", 275, 1633, 65, 27, 92.0, ("1", "9", "2")),
            ocr_word("पति", 162, 1633, 21, 27, 96.0, ("1", "9", "3")),
            ocr_word("का", 227, 1633, 40, 27, 97.0, ("1", "9", "3")),
            ocr_word("नाम:", 335, 1652, 5, 7, 85.0, ("1", "9", "3")),
            ocr_word("डालू", 349, 1635, 65, 35, 90.0, ("1", "9", "3")),
        ]

        text, confidence = extract_ocr_line(words, 37.6, -398.8, "relative", 300)

        self.assertEqual(text, "डालू")
        self.assertGreater(confidence, 85.0)

    def test_value_in_adjacent_tesseract_block_is_accepted(self):
        words = [
            ocr_word("पति", 162, 2829, 105, 36, 96.0, ("1", "3", "32")),
            ocr_word("का", 230, 2814, 32, 55, 97.0, ("1", "3", "32")),
            ocr_word("नाम:", 275, 2839, 65, 26, 92.0, ("1", "3", "32")),
            ocr_word(":सतु", 335, 2841, 64, 34, 49.0, ("1", "4", "1")),
        ]

        text, confidence = extract_ocr_line(words, 37.6, -688.1, "relative", 300)

        self.assertEqual(text, "सतु")
        self.assertEqual(confidence, 49.0)


if __name__ == "__main__":
    unittest.main()
