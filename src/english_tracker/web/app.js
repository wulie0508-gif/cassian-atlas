const view = document.querySelector('#view');
const pageTitle = document.querySelector('#page-title');
const eyebrow = document.querySelector('#section-eyebrow');
const freshness = document.querySelector('#freshness');
const detailButton = document.querySelector('#detail-mode-button');
const studentSelect = document.querySelector('#student-select');
const subjectSelect = document.querySelector('#subject-select');
const localeSelect = document.querySelector('#locale-select');
const toast = document.querySelector('#toast');
const dialog = document.querySelector('#question-dialog');
const appStatus = document.querySelector('#app-status');
const moreButton = document.querySelector('#more-nav-button');
const mobileMoreMenu = document.querySelector('#mobile-more-menu');
const settingsButton = document.querySelector('#settings-button');
const settingsMenu = document.querySelector('#settings-menu');
const initialQuery = new URLSearchParams(window.location.search);
const state = {
  current: 'overview',
  cache: {},
  detailMode: window.localStorage.getItem('open-tutor-ledger-detail-mode') === 'true',
  locale: window.localStorage.getItem('open-tutor-locale') || 'zh-CN',
  studentId: initialQuery.get('student_id') || window.localStorage.getItem('open-tutor-student') || '',
  subjectCode: window.localStorage.getItem('open-tutor-subject') || 'english',
  students: [],
  config: null,
  renderGeneration: 0,
  renderController: null,
  bootController: null,
  auxiliaryControllers: new Map(),
  dialogTrigger: null,
};

const titles = {
  overview: ['教学总览', '教学决策'],
  'question-bank': ['题库检索', '资料与系统'],
  mastery: ['趋势与薄弱点', '教学分析'],
  assessments: ['课程与测验', '学习记录'],
  library: ['资料解析', '资料与系统'],
  dictation: ['词汇复测', '教学任务'],
  workflow: ['Agent 运行', '资料与系统'],
};

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}
function num(value) { return value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value); }
function normalizedRate(value) {
  if (value == null || value === '') return null;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  return Math.abs(numeric) > 1 ? numeric / 100 : numeric;
}
function pct(value, digits = 0) {
  const rate = normalizedRate(value);
  return rate == null ? '暂无' : `${(rate * 100).toFixed(digits)}%`;
}
function signedPct(value, digits = 1) {
  const rate = normalizedRate(value);
  if (rate == null) return '暂无变化数据';
  const sign = rate > 0 ? '+' : '';
  return `${sign}${(rate * 100).toFixed(digits)} 个百分点`;
}
function gb(bytes) { return bytes == null ? '—' : `${(Number(bytes) / 1073741824).toFixed(2)} GB`; }
function status(value) { return `<span class="status-tag ${esc(value)}">${esc(value || '未标注')}</span>`; }
function announce(message) { appStatus.textContent = message; }
function notify(message, isError = false) {
  toast.setAttribute('role', isError ? 'alert' : 'status');
  toast.setAttribute('aria-live', isError ? 'assertive' : 'polite');
  toast.textContent = message; toast.className = `toast show${isError ? ' error' : ''}`;
  window.setTimeout(() => {
    toast.className = 'toast';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
  }, 2600);
}
async function api(path, signal = undefined) {
  const url = new URL(path, window.location.origin);
  const publicEndpoints = new Set(['/api/health', '/api/app-config', '/api/students']);
  if (state.studentId && !publicEndpoints.has(url.pathname)) url.searchParams.set('student_id', state.studentId);
  const response = await fetch(`${url.pathname}${url.search}`, {
    headers: {'Accept': 'application/json'},
    signal,
  });
  const data = await response.json().catch(() => ({error: `HTTP ${response.status}`}));
  if (!response.ok) throw new Error(data.error || `请求失败：${response.status}`);
  return data;
}
function loading() {
  view.setAttribute('aria-busy', 'true');
  announce('正在读取教学数据');
  view.innerHTML = '<div class="loading-state" aria-busy="true"><div class="skeleton skeleton-title"></div><div class="skeleton-grid"><div></div><div></div><div></div></div></div>';
}
function errorState(error) {
  view.setAttribute('aria-busy', 'false');
  announce(`读取失败：${error.message}`);
  view.innerHTML = `<div class="panel error-state" role="alert"><h2>读取失败</h2><p>${esc(error.message)}</p><button class="button button-secondary" data-action="refresh">重新读取</button></div>`;
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

function selectedStudent() {
  return state.students.find(student => student.student_id === state.studentId) || null;
}

function selectedSubjectName() {
  const subject = (state.config?.subjects || []).find(item => item.subject_code === state.subjectCode);
  return subject ? subjectLabel(subject) : (state.subjectCode || '');
}

function beginRenderRequest() {
  state.renderController?.abort();
  state.auxiliaryControllers.forEach(controller => controller.abort());
  state.auxiliaryControllers.clear();
  state.renderController = new AbortController();
  state.renderGeneration += 1;
  return {generation: state.renderGeneration, signal: state.renderController.signal};
}

function beginAuxiliaryRequest(key) {
  state.auxiliaryControllers.get(key)?.abort();
  const controller = new AbortController();
  state.auxiliaryControllers.set(key, controller);
  return controller;
}

function ensureCurrent(context) {
  if (!context || context.generation !== state.renderGeneration || context.signal.aborted) {
    throw new DOMException('Stale dashboard response', 'AbortError');
  }
}

function enhanceReadOnlyTables(root = view) {
  root.querySelectorAll('table').forEach((table, index) => {
    if (!table.querySelector('caption')) {
      const caption = document.createElement('caption');
      caption.className = 'sr-only';
      caption.textContent = table.closest('.panel')?.querySelector('h2')?.textContent || `数据表 ${index + 1}`;
      table.prepend(caption);
    }
    table.querySelectorAll('thead th').forEach(cell => cell.setAttribute('scope', 'col'));
    const wrapper = table.closest('.table-wrap');
    if (wrapper) {
      wrapper.setAttribute('role', 'region');
      wrapper.setAttribute('tabindex', '0');
      wrapper.setAttribute('aria-label', table.querySelector('caption').textContent);
    }
  });
}

function isMobileNavigation() {
  return window.matchMedia('(max-width: 900px)').matches;
}

function closeTransientMenus({restoreFocus = false} = {}) {
  const moreWasOpen = mobileMoreMenu.classList.contains('is-open');
  const settingsWasOpen = settingsMenu.classList.contains('is-open');
  mobileMoreMenu.classList.remove('is-open');
  moreButton.setAttribute('aria-expanded', 'false');
  settingsMenu.classList.remove('is-open');
  settingsButton.setAttribute('aria-expanded', 'false');
  if (restoreFocus && moreWasOpen && moreButton.isConnected) moreButton.focus();
  if (restoreFocus && settingsWasOpen && settingsButton.isConnected) settingsButton.focus();
}

function applyDetailMode() {
  document.body.classList.toggle('detail-mode', state.detailMode);
  detailButton.setAttribute('aria-pressed', String(state.detailMode));
  detailButton.textContent = state.detailMode ? '返回减负模式' : '查看专业数据';
}

function applyLocale() {
  window.CassianAtlasI18n?.apply(document, state.locale);
}

function subjectLabel(subject) {
  return state.locale === 'en' ? subject.name_en : subject.name_cn;
}

function knowledgeLabel(row) {
  const raw = state.locale === 'en' ? (row.name_en || row.knowledge_point) : (row.name_cn || row.knowledge_point);
  if (state.locale !== 'en') return raw;
  return String(raw || '').replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase());
}

