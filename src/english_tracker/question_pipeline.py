from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .enrichment import GRAMMAR_KEYWORDS
from .util import stable_id, utc_now


PARSER_VERSION = "library-structure-v1"
ANSWER_TOKENS = ("解析", "答案", "详解", "教师版", "答案版", "解答")
PROMPT_TOKENS = ("空白", "原卷", "试题", "学生版", "纯净版", "未解析")
AUDIO_TOKENS = ("听力音频", "听力录音", "音频", "录音")
TEACHING_TOKENS = ("解题觉醒", "名师大招", "教材", "讲义", "知识点", "专题")
VARIANT_TOKENS = ANSWER_TOKENS + PROMPT_TOKENS + AUDIO_TOKENS + (
    "word版", "pdf版", "word", "pdf", "完整版", "打印版", "原卷版", "解析卷"
)

QUESTION_LINE_RE = re.compile(r"^\s*(\d{1,3})\s*[.\uff0e\u3001\)]\s*(.+?)\s*$")
BLANK_RE = re.compile(r"(?:_{2,}|\uff3f{2,})\s*(\d{1,3})\s*(?:_{2,}|\uff3f{2,})")
INLINE_ANSWER_RE = re.compile(r"^\s*(\d{1,3})\s*[.\uff0e\u3001\)]?\s*(?:\u3010?\u7b54\u6848\u3011?|\u7b54\u6848)\s*[:\uff1a]?\s*(.+?)\s*$", re.I)
DETAIL_RE = re.compile(r"^\s*(\d{1,3})\s*[.\uff0e\u3001\)]?\s*(?:\u3010?\u89e3\u6790\u3011?|\u89e3\u6790)\s*[:\uff1a]?\s*(.*)$", re.I)
OPTION_RE = re.compile(r"(?:^|\s)([A-K])\s*[.\uff0e\u3001\)]\s*(.+?)(?=(?:\s+[A-K]\s*[.\uff0e\u3001\)])|$)", re.S)
HEADING_HINTS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("listening comprehension", "听力理解", r"第一部分\s*听力"), "听力理解", "listening"),
    (("grammar and vocabulary", "语法和词汇", "语法填空", "语法短文填空"), "语法填空", "grammar"),
    (("cloze", "完形填空", "语言运用", "知识运用", "掌握其大意"), "完形填空", "cloze"),
    (("reading comprehension", "阅读理解", r"第[一二三四五\d]+部分\s*阅读"), "阅读理解", "reading"),
    (("六选四", "七选五", "选句填空", "选项中有两项为多余"), "六四/七五选句", "gap_fill"),
    (("在空白处填入", "括号内单词的正确形式", "括号内所给词的正确形式", "提示词的空白处"), "语法填空", "grammar_instruction"),
    (("用英文回答问题", "answer the questions in english"), "阅读简答", "short_answer_reading"),
    (("translation", "翻译", "汉译英"), "汉译英", "translation"),
    (("summary writing", "概要写作", "概写"), "概要写作", "summary"),
    (("guided writing", "应用文", "读后续写", "写作", "书面表达"), "写作", "writing"),
    (("选词填空", "word bank"), "选词填空", "word_fill"),
)


def _text(value: str | None) -> str:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", normalized)


def _resource_role(row: sqlite3.Row) -> tuple[str, float, str]:
    name = _text(row["file_name"]).lower()
    relative = _text(row["relative_path"]).lower()
    if row["media_kind"] == "audio":
        return "audio", 1.0, "扩展名为音频且位于英语资料范围。"
    if any(token.lower() in name for token in ANSWER_TOKENS):
        return "answer", 0.98, "文件名含答案/解析标识。"
    if any(token.lower() in name for token in PROMPT_TOKENS):
        return "prompt", 0.96, "文件名含原卷/空白卷/学生版标识。"
    if any(token.lower() in relative for token in TEACHING_TOKENS):
        return "teaching", 0.90, "路径显示为教材、讲义或解题方法资料。"
    if row["media_kind"] in {"document", "pdf"}:
        return "other", 0.55, "文件名无明确版本标识，保留为待判定文本。"
    return "other", 0.50, "未匹配专用规则。"


def _logical_key(row: sqlite3.Row) -> tuple[str, str]:
    stem = unicodedata.normalize("NFKC", Path(row["file_name"]).stem).lower()

    def remove_variant(match: re.Match[str]) -> str:
        body = match.group(1)
        return "" if any(token.lower() in body for token in VARIANT_TOKENS) else body

    stem = re.sub(r"[\(\uff08\[\u3010]([^\)\uff09\]\u3011]{0,40})[\)\uff09\]\u3011]", remove_variant, stem)
    for token in VARIANT_TOKENS:
        stem = stem.replace(token.lower(), "")
    stem = stem.replace("英文", "英语").replace("真题", "试卷")
    stem = re.sub(r"(新课标\s*(?:i{1,3}|[123]))卷\b", r"\1", stem, flags=re.I)
    stem = re.sub(r"(?:\u542c\u529b)?(?:\u6750\u6599|\u539f\u6587|\u6587\u672c)$", "", stem)
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", stem)
    if len(normalized) < 6:
        parent = Path(row["relative_path"]).parent.name
        normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", parent).lower()) + normalized
    return normalized[:240], _text(stem) or Path(row["file_name"]).stem


def _content_kind(rows: list[tuple[sqlite3.Row, str, float, str]]) -> str:
    roles = {item[1] for item in rows}
    joined = " ".join(_text(item[0]["relative_path"]).lower() for item in rows)
    if "teaching" in roles:
        return "teaching_material" if roles <= {"teaching", "other"} else "mixed"
    if roles == {"audio"}:
        return "audio"
    if "answer" in roles and "prompt" not in roles and len(roles) == 1:
        return "answer_material"
    if any(token in joined for token in ("高考", "模拟", "一模", "二模", "月考", "周测", "真题", "exam")):
        return "exam"
    if any(token in joined for token in ("习题", "练习", "分类汇编", "exercise")):
        return "exercise"
    return "other"


