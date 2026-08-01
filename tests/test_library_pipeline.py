from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.quality import run_quality_checks
from english_tracker.question_pipeline import pair_library_sources, structure_library, structure_summary
from english_tracker.util import utc_now


class LibraryPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"
        initialize_database(self.data_dir, student_id="STU-TEST")
        self.conn = connect(database_path(self.data_dir))
        source_dir = Path(self.temp.name) / "sources"
        source_dir.mkdir()
        self.prompt = source_dir / "Mock Test（原卷版）.docx"
        self.answer = source_dir / "Mock Test（解析版）.docx"
        self.prompt.write_text("placeholder", encoding="utf-8")
        self.answer.write_text("placeholder", encoding="utf-8")
        extracted = self.data_dir / "library_cache" / "extracted"
        extracted.mkdir(parents=True)
        prompt_text = extracted / "prompt.txt"
        answer_text = extracted / "answer.txt"
        prompt_text.write_text(
            """第一部分 知识运用
阅读下面短文，掌握其大意，从每题所给的A、B、C、D四个选项中选出最佳选项。
The learner made a ____1____ choice and kept ____2____.
1. A. quick B. quickly C. quicker D. quickest
2. A. try B. tried C. trying D. tries
第二节
阅读下列短文，在未给提示词的空白处仅填写1个恰当的单词，在给出提示词的空白处用括号内所给词的正确形式填空。
It ____3____ (be) useful when people learn from mistakes.
""".strip(),
            encoding="utf-8",
        )
        answer_text.write_text(
            """【答案】1. A 2. C
【答案】3. is
3. 【解析】考查时态和主谓一致。
""".strip(),
            encoding="utf-8",
        )
        now = utc_now()
        rows = [
            ("RES-PROMPT", self.prompt, prompt_text, "Mock Test（原卷版）.docx"),
            ("RES-ANSWER", self.answer, answer_text, "Mock Test（解析版）.docx"),
        ]
        for resource_id, path, text_path, file_name in rows:
            self.conn.execute(
                """
                INSERT INTO library_resources(
                  resource_id,library_key,absolute_path,relative_path,file_name,extension,media_kind,
                  subject_scope,source_group,year_hint,size_bytes,modified_at,is_canonical,parse_status,
                  extraction_method,extracted_text_path,extracted_char_count,verification_status,indexed_at,updated_at
                ) VALUES (?, 'english_library', ?, ?, ?, '.docx', 'document', 'english', 'sample',
                          2025, ?, ?, 1, 'extracted', 'test', ?, ?, 'unverified', ?, ?)
                """,
                (
                    resource_id, str(path), file_name, file_name, path.stat().st_size, now,
                    str(text_path), len(text_path.read_text(encoding="utf-8")), now, now,
                ),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def test_pair_structure_and_verification_boundary(self) -> None:
        paired = pair_library_sources(self.conn)
        self.assertEqual(paired["source_sets"], 1)
        self.assertEqual(paired["pairing_status"]["paired"], 1)

        result = structure_library(self.conn)
        self.assertEqual(result["failed_source_sets"], 0)
        self.assertEqual(result["question_candidates"], 3)
        self.assertGreater(result["text_chunks"], 0)

        questions = list(self.conn.execute(
            "SELECT original_number,question_type,answer,verification_status FROM staged_questions ORDER BY source_ordinal"
        ))
        self.assertEqual([row["answer"] for row in questions], ["A", "C", "is"])
        self.assertEqual([row["question_type"] for row in questions], ["完形填空", "完形填空", "语法填空"])
        self.assertTrue(all(row["verification_status"] in {"suggested", "needs_check"} for row in questions))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM staged_question_knowledge_map WHERE verification_status<>'suggested'"
            ).fetchone()[0],
            0,
        )

        summary = structure_summary(self.conn)
        self.assertEqual(summary["candidate_quality"]["answer_coverage"], 1.0)
        self.assertEqual(run_quality_checks(self.conn)["trust_status"], "ready")


if __name__ == "__main__":
    unittest.main()