function persistStudentSelection() {
  window.localStorage.setItem('open-tutor-student', state.studentId);
  const url = new URL(window.location.href);
  if (state.studentId) url.searchParams.set('student_id', state.studentId);
  else url.searchParams.delete('student_id');
  window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
}

function populateWorkspaceControls() {
  studentSelect.innerHTML = state.students.map(student => `<option value="${esc(student.student_id)}">${esc(student.display_name || student.student_id)}</option>`).join('');
  studentSelect.value = state.studentId;
  const selectedStudent = state.students.find(student => student.student_id === state.studentId);
  const enrolledCodes = new Set((selectedStudent?.subjects || []).map(subject => subject.subject_code));
  const subjects = (state.config?.subjects || []).filter(subject => enrolledCodes.has(subject.subject_code));
  const availableSubjectCodes = new Set(subjects.map(subject => subject.subject_code));
  if (!availableSubjectCodes.has(state.subjectCode)) {
    state.subjectCode = availableSubjectCodes.has('english') ? 'english' : (subjects[0]?.subject_code || '');
    if (state.subjectCode) window.localStorage.setItem('open-tutor-subject', state.subjectCode);
    else window.localStorage.removeItem('open-tutor-subject');
  }
  subjectSelect.innerHTML = subjects.map(subject => `<option value="${esc(subject.subject_code)}">${esc(subjectLabel(subject))}</option>`).join('');
  subjectSelect.value = state.subjectCode;
  subjectSelect.disabled = subjects.length === 0;
  studentSelect.disabled = state.students.length === 0;
  document.body.classList.toggle('single-subject', subjects.length <= 1);
  localeSelect.value = state.locale;
  applyLocale();
}

async function refreshStudents(preferredId = '', signal = undefined) {
  const data = await api('/api/students', signal);
  state.students = data.items || [];
  const available = new Set(state.students.map(item => item.student_id));
  state.studentId = available.has(preferredId) ? preferredId : (available.has(state.studentId) ? state.studentId : (state.students[0]?.student_id || ''));
  persistStudentSelection();
  populateWorkspaceControls();
}

function renderEmptyStudentState() {
  pageTitle.textContent = '教学总览';
  eyebrow.textContent = '教学决策';
  view.setAttribute('aria-busy', 'false');
  view.innerHTML = `<section class="panel empty-workspace" role="status"><div class="panel-body"><h2>尚未添加学生</h2><p>请直接告诉 Codex 学生姓名和基本情况。学生建立后，这里会自动显示教学证据。</p></div></section>`;
  announce('当前没有学生，请通过 Codex 添加学生');
}

async function renderSubjectWorkspace(context) {
  const data = await api(`/api/subject-overview?subject_code=${encodeURIComponent(state.subjectCode)}`, context.signal);
  ensureCurrent(context);
  const subject = data.subject;
  const summary = data.summary;
  const name = subjectLabel(subject);
  pageTitle.textContent = name;
  eyebrow.textContent = 'SUBJECT WORKSPACE';
  view.innerHTML = `
    <section class="relief-hero compact-hero">
      <div class="relief-copy"><p class="eyebrow">${esc(subject.subject_code.toUpperCase())} · GENERIC ADAPTER</p><h2>学科工作区已就绪</h2><p>这个学科已经可以接收通用课堂、作业与测试记录；专用题库和知识树由独立适配器逐步接入。</p></div>
      <div class="next-action"><span>${data.capabilities.specialized_adapter ? '已接入' : '待扩展'}</span><strong>${esc(name)} · ${data.capabilities.specialized_adapter ? '专用适配器' : '通用接口可用'}</strong><p>Agent 写入时为题目设置 subject_code=${esc(subject.subject_code)}，数据会与其他学科严格分开。</p></div>
    </section>
    <section class="quick-facts" aria-label="学科摘要">
      ${metric('学习活动', num(summary.session_count), '按学生与学科隔离')}
      ${metric('题目证据', num(summary.attempt_count), `${num(summary.item_count)} 个不同项目`)}
      ${metric('当前正确率', pct(summary.accuracy, 1), `${num(summary.scored_count)} 次有效评分`)}
    </section>
    ${summary.attempt_count ? panel('最后活动', '当前学科最近一次有证据的时间', `<p><strong>${esc(summary.last_activity_at || '—')}</strong></p>`) : panel('当前没有学习记录', '这是正常的空工作区', '<div class="empty-state"><p>把第一次课堂或测试结果交给 Agent 后，这里会自动出现证据。</p></div>')}`;
}

function firstValue(row, keys, fallback = null) {
  for (const key of keys) {
    if (row?.[key] !== undefined && row?.[key] !== null && row?.[key] !== '') return row[key];
  }
  return fallback;
}

function compactDate(value) {
  if (!value) return '日期待补充';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value).slice(0, 10);
  return parsed.toLocaleDateString(state.locale === 'en' ? 'en' : 'zh-CN', {month: '2-digit', day: '2-digit'});
}

function priorityName(row) {
  return firstValue(row, ['name_cn', 'label', 'knowledge_name', 'title', 'knowledge_point'], '待确认知识点');
}

function priorityRate(row) {
  return firstValue(row, ['accuracy', 'weighted_accuracy', 'calibrated_mastery', 'mastery']);
}

function priorityEvidence(row) {
  const attempts = firstValue(row, ['attempt_count', 'evidence_count', 'sample_size'], 0);
  const errors = firstValue(row, ['repeated_error_count', 'repeat_error_count', 'error_count'], 0);
  const confidence = firstValue(row, ['confidence_cn', 'confidence_label', 'confidence'], '证据积累中');
  return `${num(attempts)} 次作答，${num(errors)} 次错误，${confidence}`;
}

function isStablePriority(row) {
  const confidence = String(firstValue(row, ['confidence', 'confidence_cn'], '')).toLowerCase();
  const repeated = Number(firstValue(row, ['repeated_error_count', 'repeat_error_count', 'error_count'], 0));
  return Boolean(row?.is_stable) || repeated >= 2 || ['moderate', 'established', 'stable', '较高可信', '稳定'].some(token => confidence.includes(token));
}

function dueDomainSummary(raw) {
  if (!raw) return '当前没有分领域数据';
  const labels = {grammar: '语法', vocabulary: '词汇', reading: '阅读', translation: '翻译', writing: '写作', listening: '听力'};
  const rows = Array.isArray(raw)
    ? raw.map(row => [firstValue(row, ['domain_cn', 'domain', 'label'], '其他'), firstValue(row, ['count', 'due_count', 'value'], 0)])
    : Object.entries(raw);
  return rows.filter(([, count]) => Number(count) > 0).map(([label, count]) => `${labels[label] || label} ${num(count)}`).join('，') || '当前没有到期任务';
}

function friendlySeriesLabel(performance) {
  const raw = performance?.series_label || performance?.reporting_series || performance?.assessment_kind || '同口径训练';
  const labels = {
    classroom_lesson: '课堂练习', lesson: '课堂练习', homework: '家庭练习',
    'grammar-fill': '语法填空', 'morning-review': '早读检测', dictation: '词汇听写',
  };
  return labels[raw] || String(raw).replaceAll('_', ' ');
}

