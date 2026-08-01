const view = document.querySelector('#view');
const pageTitle = document.querySelector('#page-title');
const eyebrow = document.querySelector('#section-eyebrow');
const freshness = document.querySelector('#freshness');
const toast = document.querySelector('#toast');
const dialog = document.querySelector('#question-dialog');
const state = { current: 'overview', cache: {}, dictation: [] };

const titles = {
  overview: ['项目总览', 'PROJECT CONTROL'],
  'question-bank': ['题库与知识', 'QUESTION LIBRARY'],
  mastery: ['学情掌握', 'LEARNING EVIDENCE'],
  assessments: ['测试校准', 'ASSESSMENT CALIBRATION'],
  library: ['解析中心', 'PARSING PIPELINE'],
  dictation: ['单词听写', 'DICTATION AUTOMATION'],
  workflow: ['三个对话', 'WORKFLOW CONTRACTS'],
};

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}
function num(value) { return value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value); }
function pct(value, digits = 0) { return value == null ? '暂无' : `${(Number(value) * 100).toFixed(digits)}%`; }
function gb(bytes) { return bytes == null ? '—' : `${(Number(bytes) / 1073741824).toFixed(2)} GB`; }
function status(value) { return `<span class="status-tag ${esc(value)}">${esc(value || '未标注')}</span>`; }
function notify(message, isError = false) {
  toast.textContent = message; toast.className = `toast show${isError ? ' error' : ''}`;
  window.setTimeout(() => toast.className = 'toast', 2600);
}
async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json().catch(() => ({error: `HTTP ${response.status}`}));
  if (!response.ok) throw new Error(data.error || `请求失败：${response.status}`);
  return data;
}
function loading() {
  view.innerHTML = '<div class="loading-state" aria-busy="true"><div class="skeleton skeleton-title"></div><div class="skeleton-grid"><div></div><div></div><div></div></div></div>';
}
function errorState(error) {
  view.innerHTML = `<div class="panel error-state"><h2>读取失败</h2><p>${esc(error.message)}</p><button class="button button-secondary" data-action="refresh">重新读取</button></div>`;
}
function metric(label, value, note = '', tone = '') {
  return `<div class="metric"><span class="metric-label">${esc(label)}</span><strong class="metric-value">${esc(value)}</strong><div class="metric-note ${tone}">${esc(note)}</div></div>`;
}
function barList(rows, {label = 'label', value = 'value', format = num, max = null, tone = ''} = {}) {
  if (!rows?.length) return '<div class="empty-state">暂无可展示数据</div>';
  const ceiling = max ?? Math.max(...rows.map(row => Number(row[value]) || 0), 1);
  return `<div class="bar-list">${rows.map(row => {
    const raw = Number(row[value]) || 0;
    const width = Math.max(raw ? 2 : 0, raw / ceiling * 100);
    return `<div class="bar-row"><strong title="${esc(row[label])}">${esc(row[label])}</strong><div class="bar-track"><div class="bar-fill ${tone}" style="--value:${width.toFixed(2)}%"></div></div><span class="bar-value">${esc(format(raw))}</span></div>`;
  }).join('')}</div>`;
}
function panel(title, subtitle, body, action = '') {
  return `<section class="panel"><div class="panel-head"><div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div>${action}</div><div class="panel-body">${body}</div></section>`;
}