def _pairing_status(roles: set[str], prompt_id: str | None, answer_id: str | None) -> str:
    if prompt_id and answer_id and prompt_id != answer_id:
        return "paired"
    if prompt_id and answer_id and prompt_id == answer_id:
        return "mixed_single_file"
    if prompt_id:
        return "prompt_only"
    if answer_id:
        return "answer_only"
    if roles == {"audio"}:
        return "audio_only"
    return "needs_review"


def _preferred(items: Iterable[sqlite3.Row]) -> sqlite3.Row | None:
    rows = list(items)
    if not rows:
        return None
    extension_rank = {".docx": 0, ".doc": 1, ".pdf": 2, ".txt": 3, ".md": 4, ".mp3": 5, ".wav": 6}
    status_rank = {"ingested": 0, "structured": 1, "extracted": 2, "indexed": 3}
    rows.sort(key=lambda row: (
        status_rank.get(row["parse_status"], 8),
        extension_rank.get(row["extension"], 9),
        -int(row["extracted_char_count"] or 0),
        len(row["relative_path"]),
    ))
    return rows[0]


def pair_library_sources(conn, *, library_key: str = "english_library") -> dict[str, Any]:
    rows = list(conn.execute(
        """
        SELECT * FROM library_resources
        WHERE library_key=? AND subject_scope='english' AND is_canonical=1
          AND parse_status NOT IN ('excluded_non_english','failed','needs_review')
        ORDER BY relative_path
        """,
        (library_key,),
    ))
    grouped: dict[str, list[tuple[sqlite3.Row, str, float, str]]] = defaultdict(list)
    titles: dict[str, str] = {}
    for row in rows:
        key, title = _logical_key(row)
        if not key:
            key = row["resource_id"].lower()
        role, confidence, rationale = _resource_role(row)
        grouped[key].append((row, role, confidence, rationale))
        titles.setdefault(key, title)

    now = utc_now()
    seen: set[str] = set()
    pair_counts: Counter[str] = Counter()
    for key, items in grouped.items():
        source_set_id = stable_id("SET", library_key, key, length=24)
        seen.add(source_set_id)
        role_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row, role, _, _ in items:
            role_rows[role].append(row)
        prompt = _preferred(role_rows["prompt"])
        answer = _preferred(role_rows["answer"])
        if prompt is None:
            prompt = _preferred(role_rows["other"] + role_rows["teaching"])
        # Many analysis/answer files contain a complete copy of the prompt.
        if prompt is None and answer is not None and answer["extracted_text_path"]:
            prompt = answer
        if answer is None and prompt is not None:
            prompt_text = _safe_read(prompt["extracted_text_path"])
            if any(token in prompt_text for token in ("【答案】", "参考答案", "答案解析")):
                answer = prompt
        audio = _preferred(role_rows["audio"])
        roles = {item[1] for item in items}
        status = _pairing_status(roles, prompt["resource_id"] if prompt else None, answer["resource_id"] if answer else None)
        pair_counts[status] += 1
        year_values = [int(item[0]["year_hint"]) for item in items if item[0]["year_hint"]]
        title = titles[key]
        exam_type = next((token for token in ("一模", "二模", "高考", "周测", "月考") if token in title), None)
        region_match = re.search(r"(?:\uff08|\()([^()\uff08\uff09]{2,12})(?:\uff09|\))", Path(items[0][0]["file_name"]).stem)
        conn.execute(
            """
            INSERT INTO library_source_sets(
              source_set_id,library_key,logical_key,title,year_hint,exam_type,region_hint,content_kind,
              pairing_status,preferred_prompt_resource_id,preferred_answer_resource_id,
              preferred_audio_resource_id,resource_count,parser_version,verification_status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'suggested',?,?)
            ON CONFLICT(source_set_id) DO UPDATE SET
              title=excluded.title,year_hint=excluded.year_hint,exam_type=excluded.exam_type,
              region_hint=excluded.region_hint,content_kind=excluded.content_kind,
              pairing_status=excluded.pairing_status,
              preferred_prompt_resource_id=excluded.preferred_prompt_resource_id,
              preferred_answer_resource_id=excluded.preferred_answer_resource_id,
              preferred_audio_resource_id=excluded.preferred_audio_resource_id,
              resource_count=excluded.resource_count,parser_version=excluded.parser_version,updated_at=excluded.updated_at
            """,
            (
                source_set_id, library_key, key, title, min(year_values) if year_values else None,
                exam_type, region_match.group(1) if region_match else None, _content_kind(items), status,
                prompt["resource_id"] if prompt else None, answer["resource_id"] if answer else None,
                audio["resource_id"] if audio else None, len(items), PARSER_VERSION, now, now,
            ),
        )
        for row, role, confidence, rationale in items:
            conn.execute(
                """
                INSERT INTO library_source_set_resources(
                  source_set_id,resource_id,resource_role,role_confidence,rationale,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(source_set_id,resource_id) DO UPDATE SET
                  resource_role=excluded.resource_role,role_confidence=excluded.role_confidence,
                  rationale=excluded.rationale,updated_at=excluded.updated_at
                """,
                (source_set_id, row["resource_id"], role, confidence, rationale, now, now),
            )
    if seen:
        placeholders = ",".join("?" for _ in seen)
        conn.execute(
            f"DELETE FROM library_source_sets WHERE library_key=? AND source_set_id NOT IN ({placeholders})",
            (library_key, *sorted(seen)),
        )
    conn.commit()
    return {"resources_considered": len(rows), "source_sets": len(grouped), "pairing_status": dict(pair_counts)}


