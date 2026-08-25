import json
import re
import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


class PublicShowcaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (SITE / "index.html").read_text(encoding="utf-8")
        cls.javascript = (SITE / "app.js").read_text(encoding="utf-8")
        cls.styles = (SITE / "styles.css").read_text(encoding="utf-8")
        cls.manifest = json.loads((SITE / "demo-manifest.json").read_text(encoding="utf-8"))

    def test_showcase_is_explicitly_synthetic_and_disconnected(self):
        self.assertTrue(self.manifest["synthetic"])
        self.assertFalse(self.manifest["connected_to_runtime"])
        self.assertFalse(self.manifest["contains_real_learner_data"])
        self.assertIn("PUBLIC DEMO", self.html)
        self.assertIn("合成学习数据", self.html)

    def test_showcase_cannot_call_private_runtime(self):
        combined = self.html + self.javascript
        self.assertNotIn("/api/", combined)
        self.assertNotIn("fetch(", self.javascript)
        self.assertIn("connect-src 'none'", self.html)
        self.assertNotRegex(combined, r"https?://127\.0\.0\.1")
        self.assertNotRegex(combined, r"https?://localhost")

    def test_public_assets_are_local_and_scripts_are_not_inline(self):
        script_sources = re.findall(r"<script[^>]+src=\"([^\"]+)\"", self.html)
        self.assertEqual(script_sources, ["app.js?v=0.4.0"])
        self.assertNotRegex(self.html, r"<script(?![^>]+src=)[^>]*>")
        self.assertIn('href="styles.css?v=0.4.0"', self.html)
        self.assertIn('href="assets/mark.svg"', self.html)

    def test_social_card_is_a_standard_1200_by_630_png(self):
        card = (SITE / "assets" / "social-card.png").read_bytes()
        self.assertEqual(card[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", card[16:24]), (1200, 630))

    def test_tabs_have_accessible_controls_and_panels(self):
        tabs = set(re.findall(r'data-demo-tab="([^"]+)"', self.html))
        panels = set(re.findall(r'data-demo-panel="([^"]+)"', self.html))
        self.assertEqual(tabs, {"overview", "evidence", "agents"})
        self.assertEqual(tabs, panels)
        self.assertEqual(self.html.count('role="tab"'), 3)
        self.assertEqual(self.html.count('role="tabpanel"'), 3)

    def test_public_copy_contains_no_private_runtime_markers(self):
        combined = self.html + self.javascript + self.styles
        forbidden = ["student_id", "database_path", "question_bank_path", "C:\\Users\\"]
        for marker in forbidden:
            self.assertNotIn(marker, combined)

    def test_pages_workflow_deploys_only_site_directory(self):
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^\s+path: site\s*$")
        self.assertIn("python scripts/release_privacy_audit.py", workflow)
        self.assertIn("actions/upload-pages-artifact@v4", workflow)


if __name__ == "__main__":
    unittest.main()