async function renderOverview(force = false) {
  const data = !force && state.cache.overview ? state.cache.overview : await api('/api/overview');
  state.cache.overview = data;
  const q = data.question_bank.counts;
  const l = data.learning;
  const mastery = l.mastery;
  const lib = data.library.totals;
  const work = data.workflow.work_items;
  const weakest = (mastery.knowledge_points || []).slice(0, 8).map(row => ({
    label: row.name_cn || row.knowledge_point,
    value: 1 - Number(row.calibrated_mastery || 0),
  }));
  const questionTypes = data.question_bank.distributions.question_types.slice(0, 8);
  const calibration = mastery.summary.offline_calibration_accuracy;
  const practice = mastery.summary.practice_accuracy;
  const progressBody = work.map(item => {
    const progress = item.progress == null ? (item.status === 'completed' ? 1 : 0) : item.progress;
    return `<div class="progress-item"><div class="progress-top"><strong>${esc(item.title)}</strong>${status(item.status)}</div><div class="progress-track"><span class="${item.status === 'completed' ? '' : 'pending'}" style="--progress:${(progress * 100).toFixed(1)}%"></span></div><small class="subtle">${num(item.completed_units)} / ${num(item.total_units)} ${esc(item.unit_label)}</small></div>`;
  }).join('');
  const calibrationBody = calibration == null
    ? `<div class="callout amber"><h3>线下校准还缺真实成绩</h3><p>权重模型已启用，但当前没有正式线下闭卷整卷或双周混合测。录入第一份成绩后，系统会显示“线下校准准确率 − 日常练习准确率”的差值。</p></div>`
    : `<div class="split-stat"><div><small>线下校准</small><strong>${pct(calibration, 1)}</strong></div><div><small>日常练习</small><strong>${pct(practice, 1)}</strong></div></div><p class="subtle">差值 ${pct(mastery.summary.calibration_gap, 1)}；正值表示受控环境表现更好。</p>`;
  view.innerHTML = `
    <section class="metric-strip">
      ${metric('结构化题目', num(q.questions), `${num(q.usable_questions)} 道可直接使用`, 'good')}
      ${metric('真实作答', num(l.counts.attempts), `${num(l.due_review_count)} 项当前到期复测`, l.due_review_count ? 'attention' : 'good')}
      ${metric('加权掌握率', pct(mastery.summary.weighted_accuracy, 1), `有效样本 ${num(mastery.summary.weighted_sample_size)}`)}
      ${metric('深层知识映射', num(l.counts.question_deep_knowledge_map), `${num(l.counts.question_enrichments)} 条解析单元`)}
      ${metric('资料可追踪', pct(lib.state_coverage_rate, 1), `${num(lib.completed_resources)} 文本已解析 · ${num(lib.audio_resources)} 音频已配对`)}
    </section>
    <div class="dashboard-grid">
      <div class="stack">
        ${panel('当前薄弱信号', '按证据权重与样本量排序；单题错误仍标为暂定', barList(weakest, {format: value => pct(value, 0), max: 1, tone: 'amber'}))}
        ${panel('题库主要构成', '真实题库 question_type 分布', barList(questionTypes))}
      </div>
      <div class="stack">
        ${panel('线下测试校准', '线下闭卷证据权重高于课堂与家庭练习', calibrationBody, '<button class="panel-action" data-nav="assessments">录入成绩 →</button>')}
        ${panel('工程进度', '从数据库自动读取，不再依赖手写交接百分比', `<div class="progress-list">${progressBody}</div>`, '<button class="panel-action" data-nav="workflow">查看职责 →</button>')}
        ${panel('数据健康', '同一数据库与只读题库的当前状态', `<div class="callout"><h3>${data.quality.trust_status === 'ready' ? '数据可用' : '需要关注'}</h3><p>${num(data.quality.checks_passed)} / ${num(data.quality.checks_total)} 项检查通过。题库路径和解析状态均由本机读取。</p></div>`)}
      </div>
    </div>`;
}

