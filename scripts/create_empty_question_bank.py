from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  source_id TEXT PRIMARY KEY,processing_status TEXT NOT NULL DEFAULT 'empty'
);
CREATE TABLE IF NOT EXISTS passages(
  passage_id TEXT PRIMARY KEY,title TEXT,passage_text TEXT,passage_type TEXT,
  source_id TEXT,source_page INTEGER,original_number TEXT,word_count INTEGER,
  context_tags TEXT,verification_status TEXT
);
CREATE TABLE IF NOT EXISTS questions(
  question_id TEXT PRIMARY KEY,source_id TEXT,passage_id TEXT,year INTEGER,
  exam_type TEXT,district_or_school TEXT,section TEXT,question_type TEXT,
  original_number TEXT,stem TEXT,answer TEXT,explanation_raw TEXT,
  primary_test_point TEXT,secondary_test_points TEXT,difficulty TEXT,
  recommended_use TEXT,verification_status TEXT,source_page INTEGER,
  source_path TEXT,source_ordinal INTEGER
);
CREATE TABLE IF NOT EXISTS options(
  question_id TEXT,option_label TEXT,option_text TEXT,
  PRIMARY KEY(question_id,option_label)
);
CREATE TABLE IF NOT EXISTS question_tag_map(
  question_id TEXT,tag_name TEXT,tag_role TEXT,
  PRIMARY KEY(question_id,tag_name,tag_role)
);
CREATE TABLE IF NOT EXISTS teaching_methods(method_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS textbook_pages(page_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS review_queue(review_id TEXT PRIMARY KEY);
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an empty private question-bank shell for Cassian Atlas.")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(output) as conn:
        conn.executescript(SCHEMA)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