def _safe_read(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _chunks(text: str, target: int = 1800, overlap: int = 180) -> Iterable[tuple[int, int, str, str]]:
    paragraphs = [(m.start(), m.end(), m.group(0).strip()) for m in re.finditer(r"[^\n]+(?:\n|$)", text) if m.group(0).strip()]
    if not paragraphs and text.strip():
        paragraphs = [(0, len(text), text.strip())]
    index = 0
    cursor = 0
    while cursor < len(paragraphs):
        start = paragraphs[cursor][0]
        end = paragraphs[cursor][1]
        parts = [paragraphs[cursor][2]]
        next_cursor = cursor + 1
        while next_cursor < len(paragraphs) and paragraphs[next_cursor][1] - start <= target:
            end = paragraphs[next_cursor][1]
            parts.append(paragraphs[next_cursor][2])
            next_cursor += 1
        chunk_text = "\n".join(parts).strip()
        heading = next((part for part in parts[:3] if len(part) <= 120 and _looks_like_heading(part)), "")
        if chunk_text:
            yield start, end, heading, chunk_text
            index += 1
        if next_cursor >= len(paragraphs):
            break
        overlap_start = max(start, end - overlap)
        cursor = next_cursor
        while cursor > 0 and paragraphs[cursor - 1][0] >= overlap_start:
            cursor -= 1
        if paragraphs[cursor][0] <= start:
            cursor = next_cursor


def _looks_like_heading(line: str) -> bool:
    value = _text(line)
    if not value or len(value) > 160:
        return False
    lower = value.lower()
    return any(re.search(pattern, lower, re.I) for hints, _, _ in HEADING_HINTS for pattern in hints) or bool(
        re.match(r"^(?:part|section|\u7b2c[\u4e00-\u5341\d]+(?:\u90e8\u5206|\u8282|\u7ae0))", lower, re.I)
    )


def _heading_info(line: str) -> tuple[str, str] | None:
    value = _text(line)
    if not value or len(value) > 220:
        return None
    lower = value.lower()
    for hints, qtype, code in HEADING_HINTS:
        if any(re.search(pattern, lower, re.I) for pattern in hints):
            return qtype, value
    return None


def _infer_question_type(section_type: str, stem: str) -> str:
    if any(token in stem for token in ("假设你是", "假定你是", "请你用英文", "读后续写", "写一封", "回复邮件")):
        return "写作"
    if section_type:
        return section_type
    low = stem.lower()
    if BLANK_RE.search(stem) or re.search(r"\([a-z]{2,20}\)", low):
        return "语法填空"
    if re.search(r"\b(?:what|why|which|who|when|where|how)\b", low) and "?" in stem:
        return "阅读理解"
    if OPTION_RE.search(stem):
        return "选择题"
    return "其他"


def _parse_options(text: str, labels: str = "ABCDEFGHIJK") -> list[dict[str, str]]:
    normalized = re.sub(rf"(?<!\s)([{labels}])\.", r" \1.", text)
    marks = list(re.finditer(rf"(?:^|\s)([{labels}])\s*[.．、\)]\s*", normalized))
    options: list[dict[str, str]] = []
    for position, mark in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else len(normalized)
        value = _text(normalized[mark.end() : end])[:1000]
        if value:
            options.append({"label": mark.group(1), "text": value})
    return options


def _answer_maps(text: str) -> tuple[dict[int, str], dict[int, str]]:
    lines = [_text(line) for line in text.splitlines() if _text(line)]
    answers: dict[int, str] = {}
    explanations: dict[int, str] = {}
    current_num: int | None = None
    answer_context = 0
    for idx, line in enumerate(lines):
        is_detail_line = bool(DETAIL_RE.match(line))
        contains_answer_marker = bool(re.search("(?:\\u3010\\u7b54\\u6848\\u3011|\\u53c2\\u8003\\u7b54\\u6848|\\u7b54\\u6848\\s*[:\\uff1a])", line))
        if contains_answer_marker:
            answer_context = 3
        number_marks = list(re.finditer(r"(?<!\d)(\d{1,3})\s*[.\uff0e\u3001]\s*", line))
        parsed_pairs: dict[int, str] = {}
        if number_marks and not is_detail_line and (contains_answer_marker or answer_context > 0 or len(number_marks) >= 3):
            for position, number_mark in enumerate(number_marks):
                end = number_marks[position + 1].start() if position + 1 < len(number_marks) else len(line)
                value = line[number_mark.end() : end].strip(" ,;，；")
                if value and len(value) <= 1000:
                    parsed_pairs[int(number_mark.group(1))] = value
            for number, value in parsed_pairs.items():
                answers[number] = value
        match = INLINE_ANSWER_RE.match(line)
        if match:
            current_num = int(match.group(1))
            answers.setdefault(current_num, match.group(2)[:1000])
            answer_context = max(0, answer_context - 1)
            continue
        qmatch = QUESTION_LINE_RE.match(line)
        if qmatch and not contains_answer_marker and answer_context <= 0 and not line.startswith(("【", "答案")):
            current_num = int(qmatch.group(1))
        marker = re.search(r"(?:\u3010\u7b54\u6848\u3011|\u53c2\u8003\u7b54\u6848|\u7b54\u6848\s*[:\uff1a])\s*(.+)", line)
        if marker and current_num is not None and not parsed_pairs:
            value = marker.group(1).strip()
            if value:
                answers.setdefault(current_num, value[:1000])
        detail = DETAIL_RE.match(line)
        if detail:
            number = int(detail.group(1))
            pieces = [detail.group(2)] if detail.group(2) else []
            for following in lines[idx + 1 : idx + 8]:
                if QUESTION_LINE_RE.match(following) or INLINE_ANSWER_RE.match(following) or DETAIL_RE.match(following):
                    break
                pieces.append(following)
            explanations[number] = "\n".join(piece for piece in pieces if piece)[:6000]

        for range_match in re.finditer(r"(\d{1,3})\s*[-~]\s*(\d{1,3})\s*[.\uff0e\u3001]?\s*([A-K]{2,})", line):
            start, end, letters = int(range_match.group(1)), int(range_match.group(2)), range_match.group(3)
            if end >= start and len(letters) >= end - start + 1:
                for offset, number in enumerate(range(start, end + 1)):
                    answers.setdefault(number, letters[offset])
        answer_context = max(0, answer_context - 1)
    return answers, explanations


def _question_candidates(text: str) -> list[dict[str, Any]]:
    raw_lines = text.splitlines()
    lines = [_text(line) for line in raw_lines]
    candidates: list[dict[str, Any]] = []
    section_type = ""
    section = ""
    last_heading_index = 0
    seen: set[tuple[str, str]] = set()
    writing_prompt_active = False
    for idx, line in enumerate(lines):
        if not line:
            continue
        heading = _heading_info(line)
        if heading:
            section_type, section = heading
            last_heading_index = idx
            writing_prompt_active = False
            continue
        if INLINE_ANSWER_RE.match(line) or DETAIL_RE.match(line) or re.match(r"^(?:\u3010?\u7b54\u6848\u3011?|\u3010?\u89e3\u6790\u3011?)", line):
            continue
        blank_numbers = [int(value) for value in BLANK_RE.findall(line)]
        if blank_numbers:
            for number in blank_numbers:
                key = (str(number), section)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({
                    "number": number, "line_index": idx, "heading_index": last_heading_index,
                    "section": section or "语法填空", "question_type": section_type or "语法填空",
                    "stem": line[:6000], "options": [],
                })
            continue
        match = QUESTION_LINE_RE.match(line)
        if not match:
            match = re.match(r"^\s*(\d{1,3})\s+(.+[?\uff1f])\s*$", line)
        if not match:
            continue
        number = int(match.group(1))
        tail = match.group(2).strip()
        if writing_prompt_active and not re.search(r"[?？]", tail):
            continue
        if number > 150 or len(tail) < 3 or re.match(r"^(?:[A-K]|[A-K][.\uff0e])$", tail, re.I):
            continue
        if not section and any(token in tail for token in ("答题前", "考生", "试卷", "条形码", "作图")):
            continue
        if any(tail.startswith(token) for token in ("【答案】", "【解析】", "答案", "解析", "考点")):
            continue
        key = (str(number), section)
        if key in seen:
            found_options = _parse_options(tail)
            if found_options:
                existing = next((item for item in reversed(candidates) if item["number"] == number and item["section"] == section), None)
                if existing is not None:
                    existing["options"] = found_options
            continue
        seen.add(key)
        pieces = [tail]
        for following in lines[idx + 1 : idx + 8]:
            if not following:
                continue
            if QUESTION_LINE_RE.match(following) or INLINE_ANSWER_RE.match(following) or DETAIL_RE.match(following) or _heading_info(following):
                break
            if re.match(r"^(?:\u3010?\u7b54\u6848\u3011?|\u3010?\u89e3\u6790\u3011?)", following):
                break
            pieces.append(following)
            if sum(len(piece) for piece in pieces) > 3500:
                break
        block = " ".join(pieces)[:6000]
        options = _parse_options(block)
        inferred_type = _infer_question_type(section_type, block)
        candidates.append({
            "number": number, "line_index": idx, "heading_index": last_heading_index,
            "section": section, "question_type": inferred_type,
            "stem": block, "options": options,
        })
        if inferred_type == "写作":
            writing_prompt_active = True
    candidates.sort(key=lambda item: (item["line_index"], item["number"]))
    gap_candidates = [item for item in candidates if item["question_type"] == "六四/七五选句"]
    for position, first in enumerate(gap_candidates):
        if first["options"]:
            continue
        if position > 0 and gap_candidates[position - 1]["section"] == first["section"]:
            continue
        group = [item for item in gap_candidates if item["section"] == first["section"] and abs(item["line_index"] - first["line_index"]) <= 80]
        start = max(item["line_index"] for item in group) + 1
        bank: list[dict[str, str]] = []
        for option_line in lines[start : start + 30]:
            if _heading_info(option_line):
                break
            option_match = re.match(r"^([A-G])\s*[.．、\)]\s*(.+)$", option_line)
            if option_match:
                bank.append({"label": option_match.group(1), "text": option_match.group(2)[:1000]})
        if len(bank) >= 4:
            for item in group:
                item["options"] = bank
    return candidates


def _passage_groups(candidates: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    needs_passage = {"语法填空", "阅读理解", "阅读简答", "完形填空", "选词填空", "六四/七五选句", "概要写作"}
    limits = {"语法填空": 10, "阅读理解": 5, "阅读简答": 4, "完形填空": 20, "选词填空": 10, "六四/七五选句": 6, "概要写作": 1}
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["question_type"] not in needs_passage:
            if current:
                groups.append(current)
                current = []
            continue
        if current and (
            candidate["question_type"] != current[-1]["question_type"]
            or candidate["section"] != current[-1]["section"]
            or candidate["number"] - current[-1]["number"] > 2
            or len(current) >= limits.get(candidate["question_type"], 5)
        ):
            groups.append(current)
            current = []
        current.append(candidate)
    if current:
        groups.append(current)
    return groups


def _extract_passage(text: str, group: list[dict[str, Any]], previous_line: int) -> str:
    lines = [_text(line) for line in text.splitlines()]
    first = group[0]
    start = max(first["heading_index"] + 1, previous_line, first["line_index"] - 80)
    end = first["line_index"] + 1
    pieces: list[str] = []
    for line in lines[start:end]:
        if not line or _heading_info(line) or line.lower().startswith("directions:"):
            continue
        if QUESTION_LINE_RE.match(line) and not BLANK_RE.search(line):
            continue
        if len(OPTION_RE.findall(line)) >= 2:
            continue
        pieces.append(line)
    result = "\n".join(pieces).strip()
    return result[-12000:]


def _knowledge_specs(question_type: str, stem: str, answer: str, explanation: str) -> list[tuple[str, str, float, str]]:
    combined = " ".join((question_type, stem, answer, explanation))
    specs: list[tuple[str, str, float, str]] = []

    def add(code: str, role: str, confidence: float, rationale: str) -> None:
        if not any(item[0] == code and item[1] == role for item in specs):
            specs.append((code, role, confidence, rationale))

    if question_type == "语法填空":
        add("grammar", "prerequisite", 0.95, "题型识别为语法填空。")
        for keyword, code in GRAMMAR_KEYWORDS:
            if keyword in explanation:
                add(code, "primary", 0.84, f"来源解析包含“{keyword}”，规则映射待核验。")
        answer_lower = answer.lower().strip()
        if re.search(r"\b(?:is|are|was|were|has|have|had|will|would|did|does)\b", answer_lower):
            add("tense", "secondary", 0.62, "答案形态含有限定动词时态线索。")
        if re.search(r"\b(?:is|are|was|were|been|being)\s+\w+(?:ed|en)\b", answer_lower):
            add("passive_voice", "secondary", 0.68, "答案形态匹配被动语态模式。")
        if answer_lower in {"a", "an", "the"}:
            add("article", "primary", 0.78, "答案为冠词。")
        if answer_lower in {"that", "which", "who", "whom", "whose", "where", "when"}:
            add("connector_function_clause_completeness", "prerequisite", 0.76, "连接词需结合从句成分完整性判断。")
            add("relative_clause", "secondary", 0.58, "答案形式可能引导定语从句，需回源确认。")
        if answer_lower.endswith("ing"):
            add("present_participle", "secondary", 0.58, "-ing 形式为分词/动名词候选。")
            add("non_finite_logical_subject", "prerequisite", 0.55, "需核对非谓语逻辑主语。")
        if re.search(r"\bto\s+\w+", answer_lower):
            add("infinitive", "secondary", 0.64, "答案为 to do 形式。")
        if re.search(r"\w+(?:ed|en)$", answer_lower):
            add("past_participle", "secondary", 0.55, "答案呈过去分词形式，需回源排除谓语。")
    elif question_type in {"阅读理解", "阅读简答", "六四/七五选句"}:
        add("reading", "prerequisite", 0.95, "题型识别为阅读理解。")
        low = stem.lower()
        if any(token in low for token in ("main idea", "best title", "mainly about")):
            add("reading_main_idea", "primary", 0.82, "题干词语指向主旨/标题题。")
        elif any(token in low for token in ("infer", "imply", "suggest")):
            add("reading_inference", "primary", 0.82, "题干词语指向推理判断。")
        elif any(token in low for token in ("purpose", "attitude", "tone")):
            add("reading_purpose_attitude", "primary", 0.82, "题干词语指向目的/态度。")
        else:
            add("reading_detail", "primary", 0.68, "默认作为细节证据定位候选，待核验。")
    elif question_type == "完形填空":
        add("cloze", "prerequisite", 0.95, "题型识别为完形填空。")
        add("cloze_context_semantics", "primary", 0.76, "完形需通过上下文判断词义。")
        add("cloze_logic", "prerequisite", 0.66, "句际逻辑是完形的通用证据。")
    elif question_type == "听力理解":
        add("listening", "prerequisite", 0.95, "题型识别为听力理解。")
        add("listening_detail", "primary", 0.70, "听力题通用细节定位候选，待核验。")
    elif question_type == "汉译英":
        add("translation_sentence_structure", "primary", 0.86, "翻译需先构建完整句子主干。")
        add("translation_grammar_accuracy", "secondary", 0.76, "时态、语态和从句组织共同影响翻译得分。")
    elif question_type in {"写作", "概要写作"}:
        add("writing_task_fulfillment", "primary", 0.88, "写作首先受任务完成度约束。")
        add("writing_organization", "secondary", 0.76, "需组织信息与段落功能。")
    return specs


def structure_library(
    conn,
    *,
    library_key: str = "english_library",
    limit: int = 0,
) -> dict[str, Any]:
    pairing = pair_library_sources(conn, library_key=library_key)
    sql = """
      SELECT s.*,
             p.extracted_text_path prompt_text_path,p.absolute_path prompt_path,p.parse_status prompt_status,
             a.extracted_text_path answer_text_path,a.absolute_path answer_path
      FROM library_source_sets s
      LEFT JOIN library_resources p ON p.resource_id=s.preferred_prompt_resource_id
      LEFT JOIN library_resources a ON a.resource_id=s.preferred_answer_resource_id
      WHERE s.library_key=?
      ORDER BY CASE WHEN p.extracted_text_path IS NOT NULL THEN 0 ELSE 1 END,
               s.year_hint DESC,s.title,s.source_set_id
    """
    params: list[Any] = [library_key]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    sets = list(conn.execute(sql, params))
    run_id = stable_id("RUN", library_key, "structure", utc_now(), length=24)
    conn.execute(
        """
        INSERT INTO library_parse_runs(
          parse_run_id,library_key,mode,status,total_resources,started_at,options_json
        ) VALUES (?,?,'structure','running',?,?,?)
        """,
        (run_id, library_key, len(sets), utc_now(), json.dumps({"limit": limit, "parser_version": PARSER_VERSION}, ensure_ascii=False)),
    )
    conn.commit()
    knowledge_ids = {row["code"]: row["knowledge_point_id"] for row in conn.execute("SELECT code,knowledge_point_id FROM knowledge_points")}
    total_questions = total_passages = total_chunks = processed = failed = 0
    type_counts: Counter[str] = Counter()
    for source_set in sets:
        set_id = source_set["source_set_id"]
        prompt_text = _safe_read(source_set["prompt_text_path"])
        answer_text = _safe_read(source_set["answer_text_path"])
        if not answer_text and source_set["preferred_answer_resource_id"] == source_set["preferred_prompt_resource_id"]:
            answer_text = prompt_text
        conn.execute("SAVEPOINT structure_one")
        try:
            conn.execute("DELETE FROM library_structure_reviews WHERE source_set_id=?", (set_id,))
            conn.execute("DELETE FROM staged_questions WHERE source_set_id=?", (set_id,))
            conn.execute("DELETE FROM staged_passages WHERE source_set_id=?", (set_id,))

            resource_rows = list(conn.execute(
                """
                SELECT r.* FROM library_source_set_resources m
                JOIN library_resources r ON r.resource_id=m.resource_id
                WHERE m.source_set_id=? AND r.extracted_text_path IS NOT NULL AND r.is_canonical=1
                """,
                (set_id,),
            ))
            preferred_ids = {
                value for value in (
                    source_set["preferred_prompt_resource_id"], source_set["preferred_answer_resource_id"]
                ) if value
            }
            chunk_resources = [resource for resource in resource_rows if resource["resource_id"] in preferred_ids]
            set_chunks = 0
            for resource in chunk_resources:
                resource_text = _safe_read(resource["extracted_text_path"])
                conn.execute(
                    "DELETE FROM library_text_chunks WHERE resource_id=? AND parser_version=?",
                    (resource["resource_id"], PARSER_VERSION),
                )
                for chunk_index, (start, end, heading, chunk_text) in enumerate(_chunks(resource_text)):
                    chunk_id = stable_id("CHK", resource["resource_id"], PARSER_VERSION, chunk_index, length=24)
                    conn.execute(
                        """
                        INSERT INTO library_text_chunks(
                          chunk_id,resource_id,source_set_id,chunk_index,char_start,char_end,heading,chunk_text,
                          token_estimate,parser_version,verification_status,source_locator,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,'suggested',?,?,?)
                        """,
                        (
                            chunk_id, resource["resource_id"], set_id, chunk_index, start, end, heading or None,
                            chunk_text, max(1, len(chunk_text) // 4), PARSER_VERSION,
                            f"{resource['absolute_path']}#chars={start}-{end}", utc_now(), utc_now(),
                        ),
                    )
                    set_chunks += 1

            candidates = _question_candidates(prompt_text) if prompt_text else []
            answers, explanations = _answer_maps(answer_text)
            passages_by_question: dict[int, str] = {}
            passage_count = 0
            previous_line = 0
            for group_index, group in enumerate(_passage_groups(candidates), start=1):
                passage_text = _extract_passage(prompt_text, group, previous_line)
                previous_line = group[-1]["line_index"] + 1
                if len(passage_text) < 80:
                    continue
                passage_id = stable_id("CPAS", set_id, PARSER_VERSION, group_index, length=24)
                first_line = passage_text.splitlines()[0]
                title = first_line if len(first_line) <= 120 and not BLANK_RE.search(first_line) else None
                conn.execute(
                    """
                    INSERT INTO staged_passages(
                      candidate_passage_id,source_set_id,prompt_resource_id,passage_type,title,passage_text,
                      original_number_range,word_count,source_locator,parser_version,confidence,
                      verification_status,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'suggested',?,?)
                    """,
                    (
                        passage_id, set_id, source_set["preferred_prompt_resource_id"], group[0]["question_type"],
                        title, passage_text, f"{group[0]['number']}-{group[-1]['number']}",
                        len(re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", passage_text)),
                        f"{source_set['prompt_path']}#line={max(1, group[0]['heading_index'] + 1)}",
                        PARSER_VERSION, 0.62, utc_now(), utc_now(),
                    ),
                )
                passage_count += 1
                for candidate in group:
                    passages_by_question[id(candidate)] = passage_id

            question_count = 0
            mapping_count = 0
            for ordinal, candidate in enumerate(candidates, start=1):
                number = candidate["number"]
                answer = answers.get(number, "")
                explanation = explanations.get(number, "")
                qtype = candidate["question_type"]
                type_counts[qtype] += 1
                issues: list[str] = []
                if not answer:
                    issues.append("missing_answer")
                expected_options = qtype in {"听力理解", "阅读理解", "完形填空", "选择题", "六四/七五选句"}
                if expected_options and len(candidate["options"]) < 2:
                    issues.append("missing_options")
                passage_id = passages_by_question.get(id(candidate))
                if qtype in {"语法填空", "阅读理解", "阅读简答", "完形填空", "选词填空", "六四/七五选句", "概要写作"} and not passage_id:
                    issues.append("missing_passage")
                confidence = max(0.30, 0.82 - 0.14 * len(issues))
                verification = "needs_check" if issues else "suggested"
                question_id = stable_id("CQ", set_id, PARSER_VERSION, ordinal, number, length=24)
                locator = f"{source_set['prompt_path']}#line={candidate['line_index'] + 1}"
                answer_locator = f"{source_set['answer_path']}#question={number}" if source_set["answer_path"] else None
                conn.execute(
                    """
                    INSERT INTO staged_questions(
                      candidate_question_id,source_set_id,candidate_passage_id,prompt_resource_id,
                      answer_resource_id,source_ordinal,original_number,section,question_type,stem,answer,
                      explanation_raw,options_json,primary_test_point,secondary_test_points_json,difficulty,
                      content_tags_json,source_locator,answer_locator,parser_version,confidence,
                      verification_status,review_reasons_json,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        question_id, set_id, passage_id, source_set["preferred_prompt_resource_id"],
                        source_set["preferred_answer_resource_id"], ordinal, str(number), candidate["section"],
                        qtype, candidate["stem"], answer or None, explanation or None,
                        json.dumps(candidate["options"], ensure_ascii=False), None, "[]", "待校准", "[]",
                        locator, answer_locator, PARSER_VERSION, confidence, verification,
                        json.dumps(issues, ensure_ascii=False), utc_now(), utc_now(),
                    ),
                )
                question_count += 1
                for code, role, confidence_value, rationale in _knowledge_specs(qtype, candidate["stem"], answer, explanation):
                    knowledge_id = knowledge_ids.get(code)
                    if not knowledge_id:
                        continue
                    conn.execute(
                        """
                        INSERT INTO staged_question_knowledge_map(
                          candidate_question_id,knowledge_point_id,role,mapping_source,confidence,
                          verification_status,rationale,source_locator,created_at,updated_at
                        ) VALUES (?,?,?,'rule',?,'suggested',?,?,?,?)
                        """,
                        (question_id, knowledge_id, role, confidence_value, rationale, locator, utc_now(), utc_now()),
                    )
                    mapping_count += 1
                for issue in issues:
                    review_id = stable_id("LREV", question_id, issue, length=24)
                    detail = {
                        "missing_answer": "未从当前答案材料稳定匹配到答案。",
                        "missing_options": "选择类题目未稳定拆出至少两个选项。",
                        "missing_passage": "篇章型题目未得到足够长的完整语篇候选。",
                    }[issue]
                    conn.execute(
                        """
                        INSERT INTO library_structure_reviews(
                          review_id,source_set_id,candidate_question_id,resource_id,problem_type,severity,
                          detail,source_locator,status,created_at
                        ) VALUES (?,?,?,?,?,'warning',?,?,'open',?)
                        """,
                        (review_id, set_id, question_id, source_set["preferred_prompt_resource_id"], issue, detail, locator, utc_now()),
                    )

            conn.execute(
                """
                UPDATE library_source_sets SET question_candidate_count=?,passage_candidate_count=?,
                  chunk_count=?,parser_version=?,verification_status=CASE
                    WHEN verification_status IN ('verified','source_checked') THEN verification_status
                    WHEN ? > 0 THEN 'needs_check' ELSE 'suggested' END,
                  updated_at=? WHERE source_set_id=?
                """,
                (question_count, passage_count, set_chunks, PARSER_VERSION, question_count, utc_now(), set_id),
            )
            for resource in resource_rows:
                counts = conn.execute(
                    """
                    SELECT COUNT(DISTINCT q.candidate_question_id) questions,
                           COUNT(DISTINCT q.candidate_passage_id) passages,
                           COUNT(DISTINCT m.knowledge_point_id || ':' || m.role) mappings
                    FROM staged_questions q
                    LEFT JOIN staged_question_knowledge_map m ON m.candidate_question_id=q.candidate_question_id
                    WHERE q.source_set_id=?
                    """,
                    (set_id,),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE library_resources SET
                      parse_status=CASE WHEN parse_status='ingested' THEN 'ingested' ELSE 'structured' END,
                      question_count=?,passage_count=?,knowledge_mapping_count=?,
                      verification_status=CASE WHEN verification_status IN ('verified','source_checked')
                        THEN verification_status ELSE 'needs_check' END,
                      updated_at=? WHERE resource_id=?
                    """,
                    (counts["questions"], counts["passages"], counts["mappings"], utc_now(), resource["resource_id"]),
                )
            total_questions += question_count
            total_passages += passage_count
            total_chunks += set_chunks
            processed += 1
            conn.execute("RELEASE SAVEPOINT structure_one")
        except (OSError, ValueError, sqlite3.Error) as exc:
            conn.execute("ROLLBACK TO SAVEPOINT structure_one")
            conn.execute("RELEASE SAVEPOINT structure_one")
            failed += 1
            conn.execute(
                """
                INSERT OR REPLACE INTO library_structure_reviews(
                  review_id,source_set_id,problem_type,severity,detail,source_locator,status,created_at
                ) VALUES (?,?,'structure_failed','error',?,?,'open',?)
                """,
                (stable_id("LREV", set_id, "structure_failed", length=24), set_id, str(exc)[:2000], source_set["prompt_path"] or source_set["title"], utc_now()),
            )
        if (processed + failed) % 25 == 0:
            conn.execute(
                "UPDATE library_parse_runs SET processed_resources=?,successful_resources=?,failed_resources=? WHERE parse_run_id=?",
                (processed + failed, processed, failed, run_id),
            )
            conn.commit()
    status = "completed_with_errors" if failed else "completed"
    summary = {
        "parser_version": PARSER_VERSION,
        "source_pairing": pairing,
        "source_sets_processed": processed,
        "failed_source_sets": failed,
        "text_chunks": total_chunks,
        "question_candidates": total_questions,
        "passage_candidates": total_passages,
        "question_types": dict(type_counts),
        "verification_rule": "自动结构化和知识映射仅为suggested/needs_check，不自动升级为source_checked或verified。",
    }
    conn.execute(
        """
        UPDATE library_parse_runs SET status=?,processed_resources=?,successful_resources=?,failed_resources=?,
          finished_at=?,summary_json=? WHERE parse_run_id=?
        """,
        (status, processed + failed, processed, failed, utc_now(), json.dumps(summary, ensure_ascii=False), run_id),
    )
    completed_resources = conn.execute(
        """
        SELECT COUNT(*) FROM library_resources WHERE library_key=? AND subject_scope='english'
          AND parse_status IN ('structured','ingested')
        """,
        (library_key,),
    ).fetchone()[0]
    total_resources = conn.execute(
        """
        SELECT COUNT(*) FROM library_resources
        WHERE library_key=? AND subject_scope='english'
          AND extension NOT IN ('.mp3','.wav','.m4a','.aac','.flac','.ogg')
        """,
        (library_key,),
    ).fetchone()[0]
    conn.execute(
        """
        UPDATE project_work_items SET completed_units=?,total_units=?,status=CASE
          WHEN ? >= ? AND ? > 0 THEN 'completed' ELSE 'in_progress' END,
          evidence_path='exports/library_structure_current.json',updated_at=?
        WHERE work_item_id='WORK-FULL-PARSE'
        """,
        (completed_resources, total_resources, completed_resources, total_resources, total_resources, utc_now()),
    )
    conn.commit()
    return {"parse_run_id": run_id, **summary}


def structure_summary(conn, *, library_key: str = "english_library") -> dict[str, Any]:
    source_sets = dict(conn.execute(
        """
        SELECT COUNT(*) source_sets,
               SUM(question_candidate_count) question_candidates,
               SUM(passage_candidate_count) passage_candidates,
               SUM(chunk_count) text_chunks,
               SUM(CASE WHEN pairing_status='paired' THEN 1 ELSE 0 END) paired_sets,
               SUM(CASE WHEN pairing_status='audio_only' THEN 1 ELSE 0 END) audio_only_sets
        FROM library_source_sets WHERE library_key=?
        """,
        (library_key,),
    ).fetchone())
    by_pairing = [dict(row) for row in conn.execute(
        "SELECT pairing_status,COUNT(*) source_sets FROM library_source_sets WHERE library_key=? GROUP BY pairing_status ORDER BY source_sets DESC",
        (library_key,),
    )]
    by_type = [dict(row) for row in conn.execute(
        "SELECT question_type,COUNT(*) question_candidates FROM staged_questions GROUP BY question_type ORDER BY question_candidates DESC"
    )]
    review = dict(conn.execute(
        """
        SELECT COUNT(*) open_reviews,
               SUM(CASE WHEN problem_type='missing_answer' THEN 1 ELSE 0 END) missing_answers,
               SUM(CASE WHEN problem_type='missing_options' THEN 1 ELSE 0 END) missing_options,
               SUM(CASE WHEN problem_type='missing_passage' THEN 1 ELSE 0 END) missing_passages
        FROM library_structure_reviews WHERE status='open'
        """
    ).fetchone())
    candidate_quality = dict(conn.execute(
        """
        SELECT COUNT(*) total_candidates,
               SUM(CASE WHEN answer IS NOT NULL AND trim(answer)<>'' THEN 1 ELSE 0 END) with_answer,
               SUM(CASE WHEN candidate_passage_id IS NOT NULL THEN 1 ELSE 0 END) with_passage,
               SUM(CASE WHEN verification_status='suggested' THEN 1 ELSE 0 END) clean_suggestions,
               SUM(CASE WHEN verification_status='needs_check' THEN 1 ELSE 0 END) needs_check,
               (SELECT COUNT(*) FROM staged_question_knowledge_map) knowledge_mappings
        FROM staged_questions
        """
    ).fetchone())
    total_candidates = int(candidate_quality.get("total_candidates") or 0)
    candidate_quality["answer_coverage"] = round(
        int(candidate_quality.get("with_answer") or 0) / total_candidates, 4
    ) if total_candidates else 0.0
    return {
        "parser_version": PARSER_VERSION,
        "source_sets": source_sets,
        "by_pairing_status": by_pairing,
        "question_types": by_type,
        "candidate_quality": candidate_quality,
        "review_queue": review,
        "verification_boundary": "候选题与规则知识点均需回源审核；未审核不进入source_checked题库。",
        "generated_at": utc_now(),
    }


def search_library_chunks(conn, query: str, *, limit: int = 30) -> dict[str, Any]:
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    if not terms:
        return {"query": query, "count": 0, "items": []}
    where = " AND ".join("c.chunk_text LIKE ?" for _ in terms)
    params: list[Any] = [f"%{term}%" for term in terms]
    params.append(max(1, min(limit, 100)))
    rows = [dict(row) for row in conn.execute(
        f"""
        SELECT c.chunk_id,c.heading,substr(c.chunk_text,1,1200) snippet,c.source_locator,
               c.verification_status,s.title source_title,s.content_kind,r.file_name,r.relative_path
        FROM library_text_chunks c
        JOIN library_resources r ON r.resource_id=c.resource_id
        LEFT JOIN library_source_sets s ON s.source_set_id=c.source_set_id
        WHERE {where}
        ORDER BY CASE c.verification_status WHEN 'verified' THEN 0 WHEN 'source_checked' THEN 1 ELSE 2 END,
                 s.year_hint DESC,c.updated_at DESC LIMIT ?
        """,
        params,
    )]
    return {"query": query, "count": len(rows), "items": rows}


def search_staged_questions(
    conn,
    *,
    query: str = "",
    question_type: str = "",
    verification_status: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    clauses = ["1=1"]
    params: list[Any] = []
    if query:
        clauses.append("(q.stem LIKE ? OR q.answer LIKE ? OR q.explanation_raw LIKE ? OR s.title LIKE ?)")
        params.extend([f"%{query}%"] * 4)
    if question_type:
        clauses.append("q.question_type=?")
        params.append(question_type)
    if verification_status:
        clauses.append("q.verification_status=?")
        params.append(verification_status)
    total = conn.execute(
        f"SELECT COUNT(*) FROM staged_questions q JOIN library_source_sets s ON s.source_set_id=q.source_set_id WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()[0]
    rows = [dict(row) for row in conn.execute(
        f"""
        SELECT q.candidate_question_id,q.original_number,q.question_type,q.section,
               substr(q.stem,1,500) stem,q.answer,q.confidence,q.verification_status,
               q.review_reasons_json,q.source_locator,s.title source_title,s.year_hint
        FROM staged_questions q JOIN library_source_sets s ON s.source_set_id=q.source_set_id
        WHERE {' AND '.join(clauses)}
        ORDER BY s.year_hint DESC,q.verification_status,q.confidence DESC,q.candidate_question_id
        LIMIT ?
        """,
        (*params, max(1, min(limit, 200))),
    )]
    for row in rows:
        row["review_reasons"] = json.loads(row.pop("review_reasons_json") or "[]")
    return {"total": total, "count": len(rows), "items": rows}


def staged_question_detail(conn, candidate_question_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT q.*,s.title source_title,s.year_hint,s.pairing_status
        FROM staged_questions q JOIN library_source_sets s ON s.source_set_id=q.source_set_id
        WHERE q.candidate_question_id=?
        """,
        (candidate_question_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown staged question: {candidate_question_id}")
    result = dict(row)
    for key in ("options_json", "secondary_test_points_json", "content_tags_json", "review_reasons_json"):
        result[key.removesuffix("_json")] = json.loads(result.pop(key) or "[]")
    if result.get("candidate_passage_id"):
        passage = conn.execute(
            "SELECT * FROM staged_passages WHERE candidate_passage_id=?",
            (result["candidate_passage_id"],),
        ).fetchone()
        result["passage"] = dict(passage) if passage else None
    result["knowledge_points"] = [dict(item) for item in conn.execute(
        """
        SELECT kp.code,kp.name_cn,m.role,m.mapping_source,m.confidence,m.verification_status,
               m.rationale,m.source_locator
        FROM staged_question_knowledge_map m
        JOIN knowledge_points kp ON kp.knowledge_point_id=m.knowledge_point_id
        WHERE m.candidate_question_id=?
        ORDER BY CASE m.role WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1
          WHEN 'prerequisite' THEN 2 ELSE 3 END,m.confidence DESC
        """,
        (candidate_question_id,),
    )]
    return result