async function renderQuestionBank() {
  const [summary, mastery] = await Promise.all([
    state.cache.questionBank || api('/api/question-bank'),
    api('/api/mastery'),
  ]);
  state.cache.questionBank = summary;
  const q = summary.counts;
  const suggestedTargets = (mastery.knowledge_points || []).slice(0, 12);
  view.innerHTML = `
    <section class="metric-strip">
      ${metric('题目总数', num(q.questions), '同一结构化题库')}
      ${metric('可直接使用', num(q.usable_questions), pct(q.usable_questions / q.questions, 1), 'good')}
      ${metric('完整文章', num(q.passages), `${num(q.sources)} 个逻辑来源`)}
      ${metric('已有解析', num(q.explanations_available), pct(q.explanations_available / q.questions, 1))}
      ${metric('教学方法', num(q.teaching_methods), `${num(q.ocr_pages)} 页教材OCR`)}
    </section>
    <section class="panel" style="margin-top:18px">
      <div class="panel-head"><div><h2>完整语篇自动组卷</h2><p>按近期错题加权的 set-cover 选择尽量少的完整语篇；不会拆散文章</p></div></div>
      <div class="panel-body">
        <form id="passage-selector">
          <div class="check-grid">${suggestedTargets.map((row, index) => `<label class="check-card"><input type="checkbox" name="target_code" value="${esc(row.knowledge_point)}" ${index < 3 ? 'checked' : ''}><span><strong>${esc(row.name_cn)}</strong><small class="mono">${esc(row.knowledge_point)}</small></span></label>`).join('') || '<p class="subtle">尚无学生知识点证据，可在下方手工输入代码。</p>'}</div>
          <div class="toolbar" style="margin-top:14px"><div class="field grow"><label for="manual-targets">补充知识点代码（逗号分隔）</label><input id="manual-targets" name="manual_targets" placeholder="例如：tense, passive_voice"></div><div class="field"><label for="max-passages">最多语篇</label><input id="max-passages" name="max_passages" type="number" min="1" max="12" value="5"></div><button class="button" type="submit">生成选篇</button></div>
        </form>
        <div id="passage-selection-results"><p class="subtle">默认勾选当前证据最弱的三个知识点；机器建议标签不会被自动升级为已核验。</p></div>
      </div>
    </section>
    <section class="panel" style="margin-top:18px">
      <div class="panel-head"><div><h2>题目检索</h2><p>可按题干、答案、解析、考点或题目ID检索</p></div></div>
      <div class="panel-body">
        <form id="question-search" class="toolbar">
          <div class="field grow"><label for="question-query">关键词</label><input id="question-query" name="q" type="search" placeholder="例如：非谓语、文章主旨、Q-20F6…"></div>
          <div class="field"><label for="question-type">题型</label><select id="question-type" name="type"><option value="">全部题型</option>${summary.distributions.question_types.map(row => `<option>${esc(row.label)}</option>`).join('')}</select></div>
          <div class="field"><label for="question-status">核验状态</label><select id="question-status" name="status"><option value="">全部状态</option>${summary.distributions.verification.map(row => `<option>${esc(row.label)}</option>`).join('')}</select></div>
          <button class="button" type="submit">检索</button>
        </form>
        <div id="question-results"><div class="empty-state">输入关键词，或直接查看首批可用题目。</div></div>
      </div>
    </section>`;
  await searchQuestionRows();
}

async function selectCompletePassages() {
  const form = document.querySelector('#passage-selector');
  const host = document.querySelector('#passage-selection-results');
  const data = new FormData(form);
  const manual = String(data.get('manual_targets') || '').split(/[,，;；\s]+/).filter(Boolean);
  const targetCodes = [...new Set([...data.getAll('target_code'), ...manual])];
  if (!targetCodes.length) { notify('请至少选择一个知识点', true); return; }
  host.innerHTML = '<div class="empty-state">正在计算完整语篇覆盖…</div>';
  try {
    const result = await api('/api/grammar/select-passages', {method:'POST', body:JSON.stringify({target_codes: targetCodes, max_passages: Number(data.get('max_passages') || 5)})});
    const passages = result.selected_passages || [];
    host.innerHTML = `<div class="selection-summary"><p><strong>${num(passages.length)} 篇完整语篇</strong> · 算法 ${esc(result.algorithm)}</p>${passages.map((row,index) => `<article class="selection-card"><span>${index + 1}</span><div><strong>${esc(row.title || row.passage_id)}</strong><small class="mono">${esc(row.passage_id)} · ${num(row.question_count)} 空/题</small><p>${row.coverage_added.map(point => esc(point.code)).join(' · ')}</p></div></article>`).join('') || '<div class="empty-state">当前已核验完整语篇无法覆盖所选知识点。</div>'}<p class="subtle">未覆盖：${esc((result.uncovered || []).join('、') || '无')}；仅建议覆盖：${esc((result.suggested_only || []).join('、') || '无')}</p></div>`;
  } catch (error) { host.innerHTML = `<div class="error-state">${esc(error.message)}</div>`; }
}

async function searchQuestionRows() {
  const form = document.querySelector('#question-search');
  if (!form) return;
  const params = new URLSearchParams(new FormData(form)); params.set('limit', '60');
  const host = document.querySelector('#question-results');
  host.innerHTML = '<div class="empty-state">正在检索…</div>';
  try {
    const data = await api(`/api/questions?${params}`);
    host.innerHTML = `<p class="subtle">找到 ${num(data.total)} 道，当前显示 ${num(data.count)} 道。</p><div class="table-wrap"><table><thead><tr><th>题目</th><th>题型</th><th>知识点</th><th>年份/地区</th><th>状态</th><th></th></tr></thead><tbody>${data.items.map(row => `<tr><td><div class="mono">${esc(row.question_id)}</div><div class="truncate" title="${esc(row.stem)}">${esc(row.stem || '题干需回源查看')}</div></td><td>${esc(row.question_type)}</td><td>${esc(row.primary_test_point || '未标注')}<br><small class="subtle">${esc(row.secondary_test_points || '')}</small></td><td>${esc(row.year || '—')}<br><small class="subtle">${esc(row.district_or_school || '')}</small></td><td>${status(row.verification_status)}</td><td><button class="button button-secondary button-small" data-question="${esc(row.question_id)}">详情</button></td></tr>`).join('')}</tbody></table></div>`;
  } catch (error) { host.innerHTML = `<div class="error-state">${esc(error.message)}</div>`; }
}

