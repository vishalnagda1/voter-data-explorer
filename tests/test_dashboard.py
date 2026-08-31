import re
import shutil
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "voter_dashboard.html"


class DashboardArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    def test_dashboard_is_standalone_and_offline(self):
        self.assertNotRegex(self.html, r"<script[^>]+src=")
        self.assertNotRegex(self.html, r"<link[^>]+href=")
        self.assertNotRegex(self.html, r"https?://")
        self.assertRegex(self.html, r'<input[^>]+type="file"[^>]+multiple')

    def test_requested_filter_analytics_and_print_surfaces_exist(self):
        required_ids = {
            "fileInput",
            "globalSearch",
            "lastNameHindiFilter",
            "lastNameEnglishFilter",
            "advancedDialog",
            "analyticsStamp",
            "surnameBars",
            "tableHead",
            "printColumnGrid",
            "orientationBadge",
            "printArea",
        }
        present_ids = set(re.findall(r'id="([^"]+)"', self.html))
        self.assertTrue(required_ids.issubset(present_ids))
        self.assertIn('@page { size: A4 portrait;', self.html)
        self.assertIn('state.printColumns.length >= 6 ? "landscape" : "portrait"', self.html)

    def test_defaults_persistence_and_column_reordering_are_present(self):
        expected_order = (
            '["ward_number", "part_number", "serial_number", "voter_id", '
            '"name_hindi", "relation_type", "relative_name_hindi", "gender", '
            '"age", "roll_year", "house_number_or_address"]'
        )
        self.assertIn(f"const DEFAULT_VIEW = {expected_order};", self.html)
        self.assertIn('status: "Active"', self.html)
        self.assertIn('localStorage.setItem(STORAGE_KEY', self.html)
        self.assertIn('data-move-column=', self.html)
        self.assertIn('function reorderColumn(', self.html)
        self.assertIn('Surname counts are a community-name proxy only.', self.html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript syntax validation")
    def test_embedded_javascript_has_valid_syntax(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", self.html)
        self.assertEqual(len(scripts), 1)
        result = subprocess.run(
            ["node", "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"],
            input=scripts[0],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