function signalText(item) {
  if (typeof item === 'string') return item;
  if (item?.knowledge_point || item?.name_cn) return `${priorityName(item)}：${priorityEvidence(item)}`;
  const title = firstValue(item, ['title', 'label', 'signal'], '待确认事项');
  const detail = firstValue(item, ['detail', 'rationale', 'message'], '');
  return detail ? `${title}：${detail}` : title;
}

function trendFigure(performance) {
  const items = (performance?.items || performance?.points || []).slice(-6);
  const series = friendlySeriesLabel(performance);
  if (!items.length) return '<div class="empty-state compact"><p>暂无可比较的同口径训练记录。</p></div>';
  const rates = items.map(item => normalizedRate(firstValue(item, ['accuracy', 'derived_accuracy', 'rate'])) ?? 0);
  const width = 640;
  const height = 190;
  const left = 42;
  const right = 20;
  const top = 18;
  const bottom = 42;
  const usableWidth = width - left - right;
  const usableHeight = height - top - bottom;
  const points = rates.map((rate, index) => {
    const x = items.length === 1 ? left + usableWidth / 2 : left + usableWidth * index / (items.length - 1);
    const y = top + usableHeight * (1 - Math.max(0, Math.min(1, rate)));
    return {x, y, rate, item: items[index]};
  });
  const description = points.map(point => `${compactDate(firstValue(point.item, ['started_at', 'date', 'label']))} ${pct(point.rate, 1)}`).join('；');
  const grid = [0, .5, 1].map(rate => {
    const y = top + usableHeight * (1 - rate);
    return `<line x1="${left}" y1="${y}" x2="${width - right}" y2="${y}" class="trend-gridline"/><text x="${left - 8}" y="${y + 4}" text-anchor="end">${Math.round(rate * 100)}%</text>`;
  }).join('');
  const pointMarkup = points.map(point => `<circle cx="${point.x}" cy="${point.y}" r="5"><title>${esc(compactDate(firstValue(point.item, ['started_at', 'date', 'label'])))} ${esc(pct(point.rate, 1))}</title></circle>`).join('');
  const labels = points.map(point => `<text x="${point.x}" y="${height - 14}" text-anchor="middle">${esc(compactDate(firstValue(point.item, ['started_at', 'date', 'label'])))}</text>`).join('');
  const textRows = points.map(point => `<li><span>${esc(compactDate(firstValue(point.item, ['started_at', 'date', 'label'])))}</span><strong>${esc(pct(point.rate, 1))}</strong></li>`).join('');
  return `<figure class="trend-figure"><figcaption><strong>${esc(series)}</strong><span>仅比较同一训练系列，最近 ${num(items.length)} 次</span></figcaption><svg viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="trend-title trend-description"><title id="trend-title">${esc(series)}表现趋势</title><desc id="trend-description">${esc(description)}</desc>${grid}<polyline points="${points.map(point => `${point.x},${point.y}`).join(' ')}" class="trend-line"/>${pointMarkup}${labels}</svg><ol class="trend-text-summary sr-only">${textRows}</ol></figure>`;
}

function coverageMarkup(coverage) {
  const labels = {
    session_count: '已有课程', attempt_count: '已有作答', scored_attempt_count: '有效评分',
    captured_answer_count: '保留原始答案', knowledge_point_count: '覆盖知识点',
    question_count: '关联题目', coverage_rate: '数据覆盖率', last_activity_at: '最近活动',
    active_attempts: '有效作答', scored_attempts: '可评分作答', answer_capture: '原始答案留存',
    knowledge_map: '知识点映射', specific_knowledge_map: '细分知识点映射',
    confirmed_knowledge_map: '已确认知识映射', error_diagnosis: '错因诊断', timing: '用时记录',
  };
  const rows = [];
  Object.entries(coverage || {}).forEach(([key, value]) => {
    if (value == null) return;
    let display;
    if (typeof value === 'object') {
      const numerator = firstValue(value, ['numerator'], 0);
      const denominator = firstValue(value, ['denominator'], 0);
      display = `${num(numerator)} / ${num(denominator)}${value.rate == null ? '' : `，${pct(value.rate, 1)}`}`;
    } else {
      display = key.endsWith('_rate') || key.endsWith('_accuracy') ? pct(value, 1) : String(value);
    }
    rows.push(`<div><dt>${esc(labels[key] || key.replaceAll('_', ' '))}</dt><dd>${esc(display)}</dd></div>`);
  });
  return rows.slice(0, 10).join('') || '<div><dt>状态</dt><dd>暂无覆盖统计</dd></div>';
}