async function openQuestion(id) {
  const data = await api(`/api/questions/${encodeURIComponent(id)}`);
  document.querySelector('#dialog-title').textContent = id;
  const knowledge = data.deep_knowledge || [];
  const enrichments = (data.enrichments || []).map(row => {
    let content; try { content = JSON.parse(row.content_json); } catch { content = row.content_json; }
    return `<section><h3>${esc(row.enrichment_type)}</h3><pre class="text-block">${esc(typeof content === 'string' ? content : JSON.stringify(content, null, 2))}</pre><p class="subtle">${esc(row.rationale)} · ${status(row.verification_status)}</p></section>`;
  }).join('');
  document.querySelector('#dialog-body').innerHTML = `
    <div class="chip-list">${[data.question_type, data.primary_test_point, data.difficulty, data.verification_status].filter(Boolean).map(v => `<span class="chip">${esc(v)}</span>`).join('')}</div>
    ${data.passage?.passage_text ? `<h3>完整语篇</h3><div class="text-block">${esc(data.passage.passage_text)}</div>` : ''}
    <h3>题干</h3><div class="text-block">${esc(data.stem || '请按来源路径回看原题。')}</div>
    ${data.options?.length ? `<h3>选项</h3><div class="definition-list">${data.options.map(o => `<div><dt>${esc(o.option_label)}</dt><dd>${esc(o.option_text)}</dd></div>`).join('')}</div>` : ''}
    <h3>答案与来源解析</h3><div class="text-block">${esc(data.answer || '暂无答案')}\n\n${esc(data.explanation_raw || '暂无来源解析')}</div>
    <h3>细粒度知识映射</h3><div class="table-wrap"><table><thead><tr><th>知识点</th><th>角色</th><th>来源</th><th>置信度</th><th>状态</th></tr></thead><tbody>${knowledge.map(k => `<tr><td>${esc(k.name_cn)}<br><small class="mono">${esc(k.code)}</small></td><td>${esc(k.role)}</td><td>${esc(k.mapping_source)}</td><td>${pct(k.confidence, 0)}</td><td>${status(k.verification_status)}</td></tr>`).join('') || '<tr><td colspan="5">尚未完成深层映射</td></tr>'}</tbody></table></div>
    ${enrichments}
    <h3>回源</h3><div class="mono">${esc(data.source_path)}${data.source_page ? `#page=${esc(data.source_page)}` : ''}</div>`;
  dialog.showModal();
}

async function renderMastery() {
  const data = await api('/api/mastery');
  const s = data.summary;
  const rows = data.knowledge_points || [];
  view.innerHTML = `
    <section class="metric-strip">
      ${metric('加权掌握率', pct(s.weighted_accuracy, 1), 'Beta先验仅用于知识点置信收缩')}
      ${metric('有效样本量', num(s.weighted_sample_size), `${num(s.attempt_count)} 次真实作答`)}
      ${metric('线下校准', pct(s.offline_calibration_accuracy, 1), `锚点样本 ${num(s.calibration_anchor_sample)}`)}
      ${metric('日常练习', pct(s.practice_accuracy, 1), '课堂、线上与家庭练习')}
      ${metric('校准差值', pct(s.calibration_gap, 1), '线下 − 日常')}
    </section>
    <div class="dashboard-grid">
      ${panel('知识点掌握明细', '所有薄弱点同时显示作答次数、题目数和加权样本', `<div class="table-wrap"><table><thead><tr><th>知识点</th><th>掌握度</th><th>加权正确率</th><th>作答/题目</th><th>错误</th><th>证据</th></tr></thead><tbody>${rows.map(row => `<tr><td>${esc(row.name_cn)}<br><small class="mono">${esc(row.knowledge_point)}</small></td><td><strong>${pct(row.calibrated_mastery, 1)}</strong></td><td>${pct(row.weighted_accuracy, 1)}</td><td>${num(row.attempt_count)} / ${num(row.distinct_item_count)}</td><td>${num(row.error_count)}</td><td>${status(row.confidence)}<br><small>${esc(row.confidence_cn)}</small></td></tr>`).join('') || '<tr><td colspan="6">尚无可计算知识点证据</td></tr>'}</tbody></table></div>`) }
      <div class="stack">
        ${panel('口径说明', '为什么线下测试权重更高', `<div class="callout"><h3>校准锚点优先</h3><p>正式线下闭卷整卷 1.60，双周线下混合测 1.40，线下专题测 1.20。课堂和家庭练习主要用于发现问题，不会压过受控线下证据。</p></div>`)}
        ${panel('证据边界', '系统不会伪造诊断', `<p>没有保存原始答案时，正确/错误仍是有效证据，但具体错因不能反推；只有一道错误证据时显示“暂定薄弱点”。OCR与规则标签在人工复核前只作建议。</p>`)}
      </div>
    </div>`;
}

