import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voter_pdf_to_csv import (
    CSV_FIELDS,
    ConversionError,
    interactive_argv,
    main,
    retransliterate_csv,
)


def sample_row() -> dict[str, str]:
    row = {field: "" for field in CSV_FIELDS}
    row.update(
        {
            "serial_number": "1",
            "voter_id": "AFC1467737",
            "name_hindi": "राजेश",
            "first_name": "राजेश",
            "name_split_needs_review": "Yes",
            "name_english": "Raajesh",
            "english_first_name": "Raajesh",
            "english_name_needs_review": "Yes",
            "relative_name_hindi": "सुरेश",
            "relative_first_name": "सुरेश",
            "relative_name_split_needs_review": "Yes",
            "relative_name_english": "Reviewed Suresh",
            "relative_english_first_name": "Reviewed",
            "relative_english_last_name": "Suresh",
            "relative_english_name_needs_review": "No",
            "age": "30",
            "gender": "पुरुष",
            "status": "Active",
            "needs_review": "No",
        }
    )
    return row


def write_csv(path: Path, row: dict[str, str], fieldnames=CSV_FIELDS) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)


def read_row(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return next(csv.DictReader(handle))


class CsvRetransliterationTests(unittest.TestCase):
    def test_default_mode_updates_review_rows_and_preserves_reviewed_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "existing.csv"
            output_dir = root / "updated"
            original = sample_row()
            write_csv(source, original)

            output, audit = retransliterate_csv(
                source,
                output_dir,
                {"राजेश": "Rajesh", "सुरेश": "Suresh"},
            )

            updated = read_row(output)
            self.assertEqual(updated["name_english"], "Rajesh")
            self.assertEqual(updated["english_first_name"], "Rajesh")
            self.assertEqual(updated["english_name_needs_review"], "No")
            self.assertEqual(updated["relative_name_english"], "Reviewed Suresh")
            self.assertEqual(updated["relative_english_name_needs_review"], "No")
            self.assertEqual(updated["voter_id"], original["voter_id"])
            self.assertEqual(updated["name_hindi"], original["name_hindi"])
            self.assertEqual(read_row(source), original)
            self.assertEqual(audit["voter_names_updated"], 1)
            self.assertEqual(audit["relative_names_updated"], 0)

    def test_force_mode_regenerates_reviewed_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "existing.csv"
            write_csv(source, sample_row())

            output, audit = retransliterate_csv(
                source,
                root / "updated",
                {"राजेश": "Rajesh", "सुरेश": "Suresh"},
                force=True,
            )

            updated = read_row(output)
            self.assertEqual(updated["relative_name_english"], "Suresh")
            self.assertEqual(updated["relative_english_first_name"], "Suresh")
            self.assertEqual(updated["relative_english_last_name"], "")
            self.assertEqual(audit["voter_names_updated"], 1)
            self.assertEqual(audit["relative_names_updated"], 1)

    def test_missing_required_columns_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "incompatible.csv"
            write_csv(source, {"name_hindi": "राजेश"}, ["name_hindi"])
            with self.assertRaises(ConversionError):
                retransliterate_csv(source, root / "updated", {})

    def test_csv_mode_main_does_not_probe_ocr_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "existing.csv"
            output_dir = root / "updated"
            write_csv(source, sample_row())
            with patch(
                "voter_pdf_to_csv.find_executable",
                side_effect=AssertionError("CSV mode must not inspect PDF tools"),
            ):
                result = main(
                    [
                        str(source),
                        "--retransliterate-csv",
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertTrue((output_dir / source.name).is_file())

    def test_interactive_csv_mode_builds_arguments_without_ocr_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "existing.csv"
            source.write_text("name_hindi\nराजेश\n", encoding="utf-8")
            responses = ["2", str(root), "n", "all", "", "n", "n", "y"]
            with patch("builtins.input", side_effect=responses):
                argv = interactive_argv()

            self.assertIn(str(source.resolve()), argv)
            self.assertIn("--retransliterate-csv", argv)
            self.assertNotIn("--jobs", argv)
            output_index = argv.index("--output-dir") + 1
            self.assertEqual(
                Path(argv[output_index]),
                (root / "retransliterated_csv").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
