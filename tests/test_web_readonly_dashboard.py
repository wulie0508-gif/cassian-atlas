from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "english_tracker" / "web"
WEBAPP = ROOT / "src" / "english_tracker" / "webapp.py"


class ReadOnlyDashboardContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = (WEB / "app.js").read_text(encoding="utf-8")
        cls.index = (WEB / "index.html").read_text(encoding="utf-8")
        cls.styles = (WEB / "styles.css").read_text(encoding="utf-8")
        cls.i18n = (WEB / "i18n.js").read_text(encoding="utf-8")
        cls.webapp = WEBAPP.read_text(encoding="utf-8")

    def test_frontend_has_no_http_mutation_verb_or_write_endpoint(self):
        self.assertIsNone(
            re.search(r"\b(?:POST|PUT|PATCH|DELETE)\b", self.app),
            "app.js must not issue or advertise an HTTP mutation",
        )
        for endpoint in (
            "/api/grammar/select-passages",
            "/api/reading/diagnostics",
            "/api/dictation/results",
            "/api/agent/route",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertNotIn(endpoint, self.app)
        self.assertNotRegex(self.app, r"\bapi\([^\n]+,\s*\{")

    def test_removed_data_entry_controls_do_not_return(self):
        removed_ids = (
            "add-student-button",
            "student-dialog",
            "student-form",
            "assessment-form",
            "dictation-form",
        )
        combined = self.index + self.app
        for element_id in removed_ids:
            with self.subTest(element_id=element_id):
                self.assertNotIn(element_id, combined)
        self.assertNotIn("diagnostic-form", self.app)

    def test_student_switch_is_shareable_and_keeps_local_fallback(self):
        self.assertIn('id="student-select"', self.index)
        self.assertIn("initialQuery.get('student_id')", self.app)
        self.assertIn("window.localStorage.getItem('open-tutor-student')", self.app)
        self.assertIn("url.searchParams.set('student_id', state.studentId)", self.app)
        self.assertIn("window.history.replaceState", self.app)

    def test_dashboard_uses_cassian_atlas_brand_and_keeps_legacy_storage(self):
        self.assertIn("<title>Cassian Atlas</title>", self.index)
        self.assertIn("Cassian Atlas", self.i18n)
        self.assertIn("window.CassianAtlasI18n", self.i18n)
        self.assertIn("window.CassianAtlasI18n", self.app)
        self.assertIn("open-tutor-ledger-detail-mode", self.app)

    def test_subject_selector_only_uses_the_selected_students_enrollments(self):
        self.assertIn("selectedStudent?.subjects", self.app)
        self.assertIn("filter(subject => enrolledCodes.has(subject.subject_code))", self.app)
        self.assertIn("!availableSubjectCodes.has(state.subjectCode)", self.app)
        student_change = self.app.split("studentSelect.addEventListener('change'", 1)[1]
        self.assertIn("populateWorkspaceControls();", student_change.split("});", 1)[0])

    def test_serving_the_read_only_frontend_does_not_mutate_work_items(self):
        self.assertNotIn("UPDATE project_work_items", self.webapp)

    def test_teacher_home_uses_decision_endpoint_and_not_router_marketing(self):
        overview = self.app.split("async function renderOverview", 1)[1].split(
            "async function renderQuestionBank", 1
        )[0]
        self.assertIn("/api/teacher/dashboard", overview)
        for label in (
            "下一节优先处理",
            "近期可比正确率",
            "待处理与证据缺口",
            "近期同口径表现",
            "Top 5 薄弱点证据",
            "最近有作答的课程",
            "数据覆盖与口径",
        ):
            with self.subTest(label=label):
                self.assertIn(label, overview)
                self.assertIn(label, self.i18n)
        self.assertNotIn("ROUTED · LOW-FRICTION", overview)
        self.assertNotIn("三个工作对话都已接通", overview)
        self.assertNotIn("当前成绩", overview)

    def test_navigation_is_grouped_and_mobile_more_keeps_system_routes(self):
        for label in ("教学", "资料与系统", "教学总览", "趋势与薄弱点", "课程与测验", "词汇复测"):
            self.assertIn(label, self.index)
        self.assertIn('id="more-nav-button"', self.index)
        self.assertIn('id="mobile-more-menu"', self.index)
        for route in ("question-bank", "library", "workflow"):
            self.assertRegex(
                self.index,
                rf'id="mobile-more-menu"[\s\S]+data-view="{route}"',
            )
        self.assertIn("@media (max-width: 900px)", self.styles)
        self.assertIn("grid-template-columns: repeat(5", self.styles)
        self.assertRegex(self.styles, r"min-height:\s*44px")

    def test_accessibility_status_retry_and_table_contracts_are_present(self):
        self.assertIn('id="app-status"', self.index)
        self.assertIn('role="status"', self.index)
        view_tag = re.search(r'<section id="view"[^>]+>', self.index).group(0)
        self.assertNotIn("aria-live", view_tag)
        self.assertIn('aria-busy="true"', view_tag)
        self.assertIn('aria-labelledby="dialog-title"', self.index)
        self.assertIn("aria-current", self.app)
        self.assertIn("role=\"alert\"", self.app)
        self.assertIn("data-action=\"boot-retry\"", self.app)
        self.assertIn("enhanceReadOnlyTables", self.app)
        self.assertIn("setAttribute('scope', 'col')", self.app)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.styles)
        self.assertIn(".sr-only", self.styles)

    def test_stale_render_requests_are_aborted_and_empty_student_is_explicit(self):
        self.assertIn("new AbortController()", self.app)
        self.assertIn("state.renderController?.abort()", self.app)
        self.assertIn("renderGeneration", self.app)
        self.assertIn("ensureCurrent(context)", self.app)
        self.assertIn("if (!state.studentId)", self.app)
        self.assertIn("尚未添加学生", self.app)

    def test_single_subject_and_mobile_settings_are_supported(self):
        self.assertIn("document.body.classList.toggle('single-subject'", self.app)
        self.assertIn("body.single-subject .subject-control", self.styles)
        self.assertIn('id="settings-button"', self.index)
        self.assertIn('id="settings-menu"', self.index)
        self.assertIn('id="locale-select"', self.index)
        self.assertIn('id="detail-mode-button"', self.index)


if __name__ == "__main__":
    unittest.main()