async function renderOverview(force = false, context) {
  const cacheKey = `${state.studentId}:${state.subjectCode}`;
  let data = !force && state.cache.teacherDashboard?.key === cacheKey ? state.cache.teacherDashboard.data : null;
  if (!data) data = await api(`/api/teacher/dashboard?subject_code=${encodeURIComponent(state.subjectCode)}`, context.signal);
  ensureCurrent(context);
  state.cache.teacherDashboard = {key: cacheKey, data};
  const student = selectedStudent();
  const studentName = student?.display_name || student?.student_id || '未选择学生';
  const subjectName = selectedSubjectName() || '英语';
  pageTitle.textContent = `${studentName} · ${subjectName}`;
  eyebrow.textContent = '教学总览';

  const performance = data.comparable_performance || {};
  const priorities = (data.teaching_priorities || []).slice(0, 5);
  const stableCount = priorities.length;
  const confirmationSignals = (data.confirmation_signals || []).slice(0, 6);
  const review = data.review_health || {};
  const calibration = data.calibration || {};
  const sessions = (data.recent_sessions || []).slice(0, 5);
  const agent = data.agent_summary || {};
  const nextAction = data.next_action || {};
  const calibrationReady = ['ready', 'calibrated', 'available', 'complete'].includes(String(calibration.status || '').toLowerCase()) || Number(calibration.anchor_count || 0) > 0;
  const prompt = firstValue(nextAction, ['prompt', 'codex_prompt'], '');

  const actionPriorities = priorities.slice(0, 3).map((row, index) => `<li><span>${index + 1}</span><div><strong>${esc(priorityName(row))}</strong><small>${esc(priorityEvidence(row))}</small></div><b>${esc(pct(priorityRate(row), 0))}</b></li>`).join('');
  const signalRows = confirmationSignals.map(item => `<li>${esc(signalText(item))}</li>`).join('');
  const priorityRows = priorities.map(row => `<article class="evidence-row"><div><strong>${esc(priorityName(row))}</strong><small>${esc(priorityEvidence(row))}</small><small class="mono detail-only">${esc(firstValue(row, ['knowledge_point', 'code'], ''))}</small></div><div><b>${esc(pct(priorityRate(row), 1))}</b><span class="evidence-state ${isStablePriority(row) ? 'stable' : ''}">${isStablePriority(row) ? '稳定信号' : '继续观察'}</span></div></article>`).join('');
  const sessionRows = sessions.map(row => {
    const date = compactDate(firstValue(row, ['started_at', 'date']));
    const accuracy = firstValue(row, ['accuracy', 'weighted_accuracy']);
    const attempts = firstValue(row, ['attempt_count', 'scored_attempt_count'], 0);
    return `<article><time>${esc(date)}</time><div><strong>${esc(firstValue(row, ['title', 'session_title'], '未命名课程'))}</strong><small>${num(attempts)} 次作答</small></div><b>${esc(pct(accuracy, 1))}</b></article>`;
  }).join('');
  const dueTotal = Number(firstValue(review, ['due_total', 'due_count', 'open_due_total'], 0));
  const recovery = firstValue(review, ['retest_recovery_rate', 'recovery_rate'], review.latest_retest_recovery?.rate);
  const change = firstValue(performance, ['change']);
  const changeRate = normalizedRate(change);
  const latestAccuracy = firstValue(performance, ['latest_accuracy'], performance.latest?.accuracy);
  const seriesLabel = friendlySeriesLabel(performance);
  const freshnessValue = data.generated_at || (typeof data.freshness === 'string'
    ? data.freshness
    : firstValue(data.freshness || {}, ['latest_at', 'generated_at'], null));
  const freshnessStatus = typeof data.freshness === 'object' ? firstValue(data.freshness, ['status_cn', 'label'], '') : '';
  if (freshnessValue) freshness.textContent = `${freshnessStatus ? `${freshnessStatus}，` : ''}数据更新 ${compactDate(freshnessValue)} ${String(freshnessValue).includes('T') ? new Date(freshnessValue).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'}) : ''}`.trim();

  view.innerHTML = `
    <section class="teacher-kpis" aria-label="教学判断摘要">
      ${metric('近期可比正确率', pct(latestAccuracy, 1), `${esc(seriesLabel)}，${signedPct(change, 1)}`, changeRate == null || changeRate >= 0 ? 'good' : 'attention')}
      ${metric('到期复测', num(dueTotal), dueDomainSummary(review.due_by_domain || review.open_due_by_domain), dueTotal ? 'attention' : 'good')}
      ${metric('稳定薄弱点', num(stableCount), `当前优先查看 ${num(priorities.length)} 项`, stableCount ? 'attention' : 'good')}
      ${metric('线下校准', calibrationReady ? '已建立' : '待补充', `${num(calibration.anchor_count || 0)} 个校准锚点`, calibrationReady ? 'good' : 'attention')}
    </section>
    <div class="decision-grid">
      <section class="panel next-lesson-panel">
        <div class="panel-head"><div><h2>下一节优先处理</h2><p>${esc(firstValue(nextAction, ['rationale', 'reason'], '依据近期同口径表现、重复错误和到期复测生成。'))}</p></div></div>
        <div class="panel-body"><h3>${esc(firstValue(nextAction, ['title'], priorities.length ? `先处理 ${priorityName(priorities[0])}` : '等待更多学习证据'))}</h3><ol class="priority-brief">${actionPriorities || '<li class="empty-inline">暂无稳定薄弱点，继续收集同口径作答。</li>'}</ol>${prompt ? `<div class="codex-prompt"><p>${esc(prompt)}</p><button class="button button-secondary button-small" type="button" data-copy-prompt="${esc(prompt)}">复制给 Codex</button></div>` : ''}</div>
      </section>
      <aside class="panel attention-panel">
        <div class="panel-head"><div><h2>待处理与证据缺口</h2><p>只列出会影响教学判断的事项</p></div></div>
        <div class="panel-body"><ul class="signal-list">${signalRows || '<li>当前没有需要人工确认的事项。</li>'}</ul><dl class="attention-facts"><div><dt>复测恢复率</dt><dd>${pct(recovery, 1)}</dd></div><div><dt>线下校准</dt><dd>${calibrationReady ? '已建立' : '尚未建立'}</dd></div></dl></div>
      </aside>
    </div>
    <section class="panel trend-panel">
      <div class="panel-head"><div><h2>近期同口径表现</h2><p>不同题型和不同评分口径不会连成一条趋势</p></div><span class="trend-change ${changeRate == null ? '' : (changeRate >= 0 ? 'positive' : 'negative')}">${esc(signedPct(change, 1))}</span></div>
      <div class="panel-body">${trendFigure(performance)}</div>
    </section>
    <div class="evidence-grid">
      ${panel('Top 5 薄弱点证据', '同时显示作答量、错误次数和证据稳定性', `<div class="evidence-list">${priorityRows || '<div class="empty-state compact">暂无知识点证据</div>'}</div>`, '<button class="panel-action" data-nav="mastery">查看完整分析</button>')}
      ${panel('最近有作答的课程', '最多显示最近 5 次，不混入没有作答的课次', `<div class="recent-session-list">${sessionRows || '<div class="empty-state compact">暂无课程作答</div>'}</div>`, '<button class="panel-action" data-nav="assessments">查看全部记录</button>')}
    </div>
    <details class="coverage-details"><summary>数据覆盖与口径</summary><div class="coverage-body"><dl class="definition-list">${coverageMarkup(data.data_coverage || {})}</dl><p>综合正确率只用于学习证据分析，不等同于正式考试成绩。</p></div></details>
    <section class="agent-status-line" aria-label="Agent 状态"><span class="health-dot ${Number(agent.failed || 0) ? 'error' : 'ok'}"></span><strong>Agent 状态</strong><span>处理中 ${num(agent.active || 0)}</span><span>等你确认 ${num(agent.needs_input || 0)}</span><span>失败 ${num(agent.failed || 0)}</span><button class="panel-action" data-nav="workflow">查看运行记录</button></section>`;
}

async function renderQuestionBank(context) {
  const [summary, mastery] = await Promise.all([
    state.cache.questionBank || api('/api/question-bank', context.signal),
    api('/api/mastery', context.signal),
  ]);
  ensureCurrent(context);
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
      <div class="panel-head"><div><h2>当前薄弱知识点</h2><p>从数据库读取；选题与组卷由 Codex/CLI 完成</p></div></div>
      <div class="panel-body">
        <div class="check-grid">${suggestedTargets.map(row => `<article class="check-card readonly-card"><span><strong>${esc(knowledgeLabel(row))}</strong><small class="mono">${esc(row.knowledge_point)}</small></span><b>${pct(row.calibrated_mastery, 0)}</b></article>`).join('') || '<div class="empty-state">尚无学生知识点证据</div>'}</div>
        <p class="subtle readonly-note">看板只展示证据，不在网页中创建练习。把选题要求直接告诉 Codex 即可。</p>
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
  await searchQuestionRows(context);
}

