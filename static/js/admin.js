'use strict';
/* Admin pages: user list, one user's conversations (hidden included) and the dashboard.
   Loaded before app.js like assistant.js; uses app.js helpers (api, el, fill, taipeiDate) at call time.
   Charts are plain DOM: every bar has a title tooltip, the numbers are printed, and a table repeats them. */
window.KP_PAGES = window.KP_PAGES || {};

const ROLE_NAMES = { user: '員工', admin: '管理員' };
const HIDDEN_REASONS = { user: '使用者隱藏', department_change: '切換部門隱藏' };
const KIND_NAMES = { clarify: '追問', answer: '回答', out_of_scope: '超出範圍', error: '錯誤' };
const STATUS_NAMES = { answered: '完整回答', partial: '部分回答', not_found: '找不到', unknown: '未知' };
const dateTimeFormat = new Intl.DateTimeFormat('zh-TW', { timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit',
  day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });

function taipeiDateTime(value) { return value ? dateTimeFormat.format(new Date(value)) : '—'; }
function usd(value) { return value === null || value === undefined ? '—' : '$' + Number(value).toFixed(4); }
function percent(value) { return value === null || value === undefined ? '—' : value + '%'; }

function link(href, text) {
  const anchor = el('a', 'admin-link', text);
  anchor.href = href;
  return anchor;
}

/* A table that turns into labelled cards on narrow screens (td[data-label]), so the page never scrolls sideways.
   keepTable: stay a table and scroll inside its own box instead (long, all-numeric tables such as the daily detail). */
function dataTable(columns, rows, emptyText, { keepTable = false } = {}) {
  if (!rows.length) return el('p', 'muted', emptyText);
  const table = el('table', keepTable ? 'admin-table keep-table' : 'admin-table');
  const head = table.createTHead().insertRow();
  for (const column of columns) head.append(el('th', column.numeric ? 'numeric' : '', column.label));
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const column of columns) {
      const cell = el('td', column.numeric ? 'numeric' : '');
      cell.dataset.label = column.label;
      const value = column.value(row);
      cell.append(value instanceof Node ? value : String(value ?? '—'));
      tr.append(cell);
    }
  }
  const wrap = el('div', 'admin-table-wrap');
  wrap.append(table);
  return wrap;
}

KP_PAGES.admin_users = async function adminUsersPage() {
  const { data } = await api('/api/admin/users');
  fill(document.getElementById('main-content'), { total: data.length });
  document.getElementById('user-table').append(dataTable([
    { label: '名稱', value: (u) => link('/admin/users/' + u.id, u.name) },
    { label: 'Email', value: (u) => u.email },
    { label: '部門', value: (u) => u.department.name },
    { label: '角色', value: (u) => ROLE_NAMES[u.role] || u.role },
    { label: '註冊時間', value: (u) => taipeiDate(u.created_at) },
    { label: '對話數', value: (u) => u.conversations, numeric: true },
    { label: '今日用量', value: (u) => percent(u.today_percent), numeric: true },
    { label: '最後活動', value: (u) => u.last_active_at ? taipeiDateTime(u.last_active_at) : '尚未提問' },
  ], data, '還沒有使用者。'));
};

/* ---- one user's conversations ---------------------------------------------------------- */

function traceSummary(trace) {
  const details = el('details', 'trace');
  details.append(el('summary', '', '路由與回答紀錄'));
  const list = el('dl');
  const add = (term, value) => { list.append(el('dt', '', term), el('dd', '', value)); };
  const call = (llm) => llm ? `${llm.model}・input ${llm.input_tokens.toLocaleString()}／output ${llm.output_tokens.toLocaleString()} token・`
    + `${(llm.latency_ms / 1000).toFixed(1)} s・${usd(llm.budget_cost_usd)}${llm.error ? '・錯誤 ' + llm.error : ''}` : '未呼叫';
  add('路由動作', trace.route_action + (trace.fallback ? `（保底：${trace.fallback}）` : '') + (trace.route_error ? `・${trace.route_error}` : ''));
  add('選用文件', trace.selected_docs.join('、') || '—');
  add('送入段落', trace.chunk_ids.join('、') || '—');
  if (trace.dropped_docs.length) add('丟棄的不可見文件', trace.dropped_docs.join('、'));
  add('回答狀態', trace.answer_status ? (STATUS_NAMES[trace.answer_status] || trace.answer_status) + (trace.answer_error ? `・${trace.answer_error}` : '') : '—');
  if (trace.removed_citations.length || trace.dropped_points) {
    add('移除的引用', `${trace.removed_citations.join('、') || '—'}（刪除要點 ${trace.dropped_points} 個）`);
  }
  add('路由呼叫', call(trace.router));
  add('回答呼叫', call(trace.answer));
  details.append(list);
  return details;
}

