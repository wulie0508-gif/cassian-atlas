(() => {
  const en = {
    '项目总览': 'Overview',
    '题库与知识': 'Question Bank',
    '学情掌握': 'Mastery',
    '学习记录': 'Learning Records',
    '解析中心': 'Parsing',
    '单词听写': 'Dictation',
    'Agent 工作流': 'Agent Workflows',
    '三个对话': 'Agent Workflows',
    '学生': 'Learner',
    '学科': 'Subject',
    '语言': 'Language',
    '查看专业数据': 'View evidence',
    '返回减负模式': 'Back to simple view',
    '刷新': 'Refresh',
    '数据已连接': 'Data connected',
    '连接中': 'Connecting',
    '连接失败': 'Connection failed',
    '添加学生': 'Add learner',
    '关闭学生管理': 'Close learner manager',
    '本机私有': 'Private on this device',
    '学生姓名与学习记录只写入本机数据库，不进入开源仓库。': 'Learner names and records stay in the local database and never enter the public repository.',
    '显示名称': 'Display name',
    '时区': 'Timezone',
    '初始学科': 'Initial subject',
    '创建学生工作区': 'Create learner workspace',
    '后台已接通，平时不用维护网站': 'The backend is connected. No dashboard upkeep required.',
    '课堂、阅读、听写和线下测试都由对应 Agent 调用统一接口。你不需要再抄表、重复录入或手写交接。': 'The responsible agents record lessons, reading, dictation, and offline tests through one API. No duplicate entry or handwritten handoffs.',
    '你只需': 'Your only step',
    '把结果交给对应对话': 'Send the result to the right agent',
    '课堂与阅读交给课件对话，听写交给单词听写对话；Agent 会写入统一数据库，网站无需重复录入。': 'Send lessons and reading to the courseware agent, and dictation to the dictation agent. They write to the same local store.',
    '当前成绩': 'Current mastery',
    '已记录作答': 'Recorded attempts',
    '本次听写批次': 'Current dictation batch',
    '三个对话都已接通': 'All three agents are connected',
    '平时直接对话，Agent 在后台读写；只有遇到无法判断的内容才需要你确认。': 'Keep working in chat. Agents read and write in the background and ask only when a decision genuinely needs you.',
    '课件生成': 'Courseware',
    '单词听写': 'Dictation',
    '工程与数据': 'Engineering & data',
    '读取薄弱点、完整语篇、教学方法与知识覆盖，生成课堂材料。': 'Reads weaknesses, complete passages, teaching methods, and coverage to build lesson materials.',
    '生成听写清单、保存原始作答、自动批改并安排复测。': 'Builds dictation queues, preserves raw answers, grades locally, and schedules retests.',
    '数据库架构、迁移、解析、接口、质量检查和本地站点。': 'Owns schema, migrations, parsing, APIs, quality checks, and the local app.',
    '专业数据': 'Evidence & audit detail',
    '结构化题目': 'Structured questions',
    '真实作答': 'Real attempts',
    '加权掌握率': 'Weighted mastery',
    '深层知识映射': 'Deep mappings',
    '资料可追踪': 'Traceable sources',
    '当前薄弱信号': 'Current weak signals',
    '按证据权重与样本量排序；单题错误仍标为暂定': 'Ranked by evidence weight and sample size; one-question signals remain tentative.',
    '题库主要构成': 'Question-bank mix',
    '线下测试校准': 'Offline calibration',
    '工程进度': 'Engineering progress',
    '数据健康': 'Data health',
    '数据可用': 'Data ready',
    '题目总数': 'Questions',
    '可直接使用': 'Ready to use',
    '完整文章': 'Complete passages',
    '已有解析': 'With explanations',
    '教学方法': 'Teaching methods',
    '完整语篇自动组卷': 'Complete-passage selection',
    '生成选篇': 'Select passages',
    '题目检索': 'Question search',
    '关键词': 'Keywords',
    '题型': 'Question type',
    '核验状态': 'Verification',
    '全部题型': 'All types',
    '全部状态': 'All statuses',
    '检索': 'Search',
    '详情': 'Details',
    '题目': 'Question',
    '知识点': 'Knowledge point',
    '年份/地区': 'Year / region',
    '状态': 'Status',
    '每次作答都是成绩，线下测试负责校准': 'Every response is evidence; offline tests calibrate it',
    '课堂、阅读、语法、作业和听写都会自动累计；线下闭卷权重更高，但不会覆盖日常证据。': 'Lessons, reading, grammar, homework, and dictation accumulate automatically. Closed offline tests carry more weight without replacing daily evidence.',
    '把本次结果发给对应对话': 'Send this result to the right agent',
    '课件对话或听写对话会保存逐题结果；有阅读原始答案时，再自动生成可核验错因。': 'The courseware or dictation agent stores item-level results and derives reviewable reading causes only when raw answers exist.',
    '当前正确率': 'Current accuracy',
    '线下校准': 'Offline calibration',
    '待首次': 'Awaiting first test',
    '最近三次记录': 'Latest three records',
    '快速确认数据是否已经进库；完整记录在专业数据中': 'Quickly confirm ingestion; the full ledger is available in evidence view.',
    '专业数据与应急录入': 'Evidence detail & emergency entry',
    '真实课堂成绩': 'Classroom evidence',
    '阅读整篇诊断': 'Passage-level reading diagnosis',
    '补录高权重校准': 'Emergency calibration entry',
    '校准锚点记录': 'Calibration anchors',
    '证据权重': 'Evidence weights',
    '题目修正项': 'Item modifiers',
    '听写固定工作流已接通': 'The dictation workflow is connected',
    '单词听写对话会自动取到期词、保留 OCR 原始答案、本地精确批改、写入成绩并安排复测。': 'The dictation agent fetches due words, preserves raw OCR output, grades locally, records results, and schedules retests.',
    '把听写结果发给单词听写对话': 'Send the dictation result to the dictation agent',
    '网站不要求你重复录入；下方手动表格只作为断网或接口异常时的应急入口。': 'No duplicate dashboard entry is required. The manual form is only an offline or recovery fallback.',
    '本次待复测': 'Current batch',
    '对话会自动读取': 'Fetched automatically by the agent',
    '批改': 'Grading',
    '本地规则': 'Local rules',
    '重复使用不消耗模型 token': 'Repeated use consumes no model tokens',
    '结果去向': 'Destination',
    '统一数据库': 'Unified local store',
    '成绩与错题同步更新': 'Scores and review tasks update together',
    'Agent 取清单': 'Agent fetches queue',
    '按到期日、优先级和连续错误自动取词。': 'Selects words by due date, priority, and consecutive errors.',
    'OCR 保留原文': 'OCR preserves raw text',
    '识别结果不先改正，确保错因可追溯。': 'Recognition output is never corrected before storage, preserving traceability.',
    '确定性批改': 'Deterministic grading',
    '本地精确匹配并记录 correct / wrong / partial。': 'Matches locally and records correct, wrong, or partial.',
    '自动排复测': 'Automatic retest',
    '错误进入复习队列，后续对话直接调用。': 'Errors enter the review queue for the next agent run.',
    '应急手动入口': 'Emergency manual entry',
    '不用再手写交接文档': 'No more handwritten handoffs',
    '你继续和这个对话说需求，其余由它在后台完成。': 'Keep talking to this agent; it handles the rest in the background.',
    '工程审计': 'Engineering audit',
    '统一工作清单': 'Unified work ledger',
    '学科工作区已就绪': 'Subject workspace is ready',
    '这个学科已经可以接收通用课堂、作业与测试记录；专用题库和知识树由独立适配器逐步接入。': 'This subject can already accept generic lesson, homework, and assessment records. Specialized question banks and knowledge trees are added through separate adapters.',
    '当前没有学习记录': 'No learning records yet',
    '把第一次课堂或测试结果交给 Agent 后，这里会自动出现证据。': 'Send the first lesson or assessment result to an agent and evidence will appear here automatically.',
    '学习活动': 'Sessions',
    '题目证据': 'Item evidence',
    '最后活动': 'Last activity',
    '通用接口可用': 'Generic API ready',
    '专用适配器': 'Specialized adapter',
    '已接入': 'Connected',
    '待扩展': 'Extensible',
    '读取失败': 'Could not load data',
    '重新读取': 'Try again',
    '添加学生成功': 'Learner created',
    '选择学生': 'Select learner',
    '选择学科': 'Select subject',
    '选择界面语言': 'Select interface language',
    '跳到主要内容': 'Skip to main content',
    '主导航': 'Primary navigation',
    '例如：Student A': 'For example: Student A',
    '本机 127.0.0.1': 'Local 127.0.0.1',
    '当前学习状态': 'Current learning state',
    '学科摘要': 'Subject summary',
    '真实题库 question_type 分布': 'Distribution by source question type',
    '线下闭卷证据权重高于课堂与家庭练习': 'Closed-book offline evidence carries more weight than classwork and homework.',
    '录入成绩 →': 'Record score →',
    '高权重校准锚点待补充': 'A high-weight calibration anchor is still needed',
    '现有课堂与平时作答已经是真实成绩。当前只缺正式线下闭卷整卷或双周混合测，用于校准平时训练是否能迁移到受控环境。': 'Classroom and daily attempts are already real evidence. Add a formal closed-book paper or biweekly mixed test to calibrate transfer into controlled conditions.',
    '从数据库自动读取，不再依赖手写交接百分比': 'Read directly from the database instead of handwritten progress handoffs.',
    '查看职责 →': 'View responsibilities →',
    '同一数据库与只读题库的当前状态': 'Current state of the unified store and read-only question bank.',
    '题库路径和解析状态均由本机读取。': 'Question-bank paths and parsing state are read locally.',
    '按学生与学科隔离': 'Isolated by learner and subject',
    '个不同项目': 'distinct items',
    '暂无': 'No evidence',
    '这是正常的空工作区': 'This is a normal empty workspace.',
    'Agent 写入时为题目设置': 'Agents set',
    '数据会与其他学科严格分开。': 'and keep the evidence strictly isolated from other subjects.',
    '识图解析API接入主体': 'OCR API integration core',
    '本地学习管理站': 'Local learning management app',
    '统一学习数据库与审计接口': 'Unified learning database and audit API',
    '英语资料全量解析与深层知识索引': 'Full English-library parsing and deep knowledge index',
    '语法填空知识树与完整语篇覆盖': 'Grammar cloze taxonomy and complete-passage coverage',
    '套': 'deliverable',
    '文件': 'files',
    '题': 'questions',
    '完形填空': 'Cloze',
    '阅读理解': 'Reading comprehension',
    '语法填空': 'Grammar cloze',
    '选词填空': 'Word-bank cloze',
    '听力理解': 'Listening comprehension',
    '其他': 'Other',
    '汉译英': 'Chinese-to-English translation',
    '六选四': 'Six-to-four matching',
  };

  const patterns = [
    [/^(\d[\d,.]*)\s*\/\s*(\d[\d,.]*)\s+项检查通过。题库路径和解析状态均由本机读取。$/, '$1 / $2 checks passed. Question-bank paths and parsing state are read locally.'],
    [/^(\d[\d,.]*)\s*\/\s*(\d[\d,.]*)\s+套$/, '$1 / $2 deliverables'],
    [/^(\d[\d,.]*)\s*\/\s*(\d[\d,.]*)\s+文件$/, '$1 / $2 files'],
    [/^(\d[\d,.]*)\s*\/\s*(\d[\d,.]*)\s+题$/, '$1 / $2 questions'],
    [/^(.+)\s+·\s+通用接口可用$/, '$1 · Generic API ready'],
    [/^(\d[\d,.]*)\s+项当前到期复测$/, '$1 review tasks currently due'],
    [/^(\d[\d,.]*)\s*\/\s*(\d[\d,.]*)\s+项检查通过。(.+)$/, '$1 / $2 checks passed. $3'],
    [/^(\d[\d,.]*)\s+个不同项目$/, '$1 distinct items'],
    [/^Agent 写入时为题目设置\s+(.+)，数据会与其他学科严格分开。$/, 'Agents set $1 when writing items, keeping evidence isolated from other subjects.'],
    [/^数据刷新于\s+(.+)$/, 'Updated $1'],
    [/^(\d[\d,.]*)\s+次有效评分$/, '$1 scored responses'],
    [/^(\d[\d,.]*)\s+次学习活动$/, '$1 learning sessions'],
    [/^(\d[\d,.]*)\s+项到期任务由对话分批处理$/, '$1 due tasks handled in batches'],
    [/^(\d[\d,.]*)\s+道可直接使用$/, '$1 ready to use'],
    [/^有效样本\s+(.+)$/, 'Effective sample $1'],
    [/^(\d[\d,.]*)\s+条解析单元$/, '$1 explanation units'],
    [/^(\d[\d,.]*)\s+文本已解析\s+·\s+(\d[\d,.]*)\s+音频已配对$/, '$1 texts parsed · $2 audio files paired'],
    [/^(\d[\d,.]*)\s+次有效作答已落库$/, '$1 active attempts stored'],
    [/^(\d[\d,.]*)\s+项检查通过。(.+)$/, '$1 checks passed. $2'],
  ];

  const originalText = new WeakMap();
  const originalAttrs = new WeakMap();

  function translateValue(value) {
    if (en[value]) return en[value];
    for (const [pattern, replacement] of patterns) {
      if (pattern.test(value)) return value.replace(pattern, replacement);
    }
    return value;
  }

  function apply(root, locale) {
    const english = locale === 'en';
    document.documentElement.lang = english ? 'en' : 'zh-CN';
    document.title = english ? 'OpenTutor Ledger — Local learning evidence' : 'OpenTutor Ledger · 本地学习证据';
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const parent = node.parentElement;
      if (!parent || parent.closest('script,style,.mono,.text-block')) continue;
      if (!originalText.has(node)) originalText.set(node, node.nodeValue);
      const source = originalText.get(node);
      const trimmed = source.trim();
      if (!trimmed) continue;
      const translated = english ? translateValue(trimmed) : trimmed;
      node.nodeValue = source.replace(trimmed, translated);
    }
    root.querySelectorAll('[placeholder],[aria-label],[title]').forEach(element => {
      if (!originalAttrs.has(element)) {
        originalAttrs.set(element, Object.fromEntries(['placeholder','aria-label','title'].filter(name => element.hasAttribute(name)).map(name => [name, element.getAttribute(name)])));
      }
      const originals = originalAttrs.get(element);
      for (const [name, source] of Object.entries(originals)) {
        element.setAttribute(name, english ? translateValue(source) : source);
      }
    });
  }

  window.OpenTutorI18n = {apply};
})();