async function renderAssessments() {
  const [records, weights] = await Promise.all([api('/api/assessments'), api('/api/weights')]);
  view.innerHTML = `
    <div class="dashboard-grid">
      <div class="stack">
        ${panel('录入一次测试', '周测、月测和正式线下整卷都写入同一数据库', `<form id="assessment-form"><div class="form-grid">
          <div class="field span-2"><label for="test-title">名称</label><input id="test-title" name="title" required placeholder="例如：8月第一周语法专题测"></div>
          <div class="field"><label for="test-date">日期</label><input id="test-date" name="date" type="date" required></div>
          <div class="field"><label for="assessment-kind">类型</label><select id="assessment-kind" name="assessment_kind"><option value="topic_quiz">专题小测</option><option value="biweekly_mixed_test">双周混合测</option><option value="full_exam">正式整卷</option><option value="dictation">听写</option><option value="lesson">课堂练习</option><option value="homework">作业</option></select></div>
          <div class="field"><label for="delivery-mode">环境</label><select id="delivery-mode" name="delivery_mode"><option value="offline_closed">线下闭卷</option><option value="offline_open">线下开卷</option><option value="online">线上</option><option value="home">家庭</option></select></div>
          <div class="field"><label for="series">趋势系列</label><input id="series" name="reporting_series" value="weekly_topic"></div>
          <div class="field"><label for="raw-score">得分</label><input id="raw-score" name="raw_score" type="number" min="0" step="0.5" required></div>
          <div class="field"><label for="max-score">满分</label><input id="max-score" name="max_score" type="number" min="0.5" step="0.5" required></div>
          <div class="field"><label for="duration">用时（秒）</label><input id="duration" name="duration_seconds" type="number" min="0"></div>
          <div class="field"><label for="blank-count">空白数</label><input id="blank-count" name="blank_count" type="number" min="0"></div>
        </div><div class="form-actions"><button class="button" type="submit">保存测试</button></div></form>`)}
        ${panel('测试记录', '不同满分、类型和系列不会连成一条原始分曲线', `<div class="table-wrap"><table><thead><tr><th>日期</th><th>测试</th><th>类型</th><th>环境</th><th>成绩</th><th>权重</th></tr></thead><tbody>${records.items.map(row => `<tr><td>${esc(String(row.started_at).slice(0,10))}</td><td>${esc(row.title)}</td><td>${esc(row.assessment_kind)}</td><td>${esc(row.delivery_mode)}</td><td>${row.raw_score == null ? '—' : `${num(row.raw_score)} / ${num(row.max_score)}`}</td><td>${num(row.evidence_weight)}</td></tr>`).join('') || '<tr><td colspan="6">尚无分类测试记录</td></tr>'}</tbody></table></div>`)}
      </div>
      <div class="stack">
        ${panel('证据权重', weights.policy_version, barList(weights.assessment_policies.filter(row => row.delivery_mode === 'offline_closed'), {label:'assessment_kind', value:'evidence_weight', max: 1.6, format: v => v.toFixed(2)}))}
        ${panel('题目修正项', '测试权重还会乘以题目与证据质量', `<div class="definition-list">${weights.question_rules.map(rule => `<div><dt>${esc(rule.dimension)} · ${esc(rule.match_value)}</dt><dd><strong>× ${Number(rule.multiplier).toFixed(2)}</strong>　${esc(rule.rationale)}</dd></div>`).join('')}</div>`)}
      </div>
    </div>`;
  document.querySelector('#test-date').value = new Date().toISOString().slice(0,10);
}