/* Every document an answer cited, once each; the links open the admin document page in a new tab. */
function citedSources(payload) {
  const citations = [...(payload.safety_note ? payload.safety_note.citations : []), ...payload.points.flatMap((p) => p.citations)];
  const unique = [...new Map(citations.map((c) => [c.chunk_id, c])).values()];
  if (!unique.length) return null;
  const box = el('p', 'admin-sources');
  box.append(el('span', 'admin-sources-label', '引用來源'));
  for (const citation of unique) {
    const label = `${citation.chunk_id}｜${citation.section_path.split(' › ').pop()}`;
    const item = el(citation.url ? 'a' : 'span', 'citation', label);
    item.title = citation.doc_title;
    if (citation.url) { item.href = citation.url; item.target = '_blank'; item.rel = 'noopener'; }
    box.append(item);
  }
  return box;
}

function adminMessage(message) {
  const box = el('article', `admin-message admin-message-${message.role}`);
  const meta = el('p', 'admin-message-meta');
  meta.append(el('strong', '', message.role === 'user' ? '使用者' : '助理'));
  if (message.kind) meta.append(el('span', 'tag', KIND_NAMES[message.kind] || message.kind));
  meta.append(el('span', '', taipeiDateTime(message.created_at)));
  box.append(meta, el('p', 'admin-message-body', message.content));
  const sources = message.kind === 'answer' && message.payload ? citedSources(message.payload) : null;
  if (sources) box.append(sources);
  if (message.kind === 'answer') {
    const feedback = message.feedback;
    const text = feedback ? `回饋：${feedback.rating === 'up' ? '有幫助' : '沒幫助'}${feedback.comment ? `「${feedback.comment}」` : ''}` : '回饋：未評分';
    box.append(el('p', `admin-feedback${feedback ? ' is-' + feedback.rating : ''}`, text));
  }
  if (message.trace) box.append(traceSummary(message.trace));
  return box;
}

KP_PAGES.admin_user = async function adminUserPage() {
  const heading = document.querySelector('[data-id]');
  const list = document.getElementById('admin-conversation-list');
  const thread = document.getElementById('admin-thread');
  const result = await api(`/api/admin/users/${heading.dataset.id}/conversations`);
  document.title = result.user.name + ' · 使用者對話 · KnowPilot';
  fill(heading, { name: result.user.name, email: result.user.email, department: '目前部門：' + result.user.department });
  document.querySelector('[data-conversation-empty]').hidden = result.data.length > 0;

  async function open(id) {
    list.querySelectorAll('button').forEach((button) => button.setAttribute('aria-current', String(Number(button.dataset.id) === id)));
    history.replaceState(null, '', `${location.pathname}?c=${id}`);
    try {
      const { conversation, messages } = await api(`/api/admin/conversations/${id}/messages`);
      const header = el('header', 'admin-thread-header');
      header.append(el('h2', '', conversation.title));
      const facts = el('p', 'muted', `${conversation.department.name}・建立於 ${taipeiDateTime(conversation.created_at)}`);
      if (conversation.hidden_reason) {
        facts.append(' ', el('span', 'hidden-badge', `${HIDDEN_REASONS[conversation.hidden_reason]}（${taipeiDateTime(conversation.hidden_at)}）`));
      }
      header.append(facts);
      thread.replaceChildren(header, ...messages.map(adminMessage));
    } catch (error) {
      thread.replaceChildren(el('p', 'alert', error.message));
    }
  }

  list.replaceChildren(...result.data.map((conversation) => {
    const item = el('li');
    const button = el('button');
    button.type = 'button';
    button.dataset.id = conversation.id;
    const meta = el('span', 'conversation-meta', `${conversation.department.name}・${conversation.message_count} 則・${taipeiDateTime(conversation.updated_at)}`);
    button.append(el('span', 'conversation-title', conversation.title), meta);
    if (conversation.hidden_reason) button.append(el('span', 'hidden-badge', HIDDEN_REASONS[conversation.hidden_reason]));
    button.addEventListener('click', () => open(conversation.id));
    item.append(button);
    return item;
  }));
  const requested = Number(new URLSearchParams(location.search).get('c'));
  if (requested) await open(requested);
};