async function searchQuestionRows(context = null) {
  const form = document.querySelector('#question-search');
  if (!form) return;
  const controller = context ? null : beginAuxiliaryRequest('question-search');
  const signal = context?.signal || controller.signal;
  const params = new URLSearchParams(new FormData(form)); params.set('limit', '60');
  const host = document.querySelector('#question-results');
  host.setAttribute('aria-busy', 'true');
  host.innerHTML = '<div class="empty-state">正在检索…</div>';
  try {
    const data = await api(`/api/questions?${params}`, signal);
    if (context) ensureCurrent(context);
    if (!host.isConnected) return;
    host.innerHTML = `<p class="subtle">找到 ${num(data.total)} 道，当前显示 ${num(data.count)} 道。</p><div class="table-wrap"><table><thead><tr><th>题目</th><th>题型</th><th>知识点</th><th>年份/地区</th><th>状态</th><th></th></tr></thead><tbody>${data.items.map(row => `<tr><td><div class="mono">${esc(row.question_id)}</div><div class="truncate" title="${esc(row.stem)}">${esc(row.stem || '题干需回源查看')}</div></td><td>${esc(row.question_type)}</td><td>${esc(row.primary_test_point || '未标注')}<br><small class="subtle">${esc(row.secondary_test_points || '')}</small></td><td>${esc(row.year || '—')}<br><small class="subtle">${esc(row.district_or_school || '')}</small></td><td>${status(row.verification_status)}</td><td><button class="button button-secondary button-small" data-question="${esc(row.question_id)}">详情</button></td></tr>`).join('')}</tbody></table></div>`;
    enhanceReadOnlyTables(host);
    host.setAttribute('aria-busy', 'false');
    announce(`检索完成，找到 ${num(data.total)} 道题，当前显示 ${num(data.count)} 道`);
  } catch (error) {
    if (error.name !== 'AbortError' && host.isConnected) {
      host.setAttribute('aria-busy', 'false');
      host.innerHTML = `<div class="error-state" role="alert">${esc(error.message)}</div>`;
      announce('题目检索失败');
    }
  }
}

