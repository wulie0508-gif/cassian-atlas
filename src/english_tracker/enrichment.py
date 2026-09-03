from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .db import connect
from .grammar_catalog import repair_mojibake
from .util import stable_id, utc_now


GRAMMAR_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("谓语", "predicate_count"),
    ("时态", "tense"),
    ("被动", "passive_voice"),
    ("主谓一致", "subject_verb_agreement"),
    ("情态", "modal_verb"),
    ("不定式", "infinitive"),
    ("动名词", "gerund"),
    ("现在分词", "present_participle"),
    ("过去分词", "past_participle"),
    ("非谓语", "non_finite"),
    ("名词派生", "noun_derivation"),
    ("形容词派生", "adjective_derivation"),
    ("副词派生", "adverb_derivation"),
    ("词性", "word_derivation"),
    ("单复数", "noun_number"),
    ("比较级", "comparative_degree"),
    ("最高级", "comparative_degree"),
    ("否定前缀", "negative_prefix"),
    ("代词", "pronoun_form"),
    ("冠词", "article"),
    ("介词", "preposition_collocation"),
    ("固定搭配", "preposition_collocation"),
    ("并列", "coordinating_conjunction"),
    ("定语从句", "relative_clause"),
    ("名词性从句", "noun_clause"),
    ("状语从句", "adverbial_clause"),
    ("连接词", "connector_function_clause_completeness"),
    ("倒装", "inversion"),
    ("强调", "emphasis"),
    ("虚拟", "subjunctive"),
    ("固定句式", "fixed_structure"),
)


METHODS: dict[str, dict[str, Any]] = {
    "语法填空": {
        "method": "句子主干—空格功能—形式约束—语义复核",
        "steps": ["划分从句并数谓语", "判断空格是否有提示词", "锁定词性或句法功能", "检查时态、语态、主谓一致与非谓语关系", "回读上下文确认语义和拼写"],
        "traps": ["只看提示词不看句法位置", "忽略从句边界", "主动被动与动作先后混淆", "派生词词性正确但语义方向错误"],
    },
    "阅读理解": {
        "method": "题干定位—证据句—同义改写—排除越界",
        "steps": ["识别题型和定位词", "回原文锁定最小证据区间", "比较选项与原文的同义改写", "排除偷换范围、无中生有和因果倒置"],
        "traps": ["凭常识代替原文", "把局部细节当主旨", "忽略态度词和转折", "选项复述原词但改变逻辑"],
    },
    "完形填空": {
        "method": "篇章主线—句内搭配—句际逻辑—复现校验",
        "steps": ["通读把握人物、事件和情感主线", "判断空格词性与句法角色", "比较搭配和语义场", "利用转折、因果、复现与照应", "回读检查叙事一致性"],
        "traps": ["只做单句翻译", "忽略褒贬和人物立场", "近义词搭配限制未检查", "前后文复现线索遗漏"],
    },
    "选词填空": {
        "method": "词性分组—语境定位—变形检查—篇章复核",
        "steps": ["给词库标注词性与可能变形", "按空格句法位置筛选", "结合搭配和语义确定候选", "检查词形与剩余词的一致性"],
        "traps": ["忽略词形变化", "只按中文词义选择", "重复使用同一词", "未检查固定搭配"],
    },
    "听力理解": {
        "method": "预读预测—首轮抓主线—二轮定位—证据核对",
        "steps": ["预读题干并圈出人物、时间、数字和态度词", "首听抓场景与主旨", "再听定位细节和转折", "区分原话、改写与推断"],
        "traps": ["听到相同单词就选", "忽略否定、修正和转折", "数字时间换算错误", "人物关系判断过度"],
    },
    "汉译英": {
        "method": "中文意群—英文主干—从句组织—搭配与形态复核",
        "steps": ["拆分意群并确定主谓宾", "确定核心句型和时态语态", "安排从句、非谓语或并列结构", "检查关键词搭配、冠词、单复数和标点"],
        "traps": ["逐字直译", "主干不完整", "中文语序直接迁移", "关键词虽出现但搭配错误"],
    },
    "写作": {
        "method": "审题约束—信息架构—段落推进—语言校验",
        "steps": ["圈定对象、体裁、目的和必写要点", "确定中心观点与段落功能", "用理由和例子展开", "检查衔接、句式变化、语域和语法"],
        "traps": ["漏任务点", "观点有而论据空", "连接词堆砌", "复杂句失控导致准确率下降"],
    },
}