async function renderLibrary() {
  const [data, resources, candidates] = await Promise.all([
    api('/api/library'),
    api('/api/library/resources?limit=80'),
    api('/api/library/candidates?limit=40'),
  ]);
  const t = data.totals;
  const structure = data.structure || {};
  const sets = structure.source_sets || {};
  const quality = structure.candidate_quality || {};
  const review = structure.review_queue || {};
  view.innerHTML = `
    <section class="metric-strip">
      ${metric('英语源文件', num(t.english_resources), `${Number(t.english_gb || 0).toFixed(2)} GB`)}
      ${metric('文本结构化', num(t.completed_resources), `${pct(t.text_completion_rate, 1)} · ${num(t.text_resources)} 个文本文件`)}
      ${metric('候选题目', num(sets.question_candidates), `有答案 ${pct(quality.answer_coverage, 1)}`)}
      ${metric('RAG 文本块', num(sets.text_chunks), `${num(sets.source_sets)} 个逻辑来源`)}
      ${metric('待回源复核', num(review.open_reviews), '机器候选不冒充已核验')}
    </section>
    <div class="dashboard-grid">
      <div class="stack">
        ${panel('资料全文检索', '搜索已分块的原卷、解析与教材；结果保留回源路径', `<form id="library-search" class="search-row"><label class="sr-only" for="library-query">搜索资料</label><input id="library-query" name="q" placeholder="例如：非谓语 逻辑主语" required><button class="button" type="submit">搜索资料</button></form><div id="library-search-results" class="search-results"><p class="subtle">这里查的是源材料分块；已核验题库仍在“题库与知识”中查询。</p></div>`)}
        ${panel('结构化候选', `当前显示 ${num(candidates.count)} / ${num(candidates.total)} 道；仅 suggested / needs_check`, `<div class="table-wrap"><table><thead><tr><th>来源/题号</th><th>题型</th><th>题干</th><th>答案</th><th>状态</th></tr></thead><tbody>${candidates.items.map(row => `<tr><td><strong>${esc(row.source_title)}</strong><br><small class="mono">${esc(row.original_number)} · ${esc(row.candidate_question_id)}</small></td><td>${esc(row.question_type)}</td><td><div class="truncate" title="${esc(row.stem)}">${esc(row.stem)}</div></td><td>${esc(row.answer || '待匹配')}</td><td>${status(row.verification_status)}<br><small>${pct(row.confidence, 0)}</small></td></tr>`).join('') || '<tr><td colspan="5">尚无结构化候选</td></tr>'}</tbody></table></div>`)}
        ${panel('解析状态', '每个文件都有可审计状态', barList(data.by_status, {label:'parse_status', value:'resource_count'}))}
      </div>
      <div class="stack">
        ${panel('逻辑来源配对', '原卷、解析、音频与同内容版本合并管理', barList(structure.by_pairing_status, {label:'pairing_status', value:'source_sets'}))}
        ${panel('候选题型', '全量规则拆分结果，审核后才可进入正式题库', barList(structure.question_types, {label:'question_type', value:'question_candidates'}))}
        ${panel('复核队列', '缺答案、缺选项和缺语篇分开统计', `<div class="definition-list"><div><dt>缺答案</dt><dd>${num(review.missing_answers)}</dd></div><div><dt>缺选项</dt><dd>${num(review.missing_options)}</dd></div><div><dt>缺完整语篇</dt><dd>${num(review.missing_passages)}</dd></div><div><dt>知识映射</dt><dd>${num(quality.knowledge_mappings)} 条，规则生成均为 suggested</dd></div></div>`)}
        ${panel('来源分组', '按桌面题库主文件夹统计', barList(data.by_source_group, {label:'source_group', value:'resource_count'}))}
        ${panel('完成定义', '不再用口头百分比', `<div class="definition-list"><div><dt>INDEXED</dt><dd>文件路径、大小和类型已登记；音频已与同套题卷配对，但不等于逐字转写。</dd></div><div><dt>EXTRACTED</dt><dd>正文已提取并保留回源路径。</dd></div><div><dt>STRUCTURED</dt><dd>题目、文章、答案、解析和知识映射已拆分。</dd></div><div><dt>INGESTED</dt><dd>已进入正式题库，可被网站和课件接口查询。</dd></div><div><dt>NEEDS REVIEW</dt><dd>OCR、答案冲突或结构歧义需人工核验。</dd></div></div>`)}
        ${panel('最近文件', '派生库和系统代码不计入解析分母', `<div class="table-wrap"><table><thead><tr><th>文件</th><th>状态</th><th>提取</th></tr></thead><tbody>${resources.items.map(row => `<tr><td><div class="truncate" title="${esc(row.relative_path)}">${esc(row.file_name)}</div><small>${esc(row.extension)}</small></td><td>${status(row.parse_status)}</td><td>${esc(row.extraction_method || '—')}<br><small>${row.extracted_char_count ? `${num(row.extracted_char_count)} 字符` : ''}</small></td></tr>`).join('')}</tbody></table></div>`)}
      </div>
    </div>`;
}