/* ---- any department's document (admin view) ------------------------------------------- */

KP_PAGES.admin_document = async function adminDocumentPage() {
  const article = document.querySelector('[data-id]');
  const { data: doc } = await api('/api/admin/documents/' + article.dataset.id);
  document.title = doc.title + '（後台檢視） · KnowPilot';
  fill(article, { ...documentFields(doc), version: '版本 ' + doc.version });
  fillMarkdown(article, { content: doc.content });
  jumpToSection(article, new URLSearchParams(location.search).get('section'));
};

/* ---- dashboard ------------------------------------------------------------------------- */

function statTile(label, value, note) {
  const tile = el('div', 'stat-tile');
  tile.append(el('span', 'stat-label', label), el('strong', 'stat-value', value));
  if (note) tile.append(el('span', 'stat-note', note));
  return tile;
}

/* Stacked columns per day. Series stack from the baseline in the given order; the peak and the last day are labelled. */
function columnChart(days, series, format) {
  const totals = days.map((day) => series.reduce((sum, s) => sum + day[s.key], 0));
  const max = Math.max(...totals, 0);
  const peak = totals.indexOf(max);
  const every = Math.ceil(days.length / 7);
  const chart = el('div', 'columns');
  chart.setAttribute('role', 'img');
  chart.setAttribute('aria-label', `最高 ${format(max)}，詳細數字見下方每日明細`);
  days.forEach((day, i) => {
    const column = el('div', 'column');
    column.title = `${day.date}：` + series.map((s) => `${s.label} ${format(day[s.key])}`).join('、')
      + (series.length > 1 ? `，合計 ${format(totals[i])}` : '');
    const value = el('span', 'column-value', max && (i === peak || i === days.length - 1) ? format(totals[i]) : '');
    const stack = el('div', 'column-stack');
    stack.style.height = max ? `${(totals[i] / max) * 100}%` : '0';
    for (const s of series) {
      if (day[s.key] > 0) {
        const segment = el('span', 'segment ' + s.className);
        // Shares of the day's total: flex-grow values summing below 1 would leave the column partly empty.
        segment.style.flexGrow = day[s.key] / totals[i];
        stack.append(segment);
      }
    }
    // Every n-th date plus the last one; a regular label too close to the last one is dropped so they never overlap.
    const last = i === days.length - 1;
    const shown = last || (i % every === 0 && days.length - 1 - i >= every);
    const label = el('span', 'column-label', shown ? day.date.slice(5).replace('-', '/') : '');
    column.append(value, stack, label);
    chart.append(column);
  });
  return chart;
}

/* Horizontal stacked bars for helpful / not helpful, with the counts printed beside each bar. */
function feedbackBars(items, labelOf) {
  if (!items.length) return el('p', 'muted', '這段期間沒有回饋。');
  const max = Math.max(...items.map((item) => item.up + item.down));
  const list = el('div', 'bars');
  for (const item of items) {
    const row = el('div', 'bar-row');
    row.title = `${labelOf(item)}：有幫助 ${item.up}、沒幫助 ${item.down}`;
    const track = el('div', 'bar-track');
    const bar = el('div', 'bar');
    bar.style.width = `${((item.up + item.down) / max) * 100}%`;
    for (const [key, className] of [['up', 'segment-up'], ['down', 'segment-down']]) {
      if (item[key] > 0) {
        const segment = el('span', 'segment ' + className);
        segment.style.flexGrow = item[key] / (item.up + item.down);
        bar.append(segment);
      }
    }
    track.append(bar);
    row.append(el('span', 'bar-label', labelOf(item)), track, el('span', 'bar-value', `有幫助 ${item.up}・沒幫助 ${item.down}`));
    list.append(row);
  }
  return list;
}