async function openQuestion(id) {
  const controller = beginAuxiliaryRequest('question-detail');
  const data = await api(`/api/questions/${encodeURIComponent(id)}`, controller.signal);
  if (controller.signal.aborted) return;
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
    ${data.options?.length ? `<h3>选项</h3><dl class="definition-list">${data.options.map(o => `<div><dt>${esc(o.option_label)}</dt><dd>${esc(o.option_text)}</dd></div>`).join('')}</dl>` : ''}
    <h3>答案与来源解析</h3><div class="text-block">${esc(data.answer || '暂无答案')}\n\n${esc(data.explanation_raw || '暂无来源解析')}</div>
    <h3>细粒度知识映射</h3><div class="table-wrap"><table><thead><tr><th>知识点</th><th>角色</th><th>来源</th><th>置信度</th><th>状态</th></tr></thead><tbody>${knowledge.map(k => `<tr><td>${esc(k.name_cn)}<br><small class="mono">${esc(k.code)}</small></td><td>${esc(k.role)}</td><td>${esc(k.mapping_source)}</td><td>${pct(k.confidence, 0)}</td><td>${status(k.verification_status)}</td></tr>`).join('') || '<tr><td colspan="5">尚未完成深层映射</td></tr>'}</tbody></table></div>
    ${enrichments}
    <h3>回源</h3><div class="mono">${esc(data.source_path)}${data.source_page ? `#page=${esc(data.source_page)}` : ''}</div>`;
  enhanceReadOnlyTables(dialog);
  dialog.showModal();
}

async function renderMastery(context) {
  const data = await api('/api/mastery', context.signal);
  ensureCurrent(context);
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

async function renderAssessments(context) {
  const [records, weights, performance] = await Promise.all([api('/api/assessments', context.signal), api('/api/weights', context.signal), api('/api/performance/sessions?limit=100', context.signal)]);
  ensureCurrent(context);
  const sessions = performance.items || [];
  const totalScore = sessions.reduce((sum,row) => sum + Number(row.derived_score || 0), 0);
  const totalMaximum = sessions.reduce((sum,row) => sum + Number(row.derived_max_score || 0), 0);
  const totalAttempts = sessions.reduce((sum,row) => sum + Number(row.attempt_count || 0), 0);
  const anchors = sessions.filter(row => row.is_calibration_anchor).length;
  const recentSessions = sessions.slice(0, 3);
  view.innerHTML = `
    <section class="relief-hero compact-hero">
      <div class="relief-copy"><p class="eyebrow">REAL EVIDENCE</p><h2>每次作答都是成绩，线下测试负责校准</h2><p>课堂、阅读、语法、作业和听写都会自动累计；线下闭卷权重更高，但不会覆盖日常证据。</p></div>
      <div class="next-action"><span>你只需</span><strong>把本次结果发给对应对话</strong><p>课件对话或听写对话会保存逐题结果；有阅读原始答案时，再自动生成可核验错因。</p></div>
    </section>
    <section class="quick-facts" aria-label="成绩摘要">
      ${metric('已记录作答', num(totalAttempts), `${num(sessions.length)} 次学习活动`, 'good')}
      ${metric('当前正确率', pct(totalMaximum ? totalScore / totalMaximum : null, 1), `${num(totalScore)} / ${num(totalMaximum)}`)}
      ${metric('线下校准', anchors ? `${num(anchors)} 次` : '待首次', anchors ? '已纳入高权重证据' : '下次线下测后交给课件对话', anchors ? 'good' : 'attention')}
    </section>
    ${panel('最近三次记录', '快速确认数据是否已经进库；完整记录在专业数据中', `<div class="record-list">${recentSessions.map(row => `<article><div><strong>${esc(row.title)}</strong><small>${esc(String(row.started_at).slice(0,10))} · ${(row.domains || []).map(d => esc(d.domain)).join(' / ')}</small></div><div><strong>${num(row.derived_score)} / ${num(row.derived_max_score)}</strong><small>${pct(row.accuracy,1)}</small></div></article>`).join('') || '<div class="empty-state">尚无作答记录</div>'}</div>`)}
    <div class="detail-only">
      <div class="detail-divider"><span>专业数据</span></div>
      <div class="dashboard-grid">
      <div class="stack">
        ${panel('真实课堂成绩', '从逐题作答自动汇总；即使未单独录入一张试卷总分，成绩也不会丢失', `<div class="table-wrap"><table><thead><tr><th>日期</th><th>课次</th><th>范围</th><th>作答</th><th>对/错</th><th>正确率</th><th>证据</th></tr></thead><tbody>${sessions.map(row => `<tr><td>${esc(String(row.started_at).slice(0,10))}</td><td><strong>${esc(row.title)}</strong><br><small class="mono">${esc(row.session_id)}</small></td><td>${(row.domains || []).map(d => `${esc(d.domain)} ${num(d.attempt_count)}`).join('<br>')}</td><td>${num(row.derived_score)} / ${num(row.derived_max_score)}</td><td>${num(row.correct_count)} / ${num(row.wrong_count + row.partial_count)}</td><td>${pct(row.accuracy,1)}</td><td>${row.is_calibration_anchor ? '<span class="status-tag verified">校准锚点</span>' : '<span class="status-tag">日常真实成绩</span>'}</td></tr>`).join('') || '<tr><td colspan="7">尚无作答记录</td></tr>'}</tbody></table></div>`)}
        ${panel('阅读整篇诊断', '输入 passage_id，查看本篇考什么、错几题、为什么错和同类题', `<form id="reading-performance-search" class="toolbar"><div class="field grow"><label for="reading-passage-id">文章 passage_id</label><input id="reading-passage-id" name="passage_id" required placeholder="PAS-..."></div><div class="field grow"><label for="reading-session-id">限定课次（可选）</label><input id="reading-session-id" name="session_id" placeholder="SES-..."></div><button class="button" type="submit">生成诊断</button></form><div id="reading-performance-results"><p class="subtle">题目知识点表示“考什么”；作答错因表示“学生为什么错”，两者不混用。</p></div>`)}
        ${panel('校准锚点记录', '不同满分、类型和系列不会连成一条原始分曲线', `<div class="table-wrap"><table><thead><tr><th>日期</th><th>测试</th><th>类型</th><th>环境</th><th>成绩</th><th>权重</th></tr></thead><tbody>${records.items.map(row => `<tr><td>${esc(String(row.started_at).slice(0,10))}</td><td>${esc(row.title)}</td><td>${esc(row.assessment_kind)}</td><td>${esc(row.delivery_mode)}</td><td>${row.raw_score == null ? '—' : `${num(row.raw_score)} / ${num(row.max_score)}`}</td><td>${num(row.evidence_weight)}</td></tr>`).join('') || '<tr><td colspan="6">尚无单独分类的高权重校准记录；上方课堂成绩仍然有效。</td></tr>'}</tbody></table></div>`)}
      </div>
      <div class="stack">
        ${panel('证据权重', weights.policy_version, barList(weights.assessment_policies.filter(row => row.delivery_mode === 'offline_closed'), {label:'assessment_kind', value:'evidence_weight', max: 1.6, format: v => v.toFixed(2)}))}
        ${panel('题目修正项', '测试权重还会乘以题目与证据质量', `<div class="definition-list">${weights.question_rules.map(rule => `<div><dt>${esc(rule.dimension)} · ${esc(rule.match_value)}</dt><dd><strong>× ${Number(rule.multiplier).toFixed(2)}</strong>　${esc(rule.rationale)}</dd></div>`).join('')}</div>`)}
      </div>
      </div>
    </div>`;
}

async function loadReadingPerformance() {
  const form = document.querySelector('#reading-performance-search');
  const host = document.querySelector('#reading-performance-results');
  if (!form || !host) return;
  const controller = beginAuxiliaryRequest('reading-performance');
  const data = new FormData(form);
  const passageId = String(data.get('passage_id') || '').trim();
  const sessionId = String(data.get('session_id') || '').trim();
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
  host.setAttribute('aria-busy', 'true');
  host.innerHTML = '<div class="empty-state">正在读取逐题证据…</div>';
  try {
    const result = await api(`/api/reading/passages/${encodeURIComponent(passageId)}/performance${query}`, controller.signal);
    if (controller.signal.aborted || !host.isConnected) return;
    state.readingPerformance = {passageId, sessionId};
    const s = result.summary;
    const questionRows = result.questions.map(row => {
      const points = (row.knowledge_points || []).map(k => k.name_cn || k.code).join('、') || row.primary_test_point || '待标注';
      const attempts = (row.attempts || []).map(a => {
        const causes = (a.error_causes || []).map(c => `${esc(c.label_cn)} ${status(c.verification_status)}`).join('<br>');
        const diagnosis = a.diagnostic_status === 'blocked_not_captured' ? '原始答案未保存，禁止反推错因' : (a.diagnostic_status === 'pending_diagnosis' ? '待诊断' : causes || '—');
        return `<div><strong>${esc(a.result)}</strong> · ${esc(a.student_answer ?? (a.answer_capture_status === 'not_captured' ? '未保存' : '空白'))}<br><small>${diagnosis}</small></div>`;
      }).join('') || '未作答';
      return `<tr><td>${esc(row.original_number || row.question_id)}<br><small class="mono">${esc(row.question_id)}</small></td><td>${esc(points)}</td><td>${attempts}</td><td>${esc(row.answer || '—')}</td></tr>`;
    }).join('');
    host.innerHTML = `<section class="metric-strip compact"><div class="metric"><span class="metric-label">题数</span><strong class="metric-value">${num(s.question_count)}</strong></div><div class="metric"><span class="metric-label">已作答</span><strong class="metric-value">${num(s.attempted_question_count)}</strong></div><div class="metric"><span class="metric-label">正确 / 错误</span><strong class="metric-value">${num(s.correct_count)} / ${num(s.wrong_count + s.partial_count)}</strong></div><div class="metric"><span class="metric-label">正确率</span><strong class="metric-value">${pct(s.accuracy,1)}</strong></div><div class="metric"><span class="metric-label">待诊断</span><strong class="metric-value">${num(s.pending_diagnosis_count)}</strong></div></section><div class="table-wrap" style="margin-top:14px"><table><thead><tr><th>题目</th><th>考查内容</th><th>作答与错因</th><th>标准答案</th></tr></thead><tbody>${questionRows}</tbody></table></div>${s.pending_diagnosis_count ? '<div class="callout amber readonly-note"><h3>有待确认错因</h3><p>把本篇作答证据交给 Codex；确认结果写入数据库后，看板会自动更新。</p></div>' : ''}<div class="selection-summary"><h3>同考点练习</h3>${(result.similar_questions || []).map(q => `<article class="selection-card"><span>→</span><div><strong>${esc(q.primary_test_point || q.question_type)}</strong><small class="mono">${esc(q.question_id)} · ${esc(q.passage_id)}</small><p>${esc(q.stem || '请打开题目详情')}</p></div></article>`).join('') || '<p class="subtle">当前没有可推荐的同考点已核验题。</p>'}</div>`;
    enhanceReadOnlyTables(host);
    host.setAttribute('aria-busy', 'false');
    announce(`阅读诊断完成，共 ${num(s.question_count)} 道题`);
  } catch (error) {
    if (error.name !== 'AbortError' && host.isConnected) {
      host.setAttribute('aria-busy', 'false');
      host.innerHTML = `<div class="error-state" role="alert">${esc(error.message)}</div>`;
      announce('阅读诊断失败');
    }
  }
}

async function renderLibrary(context) {
  const [data, resources, candidates] = await Promise.all([
    api('/api/library', context.signal),
    api('/api/library/resources?limit=80', context.signal),
    api('/api/library/candidates?limit=40', context.signal),
  ]);
  ensureCurrent(context);
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
  if (!form || !host) return;
  const controller = beginAuxiliaryRequest('library-search');
  const query = new FormData(form).get('q');
  host.innerHTML = '<p class="subtle">正在检索已分块材料……</p>';
  try {
    const data = await api(`/api/library/search?q=${encodeURIComponent(query)}&limit=30`, controller.signal);
    if (controller.signal.aborted || !host.isConnected) return;
    host.innerHTML = `<p class="subtle">找到 ${num(data.count)} 个材料片段。</p><div class="search-hit-list">${data.items.map(row => `<article class="search-hit"><div><strong>${esc(row.heading || row.source_title || row.file_name)}</strong>${status(row.verification_status)}</div><p>${esc(row.snippet)}</p><small class="mono">${esc(row.source_locator)}</small></article>`).join('') || '<div class="empty-state">未命中已分块材料</div>'}</div>`;
  } catch (error) {
    if (error.name !== 'AbortError' && host.isConnected) host.innerHTML = `<div class="error-state" role="alert">${esc(error.message)}</div>`;
  }
}

async function renderDictation(context) {
  const data = await api('/api/dictation/plan?limit=20', context.signal);
  ensureCurrent(context);
  view.innerHTML = `
    <section class="relief-hero compact-hero">
      <div class="relief-copy"><p class="eyebrow">DICTATION AUTOMATION</p><h2>听写固定工作流已接通</h2><p>单词听写对话会自动取到期词、保留 OCR 原始答案、本地精确批改、写入成绩并安排复测。</p></div>
      <div class="next-action"><span>你只需</span><strong>把听写结果发给单词听写对话</strong><p>网站只展示数据库状态，不提供重复录入入口。</p></div>
    </section>
    <section class="quick-facts" aria-label="听写自动化状态">
      ${metric('本次待复测', num(data.plan_size), '对话会自动读取', data.plan_size ? 'attention' : 'good')}
      ${metric('批改', '本地规则', '重复使用不消耗模型 token', 'good')}
      ${metric('结果去向', '统一数据库', '成绩与错题同步更新', 'good')}
    </section>
    <section class="automation-steps" aria-label="听写处理步骤">
      <article><span>01</span><div><strong>Agent 取清单</strong><p>按到期日、优先级和连续错误自动取词。</p></div></article>
      <article><span>02</span><div><strong>OCR 保留原文</strong><p>识别结果不先改正，确保错因可追溯。</p></div></article>
      <article><span>03</span><div><strong>确定性批改</strong><p>本地精确匹配并记录 correct / wrong / partial。</p></div></article>
      <article><span>04</span><div><strong>自动排复测</strong><p>错误进入复习队列，后续对话直接调用。</p></div></article>
    </section>
    <section class="panel readonly-list-panel">
      <div class="panel-head"><div><h2>当前待复测清单</h2><p>按到期日、优先级和连续错误从数据库读取</p></div><button class="button button-secondary button-small" data-action="dictation-refresh">刷新清单</button></div>
      <div class="panel-body">
        ${data.items.length ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>提示</th><th>连续错误</th><th>状态</th></tr></thead><tbody>${data.items.map((row,index) => `<tr><td>${index+1}</td><td>${esc(row.prompt_snapshot || row.item_id)}<br><small class="mono subtle">${esc(row.item_id)}</small></td><td>${num(row.consecutive_errors)}</td><td>${status(row.status || 'due')}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty-state"><h2>当前没有到期词汇</h2><p>复测队列清空，或尚未导入词汇任务。</p></div>'}
      </div>
    </section>`;
}

async function renderWorkflow(context) {
  const [data, agents] = await Promise.all([api('/api/workflow', context.signal), api('/api/agent/dashboard', context.signal)]);
  ensureCurrent(context);
  const notice = data.system_notice || {};
  const capabilityCards = agents.capabilities.map(item => `<article class="capability-card"><div><p class="eyebrow">${esc(item.mode)}</p><h3>${esc(item.name_cn)}</h3></div><span class="skill-chip">$${esc(item.skill)}</span><p>${esc(item.responsibility)}</p></article>`).join('');
  const recentRuns = agents.recent_runs.length
    ? `<div class="run-list">${agents.recent_runs.map(run => `<article><span class="run-marker ${esc(run.status)}"></span><div><div class="run-title"><strong>${esc(run.title)}</strong>${status(run.status)}</div><p>${esc(run.summary || run.primary_capability)}</p><small>${esc(run.source_thread)} · ${esc(run.updated_at)}</small></div></article>`).join('')}</div>`
    : '<div class="empty-state"><h2>还没有运行记录</h2><p>下次中枢分发任务时，这里会自动出现，不需要手动登记。</p></div>';
  const recentGenerations = agents.recent_generations.length
    ? `<div class="run-list">${agents.recent_generations.map(run => `<article><span class="run-marker ${esc(run.status)}"></span><div><div class="run-title"><strong>${esc(run.title)}</strong>${status(run.status)}</div><p>${esc(run.artifact_type)}${run.stale_reason ? ` · 已过期：${esc(run.stale_reason)}` : ''}</p><small>${esc(run.skill_name || 'Codex')} · ${esc(run.updated_at)}</small></div></article>`).join('')}</div>`
    : '<div class="empty-state"><h2>还没有生成记录</h2><p>Codex 生成课件、测评或报告后，这里会显示来源快照与结果状态。</p></div>';
  view.innerHTML = `
    <section class="relief-hero compact-hero"><div class="relief-copy"><p class="eyebrow">AGENT ROUTER</p><h2>一次判断，只调用必要能力</h2><p><span>${esc(notice.message || '学习数据已经写入统一数据库。')}</span> <span>中枢不承担所有业务，只负责路由、状态和最终汇总。</span></p></div><div class="next-action"><span>${esc(agents.router.status)}</span><strong>${num(agents.summary.active)} 个任务正在处理</strong><p>${num(agents.summary.needs_input)} 个任务需要你的确认；其余状态由 Agent 自动回写。</p></div></section>
    <section class="orchestrator-map" aria-label="Agent 能力架构"><article class="router-node"><p class="eyebrow">CENTRAL ROUTER</p><h2>${esc(agents.router.skill)}</h2><p>${esc(agents.router.rule)}</p><code>Codex / CLI → audited API</code></article><div class="route-line" aria-hidden="true"></div><div class="capability-grid">${capabilityCards}</div></section>
    ${panel('最近运行', '中枢与专用技能自动更新；这里只是看板，不是另一份录入表', recentRuns)}
    <div style="margin-top:18px">${panel('最近生成', `${num(agents.summary.generation_active)} 个处理中 · ${num(agents.summary.generation_stale)} 个因新证据待刷新`, recentGenerations)}</div>
    <div class="detail-only"><div class="detail-divider"><span>对话入口</span></div><div class="channel-grid">${data.channels.map(channel => `<article class="channel"><div class="automation-status">${status(channel.status)}</div><p class="eyebrow">${esc(channel.channel_key)}</p><h2>${esc(channel.display_name)}</h2><p>${esc(channel.responsibility)}</p><p class="agent-rule">需要时由中枢路由；明确任务也可直接调用对应专用技能。</p><dl class="definition-list"><div><dt>读取</dt><dd>${esc(channel.reads_from)}</dd></div><div><dt>写入</dt><dd>${esc(channel.writes_through)}</dd></div><div><dt>实时上下文</dt><dd class="mono">http://127.0.0.1:8788${esc(channel.context_endpoint)}</dd></div></dl><button class="button button-secondary button-small" data-copy="http://127.0.0.1:8788${esc(channel.context_endpoint)}">复制接口</button></article>`).join('')}</div><div class="detail-divider"><span>工程审计</span></div><section class="panel"><div class="panel-head"><div><h2>统一工作清单</h2><p>所有对话读取同一状态，不再重复手工写交接</p></div></div><div class="panel-body"><div class="table-wrap"><table><thead><tr><th>领域</th><th>任务</th><th>负责人</th><th>完成量</th><th>状态</th><th>证据</th></tr></thead><tbody>${data.work_items.map(item => `<tr><td>${esc(item.area)}</td><td>${esc(item.title)}</td><td>${esc(item.owner_channel)}</td><td>${num(item.completed_units)} / ${num(item.total_units)} ${esc(item.unit_label)}</td><td>${status(item.status)}</td><td class="mono">${esc(item.evidence_path || '—')}</td></tr>`).join('')}</tbody></table></div></div></section><div class="callout" style="margin-top:18px"><h3>工程约束</h3><p>学习事实只能通过审计接口写入；运行台账只记录 Agent 状态，不能替代成绩、作答或错因证据。</p></div></div>`;
}

async function render(force = false) {
  const context = beginRenderRequest();
  loading();
  const [title, label] = titles[state.current]; pageTitle.textContent = title; eyebrow.textContent = label;
  document.querySelectorAll('.nav-item[data-view]').forEach(button => {
    const active = button.dataset.view === state.current;
    button.classList.toggle('is-active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  const systemViewActive = ['question-bank', 'library', 'workflow'].includes(state.current);
  moreButton.classList.toggle('has-active', systemViewActive);
  if (systemViewActive) moreButton.setAttribute('aria-current', 'page');
  else moreButton.removeAttribute('aria-current');
  closeTransientMenus();
  if (!state.studentId) {
    renderEmptyStudentState();
    return;
  }
  try {
    if (state.subjectCode !== 'english') {
      await renderSubjectWorkspace(context);
    } else {
      if (state.current === 'overview') await renderOverview(force, context);
      if (state.current === 'question-bank') await renderQuestionBank(context);
      if (state.current === 'mastery') await renderMastery(context);
      if (state.current === 'assessments') await renderAssessments(context);
      if (state.current === 'library') await renderLibrary(context);
      if (state.current === 'dictation') await renderDictation(context);
      if (state.current === 'workflow') await renderWorkflow(context);
    }
    ensureCurrent(context);
    if (state.current !== 'overview') freshness.textContent = `数据刷新于 ${new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'})}`;
    view.setAttribute('aria-busy', 'false');
    enhanceReadOnlyTables();
    applyLocale();
    announce(`${pageTitle.textContent}已更新`);
  } catch (error) {
    if (error.name !== 'AbortError' && context.generation === state.renderGeneration) errorState(error);
  }
}

document.addEventListener('click', async event => {
  if (event.target.closest('#more-nav-button')) {
    const open = !mobileMoreMenu.classList.contains('is-open');
    closeTransientMenus();
    mobileMoreMenu.classList.toggle('is-open', open);
    moreButton.setAttribute('aria-expanded', String(open));
    if (open) mobileMoreMenu.querySelector('.nav-item[data-view]')?.focus();
    return;
  }
  if (event.target.closest('#settings-button')) {
    const open = !settingsMenu.classList.contains('is-open');
    closeTransientMenus();
    settingsMenu.classList.toggle('is-open', open);
    settingsButton.setAttribute('aria-expanded', String(open));
    if (open) settingsMenu.querySelector('select, button')?.focus();
    return;
  }
  const nav = event.target.closest('[data-view], [data-nav]');
  if (nav) {
    const navigatedFromMobileMore = isMobileNavigation() && mobileMoreMenu.contains(nav);
    state.current = nav.dataset.view || nav.dataset.nav;
    closeTransientMenus();
    await render();
    if (navigatedFromMobileMore) moreButton.focus();
    return;
  }
  if (event.target.closest('[data-action="refresh"]')) { state.cache = {}; await render(true); return; }
  if (event.target.closest('[data-action="boot-retry"]')) { await boot(); return; }
  if (event.target.closest('[data-action="dictation-refresh"]')) { state.cache = {}; await render(true); return; }
  const promptButton = event.target.closest('[data-copy-prompt]');
  if (promptButton) {
    try {
      await navigator.clipboard.writeText(promptButton.dataset.copyPrompt);
      notify('已复制给 Codex 的需求');
    } catch {
      notify('复制失败，请手动选择这段需求', true);
    }
    return;
  }
  const question = event.target.closest('[data-question]');
  if (question) {
    state.dialogTrigger = question;
    try { await openQuestion(question.dataset.question); } catch (error) { notify(error.message, true); }
    return;
  }
  const copy = event.target.closest('[data-copy]');
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      notify('接口地址已复制');
    } catch {
      notify('复制失败，请手动选择接口地址', true);
    }
  }
  if (!event.target.closest('#settings-menu, #mobile-more-menu')) closeTransientMenus();
});
document.querySelector('#refresh-button').addEventListener('click', async () => { state.cache = {}; await render(true); });
detailButton.addEventListener('click', async () => {
  state.detailMode = !state.detailMode;
  window.localStorage.setItem('open-tutor-ledger-detail-mode', String(state.detailMode));
  applyDetailMode();
  if (state.current === 'overview') await render();
  closeTransientMenus();
  notify(state.detailMode ? '已展开专业数据' : '已返回减负模式');
});
studentSelect.addEventListener('change', async () => {
  state.studentId = studentSelect.value;
  persistStudentSelection();
  state.cache = {};
  populateWorkspaceControls();
  await render(true);
});
subjectSelect.addEventListener('change', async () => {
  state.subjectCode = subjectSelect.value;
  window.localStorage.setItem('open-tutor-subject', state.subjectCode);
  state.cache = {};
  await render(true);
});
localeSelect.addEventListener('change', async () => {
  const restoreSettingsFocus = isMobileNavigation() && settingsMenu.classList.contains('is-open');
  state.locale = localeSelect.value;
  window.localStorage.setItem('open-tutor-locale', state.locale);
  closeTransientMenus();
  populateWorkspaceControls();
  await render();
  if (restoreSettingsFocus) settingsButton.focus();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && dialog.open) {
    event.preventDefault();
    dialog.close();
    return;
  }
  if (event.key === 'Escape' && (mobileMoreMenu.classList.contains('is-open') || settingsMenu.classList.contains('is-open'))) {
    event.preventDefault();
    closeTransientMenus({restoreFocus: true});
  }
});
dialog.addEventListener('close', () => {
  if (state.dialogTrigger?.isConnected) state.dialogTrigger.focus();
  state.dialogTrigger = null;
});
document.addEventListener('submit', async event => {
  if (event.target.id === 'question-search') { event.preventDefault(); await searchQuestionRows(); }
  if (event.target.id === 'library-search') { event.preventDefault(); await searchLibraryMaterials(); }
  if (event.target.id === 'reading-performance-search') { event.preventDefault(); await loadReadingPerformance(); }
});

function renderBootError(error) {
  pageTitle.textContent = '教学看板暂不可用';
  eyebrow.textContent = '连接状态';
  view.setAttribute('aria-busy', 'false');
  view.innerHTML = `<section class="panel error-state" role="alert"><div class="panel-body"><h2>初始化失败</h2><p>${esc(error.message)}</p><button class="button button-secondary" type="button" data-action="boot-retry">重新连接</button></div></section>`;
  document.querySelector('#health-label').textContent = '连接失败';
  document.querySelector('#health-dot').className = 'health-dot error';
  announce(`初始化失败：${error.message}`);
}

async function boot() {
  state.bootController?.abort();
  state.bootController = new AbortController();
  const signal = state.bootController.signal;
  applyDetailMode();
  localeSelect.value = state.locale;
  loading();
  try {
    const [health, config] = await Promise.all([api('/api/health', signal), api('/api/app-config', signal)]);
    state.config = config;
    const supportedSubjects = new Set(config.subjects.map(item => item.subject_code));
    if (!supportedSubjects.has(state.subjectCode)) state.subjectCode = 'english';
    await refreshStudents('', signal);
    document.querySelector('#health-label').textContent = health.status === 'ok' ? '数据已连接' : '需要关注';
    document.querySelector('#health-dot').className = `health-dot ${health.status === 'ok' ? 'ok' : 'error'}`;
    if (!state.students.length) {
      renderEmptyStudentState();
      return;
    }
    await render();
  } catch (error) {
    if (error.name !== 'AbortError') renderBootError(error);
  }
}
boot();