async function searchLibraryMaterials() {
  const form = document.querySelector('#library-search');
  const host = document.querySelector('#library-search-results');
  const query = new FormData(form).get('q');
  host.innerHTML = '<p class="subtle">正在检索已分块材料……</p>';
  try {
    const data = await api(`/api/library/search?q=${encodeURIComponent(query)}&limit=30`);
    host.innerHTML = `<p class="subtle">找到 ${num(data.count)} 个材料片段。</p><div class="search-hit-list">${data.items.map(row => `<article class="search-hit"><div><strong>${esc(row.heading || row.source_title || row.file_name)}</strong>${status(row.verification_status)}</div><p>${esc(row.snippet)}</p><small class="mono">${esc(row.source_locator)}</small></article>`).join('') || '<div class="empty-state">未命中已分块材料</div>'}</div>`;
  } catch (error) {
    host.innerHTML = `<div class="error-state">${esc(error.message)}</div>`;
  }
}

async function renderDictation() {
  const data = await api('/api/dictation/plan?limit=20');
  state.dictation = data.items;
  view.innerHTML = `
    <section class="metric-strip">
      ${metric('本次清单', num(data.plan_size), '从到期复测队列自动生成')}
      ${metric('原始答案', '逐项保存', '允许后续分析真实错因', 'good')}
      ${metric('批改方式', '本地精确匹配', '无需再次消耗模型 token')}
      ${metric('写入位置', '统一数据库', '同一 session + attempts')}
      ${metric('OCR/API', '接口预留', '生产者后续按同一契约接入')}
    </section>
    <section class="panel" style="margin-top:18px">
      <div class="panel-head"><div><h2>今日听写</h2><p>保存后自动批改并进入复测调度</p></div><button class="button button-secondary button-small" data-action="dictation-refresh">换一批</button></div>
      <div class="panel-body">
        ${data.items.length ? `<form id="dictation-form"><div class="table-wrap"><table><thead><tr><th>#</th><th>提示</th><th>连续错误</th><th>学生原始答案</th></tr></thead><tbody>${data.items.map((row,index) => `<tr><td>${index+1}</td><td>${esc(row.prompt_snapshot || row.item_id)}<br><small class="mono subtle">${esc(row.item_id)}</small></td><td>${num(row.consecutive_errors)}</td><td><input name="answer-${index}" aria-label="第${index+1}题学生答案" autocomplete="off"></td></tr>`).join('')}</tbody></table></div><div class="form-actions"><button class="button" type="submit">批改并保存</button></div></form>` : '<div class="empty-state"><h2>当前没有到期词汇</h2><p>复测队列清空，或尚未导入词汇任务。</p></div>'}
      </div>
    </section>`;
}

async function renderWorkflow() {
  const data = await api('/api/workflow');
  view.innerHTML = `
    <div class="channel-grid">${data.channels.map(channel => `<article class="channel"><p class="eyebrow">${esc(channel.channel_key)}</p><h2>${esc(channel.display_name)}</h2><p>${esc(channel.responsibility)}</p><dl class="definition-list"><div><dt>读取</dt><dd>${esc(channel.reads_from)}</dd></div><div><dt>写入</dt><dd>${esc(channel.writes_through)}</dd></div><div><dt>实时上下文</dt><dd class="mono">http://127.0.0.1:8788${esc(channel.context_endpoint)}</dd></div><div><dt>状态</dt><dd>${status(channel.status)}</dd></div></dl><button class="button button-secondary button-small" data-copy="http://127.0.0.1:8788${esc(channel.context_endpoint)}">复制接口</button></article>`).join('')}</div>
    <section class="panel" style="margin-top:18px"><div class="panel-head"><div><h2>统一工作清单</h2><p>所有对话读取同一状态，不再重复手工写交接</p></div></div><div class="panel-body"><div class="table-wrap"><table><thead><tr><th>领域</th><th>任务</th><th>负责人</th><th>完成量</th><th>状态</th><th>证据</th></tr></thead><tbody>${data.work_items.map(item => `<tr><td>${esc(item.area)}</td><td>${esc(item.title)}</td><td>${esc(item.owner_channel)}</td><td>${num(item.completed_units)} / ${num(item.total_units)} ${esc(item.unit_label)}</td><td>${status(item.status)}</td><td class="mono">${esc(item.evidence_path || '—')}</td></tr>`).join('')}</tbody></table></div></div></section>
    <div class="callout" style="margin-top:18px"><h3>以后怎么交接</h3><p>另一个对话优先读取自己的实时上下文接口；需要命令细节时再看 HANDOFF_FOR_THREADS.md。课堂和听写结果统一通过 API/JSON 契约写入，不直接改 SQLite。</p></div>`;
}

async function render(force = false) {
  loading();
  const [title, label] = titles[state.current]; pageTitle.textContent = title; eyebrow.textContent = label;
  document.querySelectorAll('.nav-item').forEach(button => button.classList.toggle('is-active', button.dataset.view === state.current));
  try {
    if (state.current === 'overview') await renderOverview(force);
    if (state.current === 'question-bank') await renderQuestionBank();
    if (state.current === 'mastery') await renderMastery();
    if (state.current === 'assessments') await renderAssessments();
    if (state.current === 'library') await renderLibrary();
    if (state.current === 'dictation') await renderDictation();
    if (state.current === 'workflow') await renderWorkflow();
    freshness.textContent = `数据刷新于 ${new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'})}`;
  } catch (error) { errorState(error); }
}