KP_PAGES.admin_dashboard = async function adminDashboardPage() {
  const main = document.getElementById('main-content');
  const buttons = [...main.querySelectorAll('[data-days]')];

  async function load(days) {
    buttons.forEach((button) => button.setAttribute('aria-pressed', String(Number(button.dataset.days) === days)));
    const { data } = await api('/api/admin/stats?days=' + days);
    const { usage, feedback } = data;
    fill(main, { range: `${data.from.replaceAll('-', '.')} – ${data.to.replaceAll('-', '.')}（台北時間）` });
    const rated = feedback.up + feedback.down;
    const newUsers = data.daily.reduce((sum, day) => sum + day.new_users, 0);
    document.getElementById('stat-grid').replaceChildren(
      statTile('提問數', usage.questions.toLocaleString(), `新註冊 ${newUsers} 人`),
      statTile('LLM 花費', usd(usage.cost), `平均每題 ${usd(usage.avg_cost_per_question)}`),
      statTile('今日全站預算', percent(usage.today_percent), `${usd(usage.today_spent)} ／ $${usage.today_limit}`),
      statTile('有幫助比例', feedback.up_ratio === null ? '—' : Math.round(feedback.up_ratio * 100) + '%', `有幫助 ${feedback.up}・沒幫助 ${feedback.down}（共 ${rated}）`),
      statTile('達到每人額度上限', `${usage.capped_users} 人`, '期間內至少一天用完額度'));

    const cost = document.getElementById('cost-chart');
    cost.replaceChildren(columnChart(data.daily, [{ key: 'router_cost', label: '路由', className: 'segment-router' },
      { key: 'answer_cost', label: '回答', className: 'segment-answer' }], usd));
    document.getElementById('question-chart').replaceChildren(
      columnChart(data.daily, [{ key: 'questions', label: '提問', className: 'segment-questions' }], (n) => `${n} 則`));
    document.getElementById('feedback-department').replaceChildren(feedbackBars(feedback.by_department, (item) => item.department));
    document.getElementById('feedback-status').replaceChildren(
      feedbackBars(feedback.by_status, (item) => STATUS_NAMES[item.status] || item.status));

    document.getElementById('down-list').replaceChildren(dataTable([
      { label: '時間', value: (item) => taipeiDateTime(item.created_at) },
      { label: '使用者', value: (item) => `${item.user.name}（${item.department}）` },
      { label: '問題', value: (item) => link(`/admin/users/${item.user.id}?c=${item.conversation_id}`, item.question || '（無）') },
      { label: '回答狀態', value: (item) => STATUS_NAMES[item.status] || item.status },
      { label: '意見', value: (item) => item.comment || '—' },
    ], feedback.down_list, '這段期間沒有「沒幫助」的回答。'));
    document.getElementById('top-users').replaceChildren(dataTable([
      { label: '使用者', value: (u) => link('/admin/users/' + u.user_id, u.name || '（已刪除）') },
      { label: 'Email', value: (u) => u.email },
      { label: '部門', value: (u) => u.department },
      { label: '提問數', value: (u) => u.questions, numeric: true },
      { label: '花費', value: (u) => usd(u.cost), numeric: true },
    ], data.top_users, '這段期間沒有用量。'));
    document.getElementById('daily-table').replaceChildren(dataTable([
      { label: '日期', value: (d) => d.date.replaceAll('-', '.') },
      { label: '提問', value: (d) => d.questions, numeric: true },
      { label: '新註冊', value: (d) => d.new_users, numeric: true },
      { label: '路由花費', value: (d) => usd(d.router_cost), numeric: true },
      { label: '回答花費', value: (d) => usd(d.answer_cost), numeric: true },
      { label: '有幫助', value: (d) => d.feedback_up, numeric: true },
      { label: '沒幫助', value: (d) => d.feedback_down, numeric: true },
      { label: '達上限人數', value: (d) => d.capped_users, numeric: true },
    ], [...data.daily].reverse(), '沒有資料。', { keepTable: true }));
  }

  const initial = [7, 14, 30].includes(Number(new URLSearchParams(location.search).get('days')))
    ? Number(new URLSearchParams(location.search).get('days')) : 14;
  buttons.forEach((button) => button.addEventListener('click', async () => {
    const days = Number(button.dataset.days);
    history.replaceState(null, '', `${location.pathname}?days=${days}`);
    try { await load(days); } catch (error) { showError(error); }
  }));
  await load(initial);
};