def _normalized(value: str | None) -> str:
    return (repair_mojibake(value) or "").strip()


def _question_family(question_type: str) -> str:
    value = _normalized(question_type)
    if "语法" in value:
        return "语法填空"
    if "完形" in value:
        return "完形填空"
    if "选词" in value:
        return "选词填空"
    if "听力" in value:
        return "听力理解"
    if "翻译" in value or "汉译英" in value:
        return "汉译英"
    if any(token in value for token in ("写作", "Writing", "续写", "概要")):
        return "写作"
    if any(token in value for token in ("阅读", "六选四", "七选五")):
        return "阅读理解"
    return value or "其他"


def _mapping_specs(row: sqlite3.Row) -> list[tuple[str, str, str, float, str]]:
    family = _question_family(row["question_type"])
    primary = _normalized(row["primary_test_point"])
    secondary = _normalized(row["secondary_test_points"])
    combined = " ".join((family, primary, secondary, _normalized(row["section"])))
    mappings: list[tuple[str, str, str, float, str]] = []

    def add(code: str, role: str, source: str, confidence: float, rationale: str) -> None:
        key = (code, role)
        if any((item[0], item[1]) == key for item in mappings):
            return
        mappings.append((code, role, source, confidence, rationale))

    if family == "语法填空":
        add("grammar", "prerequisite", "rule", 0.95, "题型属于语法填空。")
        for keyword, code in GRAMMAR_KEYWORDS:
            if keyword in primary:
                add(code, "primary", "legacy", 0.96, f"原题库一级考点包含“{keyword}”。")
            elif keyword in secondary:
                add(code, "secondary", "legacy", 0.90, f"原题库二级考点包含“{keyword}”。")
        if any(token in combined for token in ("从句", "连接词")):
            add("connector_function_clause_completeness", "prerequisite", "rule", 0.78, "从句题需先判断连接词功能与从句完整性。")
        if any(token in combined for token in ("非谓语", "分词", "不定式", "动名词")):
            add("non_finite_logical_subject", "prerequisite", "rule", 0.76, "非谓语选择需识别逻辑主语。")
            add("non_finite_voice", "trap", "rule", 0.72, "主动被动关系是常见干扰维度。")
            add("non_finite_sequence", "trap", "rule", 0.68, "动作先后可能影响完成式选择。")
    elif family == "阅读理解":
        add("reading", "prerequisite", "rule", 0.95, "题型属于阅读理解。")
        if "细节" in combined:
            add("reading_detail", "primary", "legacy", 0.94, "原题库考点标注为细节理解。")
        if any(token in combined for token in ("推理", "推断")):
            add("reading_inference", "primary", "legacy", 0.94, "原题库考点标注为推理判断。")
        if any(token in combined for token in ("主旨", "大意", "标题")):
            add("reading_main_idea", "primary", "legacy", 0.94, "原题库考点标注为主旨大意。")
        if any(token in combined for token in ("词义", "猜词")):
            add("reading_vocab_context", "primary", "legacy", 0.92, "原题库考点涉及语境猜词。")
        if any(token in combined for token in ("目的", "态度", "语气")):
            add("reading_purpose_attitude", "primary", "rule", 0.82, "考点文本涉及目的或态度判断。")
        if any(token in combined for token in ("结构", "段落", "顺序")):
            add("reading_text_structure", "secondary", "rule", 0.78, "考点文本涉及篇章结构。")
        add("reading_information_integration", "prerequisite", "rule", 0.62, "阅读作答通常需要整合证据并控制选项范围。")
    elif family == "完形填空":
        add("cloze", "prerequisite", "rule", 0.95, "题型属于完形填空。")
        add("cloze_context_semantics", "primary", "legacy", 0.90, "完形核心标注为语境词义。")
        if "搭配" in combined:
            add("cloze_collocation", "secondary", "legacy", 0.88, "原题库标注包含固定搭配。")
        add("cloze_logic", "prerequisite", "rule", 0.72, "句际逻辑是完形排除的重要依据。")
        add("cloze_cohesion", "prerequisite", "rule", 0.68, "篇章复现与照应可用于复核。")
    elif family == "选词填空":
        add("vocabulary_meaning", "primary", "rule", 0.82, "选词填空首先要求词义与语境匹配。")
        add("word_form", "secondary", "rule", 0.80, "空格句法位置会约束词形。")
        add("vocabulary_collocation", "trap", "rule", 0.70, "近义候选常通过搭配限制区分。")
    elif family == "听力理解":
        add("listening", "prerequisite", "rule", 0.95, "题型属于听力理解。")
        add("listening_detail", "primary", "rule", 0.78, "多数听力小题首先考查事实和细节定位。")
        if re.search(r"\bwhy\b|推断|意图|关系", combined, re.I):
            add("listening_inference", "secondary", "rule", 0.76, "题干提示需要推断。")
    elif family == "汉译英":
        add("translation_sentence_structure", "primary", "rule", 0.90, "翻译需先搭建完整英文主干。")
        add("translation_lexical_choice", "secondary", "rule", 0.82, "关键词和固定表达决定内容准确度。")
        add("translation_grammar_accuracy", "secondary", "rule", 0.82, "时态、语态和从句组织共同影响得分。")
        add("translation_idiomaticity", "trap", "rule", 0.70, "逐字直译是常见失分来源。")
    elif family == "写作":
        add("writing_task_fulfillment", "primary", "rule", 0.90, "写作首先受任务完成度约束。")
        add("writing_organization", "secondary", "rule", 0.82, "段落功能与信息顺序影响可读性。")
        add("writing_language_accuracy", "secondary", "rule", 0.82, "语言准确性是基础评分维度。")
        add("writing_cohesion", "secondary", "rule", 0.74, "衔接用于维持段落推进。")
        add("writing_register", "trap", "rule", 0.66, "体裁和对象会限制语域。")
    return mappings