document.addEventListener('click', async event => {
  const nav = event.target.closest('[data-view], [data-nav]');
  if (nav) { state.current = nav.dataset.view || nav.dataset.nav; await render(); return; }
  if (event.target.closest('[data-action="refresh"]')) { state.cache = {}; await render(true); return; }
  if (event.target.closest('[data-action="dictation-refresh"]')) { await renderDictation(); return; }
  const question = event.target.closest('[data-question]');
  if (question) { try { await openQuestion(question.dataset.question); } catch (error) { notify(error.message, true); } return; }
  const copy = event.target.closest('[data-copy]');
  if (copy) { await navigator.clipboard.writeText(copy.dataset.copy); notify('接口地址已复制'); }
});
document.querySelector('#refresh-button').addEventListener('click', async () => { state.cache = {}; await render(true); });
document.addEventListener('submit', async event => {
  if (event.target.id === 'question-search') { event.preventDefault(); await searchQuestionRows(); }
  if (event.target.id === 'passage-selector') { event.preventDefault(); await selectCompletePassages(); }
  if (event.target.id === 'library-search') { event.preventDefault(); await searchLibraryMaterials(); }
  if (event.target.id === 'assessment-form') {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(event.target));
    try { await api('/api/assessments', {method:'POST', body:JSON.stringify(body)}); notify('测试已保存，并已创建备份'); state.cache = {}; await renderAssessments(); }
    catch (error) { notify(error.message, true); }
  }
  if (event.target.id === 'dictation-form') {
    event.preventDefault();
    const form = new FormData(event.target);
    const items = state.dictation.map((row,index) => ({item_id: row.item_id, student_answer: form.get(`answer-${index}`) || ''}));
    try {
      const result = await api('/api/dictation/results', {method:'POST', body:JSON.stringify({items, delivery_mode:'offline_closed'})});
      notify(`已保存：${result.correct}/${result.total}`); state.cache = {}; await renderDictation();
    } catch (error) { notify(error.message, true); }
  }
});

async function boot() {
  try {
    const health = await api('/api/health');
    document.querySelector('#health-label').textContent = health.status === 'ok' ? '数据已连接' : '需要关注';
    document.querySelector('#health-dot').className = `health-dot ${health.status === 'ok' ? 'ok' : 'error'}`;
  } catch {
    document.querySelector('#health-label').textContent = '连接失败';
    document.querySelector('#health-dot').className = 'health-dot error';
  }
  await render();
}
boot();
