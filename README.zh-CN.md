<div align="center">
  <img src="docs/assets/logo.svg" width="420" alt="OpenTutor Ledger">
  <p><strong>面向教师、学生与 Agent 的本地优先学习证据层。</strong></p>
  <p>
    <a href="README.md">English</a> ·
    <a href="#快速开始">快速开始</a> ·
    <a href="docs/PRIVACY_BOUNDARY.md">隐私边界</a> ·
    <a href="CONTRIBUTING.md">参与贡献</a>
  </p>
</div>

---

## 普通学习工具记住答案，OpenTutor Ledger 记住证据

OpenTutor Ledger 是一套私有、可审计、面向 Agent 的学习记录与编排系统。它持续回答五个问题：

1. 学生实际上做了什么？
2. 题目考了什么，诊断依据是什么？
3. 接下来应该复测什么？
4. 这条成绩对趋势应该有多大影响？
5. 另一个 Agent 能否直接复用结果，而不是重新计算？

后台保存不可变作答、可修订评分、样本量、知识映射、复测队列与审计证据；前台默认只显示当前状态、最小下一步和自动化是否正常。

> **产品原则：后台精确，前台减负。重复劳动交给 Agent，真正需要判断的事情留给人。**

## 已实现能力

| 能力 | 说明 |
| --- | --- |
| 多学生工作区 | 学生之间的课次、作答、掌握度与复测队列严格隔离 |
| 多学科注册 | 英语具有完整专用适配器；地理、数学、语文、科学已经能记录通用学习证据 |
| 中英双语界面 | 中文与 English 即时切换，不改变底层事实 |
| Agent 接口 | 课件、听写、工程对话通过稳定 HTTP/JSON 契约查询与写入 |
| 证据加权 | 线下封闭测试以更高权重校准日常课堂，但不会覆盖平时成绩 |
| 知识与错因分离 | 题目“考什么”和学生“为什么错”分别建模 |
| 本地确定性流程 | 听写批改、复测排程与统计不必反复消耗模型 token |
| 隐私开源边界 | 代码可公开，学生数据、题库、教材、OCR、音频和本机路径全部留在仓库外 |

## 证据纪律

- 没有保存原始答案时，记录 `answer_capture_status=not_captured`，不反推具体错因。
- 只有一道错误证据时，只能视为“暂定薄弱点”。
- 模型生成的知识映射和错因只能是 `suggested`，不能自动升级为已核验。
- 不同类型、不同满分的原始成绩不会连成一条误导性趋势线。
- 完整语篇选题不会拆散文章。

## 快速开始

```powershell
git clone https://github.com/wulie0508-gif/open-tutor-ledger.git
cd open-tutor-ledger
python -m venv .venv
python -m pip install -e .

$env:ENGLISH_TRACKER_DATA_DIR = "$env:USERPROFILE\OpenTutorData"
$env:ENGLISH_TRACKER_DB_NAME = "learning.sqlite"
python -m english_tracker init --student STU-001 --display-name "本机学生"
python scripts/create_empty_question_bank.py --output "$env:USERPROFILE\OpenTutorData\question-bank.sqlite"
New-Item -ItemType Directory -Force "$env:USERPROFILE\OpenTutorData\source-library" | Out-Null
$env:ENGLISH_TRACKER_QUESTION_BANK = "$env:USERPROFILE\OpenTutorData\question-bank.sqlite"
$env:ENGLISH_TRACKER_LIBRARY_ROOT = "$env:USERPROFILE\OpenTutorData\source-library"
python -m english_tracker serve --host 127.0.0.1 --port 8788 --open-browser
```

空题库只包含结构，不包含任何习题。使用者自行接入有权使用的题库或原创材料，并把它们存放在仓库外。

## Agent 的典型用法

```text
GET  /api/home                    # 最小当前状态
GET  /api/context/courseware      # 课件 Agent 上下文
GET  /api/context/dictation       # 听写 Agent 上下文
POST /api/classroom/attempts      # 批量写入课堂作答
POST /api/dictation/results       # 本地确定性批改与写入
GET  /api/reports/weekly          # 周报
GET  /api/reports/trends          # 分系列趋势
```

网站不是第二套录入工作。正常流程是用户把结果交给对应 Agent，由 Agent 调接口完成保存，网站只负责快速查看与必要的人工确认。

## 开源隐私检查

```bash
python -m unittest discover -s tests -v
python scripts/release_privacy_audit.py
```

发布检查会拦截数据库、文档、试卷、表格、图片、音频、压缩包、超大文件、私人路径和已知学生标识。完整说明见 [隐私边界](docs/PRIVACY_BOUNDARY.md)。

## 当前边界

当前版本适合本机、单教师或可信局域环境，不提供公网认证。英语适配器已包含知识树、完整语篇覆盖、阅读错因、听写和趋势分析；其他学科已经共享统一证据模型，专用题库和知识树可作为独立适配器继续扩展。

## License

代码与原创文档使用 [MIT License](LICENSE)。学生数据与第三方教育内容从未纳入本仓库，也不属于该许可证授权范围。