def _grammar_features(row: sqlite3.Row) -> dict[str, Any]:
    text = " ".join(
        _normalized(row[key])
        for key in ("stem", "answer", "explanation_raw", "primary_test_point", "secondary_test_points")
    )
    return {
        "finite_predicate_cues": bool(re.search(r"\b(?:has|have|had|is|are|was|were|do|does|did|will|would|can|could|should|must)\b", text, re.I)),
        "passive_cues": bool(re.search(r"\b(?:by|be(?:en|ing)?|was|were|is|are)\b", text, re.I) and ("被动" in text or re.search(r"\b\w+ed\b", text, re.I))),
        "clause_markers": sorted(set(re.findall(r"\b(?:that|which|who|whom|whose|when|where|why|how|whether|if|although|because|since|while)\b", text, re.I)))[:20],
        "non_finite_cues": bool(any(token in text for token in ("非谓语", "不定式", "分词", "动名词"))),
        "derivation_cues": bool(any(token in text for token in ("词性", "派生", "形容词", "副词", "名词"))),
        "analysis_boundary": "规则特征仅用于检索和复核提示，不自动升级为verified。",
    }


def _upsert_enrichment(
    conn,
    *,
    question_id: str,
    enrichment_type: str,
    enrichment_key: str,
    content: dict[str, Any],
    confidence: float,
    rationale: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO question_enrichments(
          question_id,enrichment_type,enrichment_key,content_json,mapping_source,confidence,
          verification_status,rationale,created_at,updated_at
        ) VALUES (?,?,?,?,'rule',?,'suggested',?,?,?)
        ON CONFLICT(question_id,enrichment_type,enrichment_key) DO UPDATE SET
          content_json=excluded.content_json,confidence=excluded.confidence,rationale=excluded.rationale,
          updated_at=excluded.updated_at
        """,
        (question_id, enrichment_type, enrichment_key, json.dumps(content, ensure_ascii=False), confidence, rationale, now, now),
    )


def enrich_question_bank(conn, question_bank: str | Path, *, limit: int = 0) -> dict[str, Any]:
    question_bank_path = Path(question_bank).expanduser().resolve()
    if not question_bank_path.exists():
        raise ValueError(f"Question bank not found: {question_bank_path}")
    source = connect(question_bank_path, readonly=True)
    source.execute("PRAGMA query_only=ON")
    sql = "SELECT * FROM questions ORDER BY source_id,source_ordinal,question_id"
    if limit:
        sql += " LIMIT ?"
        rows = source.execute(sql, (limit,))
    else:
        rows = source.execute(sql)
    knowledge_ids = {row["code"]: row["knowledge_point_id"] for row in conn.execute("SELECT code,knowledge_point_id FROM knowledge_points")}
    processed = 0
    mapping_count = 0
    enrichment_count = 0
    search_count = 0
    family_counts: dict[str, int] = {}
    now = utc_now()
    for row in rows:
        processed += 1
        question_id = row["question_id"]
        family = _question_family(row["question_type"])
        family_counts[family] = family_counts.get(family, 0) + 1
        locator = row["source_path"] or f"question_bank:{question_id}"
        if row["source_page"]:
            locator += f"#page={row['source_page']}"
        specs = _mapping_specs(row)
        for code, role, mapping_source, confidence, rationale in specs:
            kp_id = knowledge_ids.get(code)
            if not kp_id:
                continue
            verification = "source_checked" if mapping_source == "legacy" and row["verification_status"] == "source_checked" else "suggested"
            conn.execute(
                """
                INSERT INTO question_deep_knowledge_map(
                  question_id,knowledge_point_id,role,mapping_source,confidence,verification_status,
                  rationale,source_locator,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(question_id,knowledge_point_id,role) DO UPDATE SET
                  mapping_source=excluded.mapping_source,confidence=excluded.confidence,
                  verification_status=CASE
                    WHEN question_deep_knowledge_map.verification_status='verified' THEN 'verified'
                    ELSE excluded.verification_status END,
                  rationale=excluded.rationale,source_locator=excluded.source_locator,updated_at=excluded.updated_at
                """,
                (question_id, kp_id, role, mapping_source, confidence, verification, rationale, locator, now, now),
            )
            mapping_count += 1

        method = METHODS.get(family, {
            "method": "题干约束—证据定位—答案生成—回源复核",
            "steps": ["识别任务与限制", "定位材料证据", "生成答案", "检查格式、语义和来源"],
            "traps": ["任务要求遗漏", "证据不足时过度推断"],
        })
        knowledge_detail = {
            "question_family": family,
            "section": _normalized(row["section"]),
            "primary_test_point": _normalized(row["primary_test_point"]),
            "secondary_test_points": _normalized(row["secondary_test_points"]),
            "difficulty": _normalized(row["difficulty"]),
            "verification_status": row["verification_status"],
            "knowledge_codes": [spec[0] for spec in specs],
            "source_locator": locator,
        }
        _upsert_enrichment(conn, question_id=question_id, enrichment_type="knowledge_detail", enrichment_key="rule-v1", content=knowledge_detail, confidence=0.86, rationale="整合原题库标签、题型和规则映射形成的细粒度检索元数据。")
        _upsert_enrichment(conn, question_id=question_id, enrichment_type="solving_method", enrichment_key="rule-v1", content=method, confidence=0.78, rationale=f"依据题型“{family}”匹配可复用解题流程。")
        _upsert_enrichment(conn, question_id=question_id, enrichment_type="trap_analysis", enrichment_key="rule-v1", content={"traps": method["traps"], "question_specific_note": _normalized(row["notes"])}, confidence=0.70, rationale="题型共性陷阱与原题备注组合；需结合学生原始答案后才能确认具体错因。")
        enrichment_count += 3
        if family == "语法填空":
            _upsert_enrichment(conn, question_id=question_id, enrichment_type="grammar_structure", enrichment_key="rule-v1", content=_grammar_features(row), confidence=0.72, rationale="从题干、答案、解析和标签中抽取句法检索特征。")
            enrichment_count += 1
        if _normalized(row["explanation_raw"]):
            _upsert_enrichment(
                conn,
                question_id=question_id,
                enrichment_type="answer_reasoning",
                enrichment_key="source-explanation-v1",
                content={"source_explanation": _normalized(row["explanation_raw"])[:6000], "answer": _normalized(row["answer"])},
                confidence=0.90 if row["verification_status"] == "source_checked" else 0.68,
                rationale="保留题库原解析作为检索依据，不代表已人工重写或复核。",
            )
            enrichment_count += 1

        search_text = "\n".join(
            value for value in (
                family,
                _normalized(row["section"]),
                _normalized(row["primary_test_point"]),
                _normalized(row["secondary_test_points"]),
                _normalized(row["stem"]),
                _normalized(row["answer"]),
                _normalized(row["explanation_raw"])[:6000],
                " ".join(spec[0] for spec in specs),
                method["method"],
            ) if value
        )
        search_id = stable_id("SEARCH", "question", question_id, length=24)
        conn.execute(
            """
            INSERT INTO knowledge_search_documents(
              search_document_id,question_id,passage_id,document_type,title,search_text,metadata_json,
              source_locator,mapping_source,verification_status,created_at,updated_at
            ) VALUES (?,?,?,'question',?,?,?,?,'rule',?,?,?)
            ON CONFLICT(search_document_id) DO UPDATE SET
              title=excluded.title,search_text=excluded.search_text,metadata_json=excluded.metadata_json,
              source_locator=excluded.source_locator,verification_status=excluded.verification_status,
              updated_at=excluded.updated_at
            """,
            (
                search_id,
                question_id,
                row["passage_id"] or None,
                f"{family} · {row['original_number'] or question_id}",
                search_text,
                json.dumps(knowledge_detail, ensure_ascii=False),
                locator,
                "source_checked" if row["verification_status"] == "source_checked" else "suggested",
                now,
                now,
            ),
        )
        search_count += 1
        if processed % 250 == 0:
            conn.commit()

    for method_row in source.execute("SELECT * FROM teaching_methods ORDER BY method_id"):
        method_id = method_row["method_id"]
        locator = method_row["source_path"] or f"teaching_method:{method_id}"
        search_id = stable_id("SEARCH", "method", method_id, length=24)
        search_text = "\n".join(
            _normalized(method_row[key])
            for key in ("question_type", "topic", "method_name", "summary", "method_text", "applicable_conditions", "common_traps")
            if _normalized(method_row[key])
        )
        conn.execute(
            """
            INSERT INTO knowledge_search_documents(
              search_document_id,document_type,title,search_text,metadata_json,source_locator,
              mapping_source,verification_status,created_at,updated_at
            ) VALUES (?,'teaching_method',?,?,?,?, 'legacy',?,?,?)
            ON CONFLICT(search_document_id) DO UPDATE SET
              title=excluded.title,search_text=excluded.search_text,metadata_json=excluded.metadata_json,
              source_locator=excluded.source_locator,verification_status=excluded.verification_status,
              updated_at=excluded.updated_at
            """,
            (
                search_id,
                _normalized(method_row["method_name"]) or method_id,
                search_text,
                json.dumps({"method_id": method_id, "question_type": _normalized(method_row["question_type"]), "topic": _normalized(method_row["topic"])}, ensure_ascii=False),
                locator,
                "source_checked" if method_row["verification_status"] in {"source_checked", "verified"} else "suggested",
                now,
                now,
            ),
        )
        search_count += 1
    source.close()
    conn.commit()
    return {
        "question_bank": str(question_bank_path),
        "questions_processed": processed,
        "knowledge_mappings_upserted": mapping_count,
        "enrichments_upserted": enrichment_count,
        "search_documents_upserted": search_count,
        "question_families": family_counts,
        "verification_rule": "规则生成内容保持suggested；只有原题库直接标签且题目source_checked时可标source_checked。",
    }


def search_knowledge(conn, query: str, *, limit: int = 30) -> dict[str, Any]:
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    if not terms:
        return {"query": query, "count": 0, "items": []}
    clauses = " AND ".join("search_text LIKE ?" for _ in terms)
    params: list[Any] = [f"%{term}%" for term in terms]
    params.append(max(1, min(limit, 100)))
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT search_document_id,question_id,passage_id,document_type,title,
                   substr(search_text,1,800) snippet,metadata_json,source_locator,
                   verification_status,updated_at
            FROM knowledge_search_documents
            WHERE {clauses}
            ORDER BY CASE verification_status WHEN 'verified' THEN 0 WHEN 'source_checked' THEN 1 ELSE 2 END,
                     updated_at DESC LIMIT ?
            """,
            params,
        )
    ]
    for row in rows:
        row["metadata"] = json.loads(row.pop("metadata_json"))
    return {"query": query, "count": len(rows), "items": rows}
