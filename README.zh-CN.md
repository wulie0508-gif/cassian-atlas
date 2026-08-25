<div align="center">
  <img src="docs/assets/logo.svg" width="420" alt="OpenTutor Ledger">
  <p><strong>面向教师、学生与 Agent 的本地优先学习证据层。</strong></p>
  <p>
    <a href="README.md">English</a> ·
    <a href="https://wulie0508-gif.github.io/open-tutor-ledger/">公开产品演示</a> ·
    <a href="#快速开始">快速开始</a> ·
    <a href="docs/CODEX_FIRST_WORKFLOW.md">Codex 工作流</a> ·
    <a href="docs/TEACHER_DASHBOARD_ROADMAP.md">教师看板路线</a> ·
    <a href="docs/PRIVACY_BOUNDARY.md">隐私边界</a> ·
    <a href="CONTRIBUTING.md">参与贡献</a>
  </p>
</div>

<p align="center">
  <a href="https://wulie0508-gif.github.io/open-tutor-ledger/">
    <img src="docs/assets/product-preview.svg" width="1100" alt="OpenTutor Ledger 教师决策台合成数据预览">
  </a>
</p>

> **公开的是产品演示，私有的是学习运行时。** 展示站全部使用人工编写的合成数据，不连接学生数据库、题库或本机服务。详见[公开展示边界](docs/PUBLIC_DEMO.md)。

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
| 教师决策台 | 只读展示下一课优先项、同口径趋势、复测队列和证据缺口 |
| 公开产品演示 | 独立部署的静态合成数据展厅，不连接私有运行时 |
| 轻量中枢路由 | 只判断一次任务类型，只调用必要的专用 Skill，并自动更新看板台账 |
| 独立专用 Skill | 录入证据、错题诊断、算法选题、课件上下文、听写和看板同步可分别加载 |
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
powershell -ExecutionPolicy Bypass -File scripts/install_codex_skills.ps1

$privateRoot = "$env:USERPROFILE\OpenTutorData"
opentutor config set data_dir $privateRoot
opentutor config set db_name "learning.sqlite"
opentutor config set question_bank "$privateRoot\question-bank.sqlite"
opentutor config set library_root "$privateRoot\source-library"
python scripts/create_empty_question_bank.py --output "$privateRoot\question-bank.sqlite"
New-Item -ItemType Directory -Force "$privateRoot\source-library" | Out-Null
opentutor init
opentutor student add --student STU-LOCAL-001 --display-name "本机学生"
opentutor info
opentutor server start --open-browser
```

空题库只包含结构，不包含任何习题。使用者自行接入有权使用的题库或原创材料，并把它们存放在仓库外。

全局 `--config` 优先于 `OPEN_TUTOR_CONFIG`；如果都没有，系统自动发现 `%USERPROFILE%\.opentutor\config.json`。`opentutor info` 显示有待迁移版本时，先执行 `opentutor upgrade`。`init` 和 `upgrade` 都不会创建学生。

## Agent 的典型用法

```text
POST /api/agent/route              # 中枢只规划最小 Skill 链
GET  /api/agent/capabilities       # 专用能力清单
GET  /api/agent/dashboard          # 自动化运行台账
POST /api/agent/runs/{id}/events   # 专用 Agent 更新进度
GET  /api/home                    # 最小当前状态
GET  /api/teacher/dashboard       # 教师决策看板（显式学生与学科）
GET  /api/context/courseware      # 课件 Agent 上下文
GET  /api/context/dictation       # 听写 Agent 上下文
POST /api/sessions                # 创建或确认课次
POST /api/classroom/attempts      # 批量写入课堂作答
POST /api/dictation/results       # 本地确定性批改与写入
GET  /api/reports/weekly          # 周报
GET  /api/reports/trends          # 分系列趋势
```

网站不是第二套录入工作。正常流程是用户把任务交给中枢，中枢只分发必要的专用 Skill；专用 Agent 调接口完成保存并自动更新看板。网站只负责快速查看与必要的人工确认。运行台账与学习证据分表保存，因此“任务已完成”不会被误算成学生成绩。

## 开源隐私检查

```bash
python -m unittest discover -s tests -v
python scripts/release_privacy_audit.py
```

发布检查会拦截数据库、文档、试卷、表格、图片、音频、压缩包、超大文件、私人路径和已知学生标识。完整说明见 [隐私边界](docs/PRIVACY_BOUNDARY.md)。

## 当前边界

当前版本适合本机、单教师或可信局域环境，不提供公网认证。英语适配器已包含知识树、完整语篇覆盖、阅读错因、听写和趋势分析；中枢路由、独立 Skill 和看板运行台账已经完成。其他学科共享统一证据模型，专用题库和知识树可作为独立适配器继续扩展。

## License

代码与原创文档使用 [MIT License](LICENSE)。学生数据与第三方教育内容从未纳入本仓库，也不属于该许可证授权范围。
