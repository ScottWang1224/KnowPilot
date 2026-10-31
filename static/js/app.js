'use strict';
const menu = document.querySelector('.menu-button');
const sidebar = document.querySelector('.sidebar');
if (menu && sidebar) {
  const closeMenu = () => {
    document.body.classList.remove('menu-open');
    menu.setAttribute('aria-expanded', 'false');
    menu.setAttribute('aria-label', '開啟導覽選單');
  };
  menu.addEventListener('click', () => {
    const open = !document.body.classList.contains('menu-open');
    document.body.classList.toggle('menu-open', open);
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', open ? '關閉導覽選單' : '開啟導覽選單');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('menu-open')) {
      closeMenu(); menu.focus();
    }
  });
  document.addEventListener('click', (event) => {
    if (!sidebar.contains(event.target) && !menu.contains(event.target)) closeMenu();
  });
  window.matchMedia('(min-width: 901px)').addEventListener('change', closeMenu);
}

/* Authentication: the token only decides what the UI shows. Every API checks it again. */
const TOKEN_KEY = 'knowpilot_token';
const token = {
  get() { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } },
  set(value) { try { localStorage.setItem(TOKEN_KEY, value); } catch { /* storage blocked */ } },
  clear() { try { localStorage.removeItem(TOKEN_KEY); } catch { /* storage blocked */ } },
};
const ROLE_LABELS = { user: '員工', admin: '管理員' };
const VISIBILITY_LABELS = { company: '全公司', department: '部門限定' };
const ERRORS = {
  404: ['找不到這個頁面', '此內容不存在，或目前無法存取。'],
  503: ['服務暫時無法使用', '資料服務暫時無法連線，請稍後再試或聯絡系統管理員。'],
};

function loginUrl() {
  return '/login?next=' + encodeURIComponent(location.pathname + location.search);
}

function safeNext(value) {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return '/';
  if (/[\u0000-\u001f]/.test(value)) return '/';
  const path = value.split(/[?#]/)[0];
  return path === '/login' || path === '/register' ? '/' : value;
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = 'GET', body, auth = true, redirect = auth } = {}) {
  const headers = {};
  const current = auth ? token.get() : null;
  if (current) headers.Authorization = 'Bearer ' + current;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, '無法連線到伺服器，請稍後再試。');
  }
  const data = await response.json().catch(() => null);
  if (response.status === 401 && auth) {
    token.clear();
    if (redirect) location.replace(loginUrl());
  }
  if (!response.ok) throw new ApiError(response.status, (data && data.message) || '無法完成請求，請稍後再試。');
  return data;
}

/* Rendering helpers: text always goes through textContent, never innerHTML. */
const dateFormat = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit' });
function taipeiDate(value) { return value ? dateFormat.format(new Date(value)).replaceAll('-', '.') : '—'; }

// A text-only element; shared by the page scripts (assistant.js, admin.js).
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

// Mirrors Jinja's truncate filter, which these lists used before.
function truncate(text, length) {
  if (text.length <= length + 5) return text;
  const cut = text.slice(0, length - 3);
  const space = cut.lastIndexOf(' ');
  return (space >= 0 ? cut.slice(0, space) : cut) + '...';
}

/* Markdown subset used by data/ files: headings, paragraphs, nested lists, tables, quotes, rules,
   **bold** and `code`. Built from DOM nodes so content never reaches innerHTML. */
const MD_LIST_ITEM = /^(\s*)([-*]|\d+\.)\s+(.*)$/;
const MD_TABLE_ROW = /^\s*\|.*\|\s*$/;
const MD_TABLE_RULE = /^\s*\|[\s:|-]+\|\s*$/;

function appendInline(parent, text) {
  let last = 0;
  for (const match of text.matchAll(/\*\*([^*]+)\*\*|`([^`]+)`/g)) {
    parent.append(text.slice(last, match.index));
    const node = document.createElement(match[1] !== undefined ? 'strong' : 'code');
    node.textContent = match[1] ?? match[2];
    parent.append(node);
    last = match.index + match[0].length;
  }
  parent.append(text.slice(last));
  return parent;
}

function mdElement(tag, text) { return appendInline(document.createElement(tag), text); }
function mdIndent(line) { return line.match(/^\s*/)[0].length; }
function mdCells(line) { return line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim()); }

function mdBlockStart(lines, i) {
  const line = lines[i];
  return /^#{1,6}\s/.test(line) || /^\s*(-{3,}|\*{3,})\s*$/.test(line) || line.startsWith('>')
    || MD_LIST_ITEM.test(line) || (MD_TABLE_ROW.test(line) && MD_TABLE_RULE.test(lines[i + 1] || ''));
}

function mdList(lines, start, indent) {
  const ordered = /^\s*\d+\./.test(lines[start]);
  const list = document.createElement(ordered ? 'ol' : 'ul');
  if (ordered) list.start = parseInt(lines[start], 10);
  let i = start;
  let item = null;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      let next = i + 1;
      while (next < lines.length && !lines[next].trim()) next++;
      if (next < lines.length && MD_LIST_ITEM.test(lines[next]) && mdIndent(lines[next]) >= indent) { i = next; continue; }
      break;
    }
    const match = line.match(MD_LIST_ITEM);
    const depth = mdIndent(line);
    if (match && depth === indent) {
      if (/^\d+\./.test(match[2]) !== ordered) break;
      item = mdElement('li', match[3]);
      list.append(item);
      i++;
    } else if (item && depth > indent && match) {
      const [child, next] = mdList(lines, i, depth);
      item.append(child);
      i = next;
    } else if (item && depth > indent) {
      appendInline(item, ' ' + line.trim());
      i++;
    } else break;
  }
  return [list, i];
}

function renderMarkdown(text) {
  const fragment = document.createDocumentFragment();
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (!line.trim()) {
      i++;
    } else if (heading) {
      fragment.append(mdElement('h' + Math.min(Math.max(heading[1].length, 2), 4), heading[2]));
      i++;
    } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      fragment.append(document.createElement('hr'));
      i++;
    } else if (MD_TABLE_ROW.test(line) && MD_TABLE_RULE.test(lines[i + 1] || '')) {
      const table = document.createElement('table');
      const head = table.createTHead().insertRow();
      mdCells(line).forEach((cell) => head.append(mdElement('th', cell)));
      const body = table.createTBody();
      for (i += 2; i < lines.length && MD_TABLE_ROW.test(lines[i]); i++) {
        const row = body.insertRow();
        mdCells(lines[i]).forEach((cell) => row.append(mdElement('td', cell)));
      }
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      wrap.append(table);
      fragment.append(wrap);
    } else if (line.startsWith('>')) {
      const parts = [];
      for (; i < lines.length && lines[i].startsWith('>'); i++) parts.push(lines[i].replace(/^>\s?/, ''));
      const quote = document.createElement('blockquote');
      quote.append(mdElement('p', parts.join(' ')));
      fragment.append(quote);
    } else if (MD_LIST_ITEM.test(line)) {
      const [list, next] = mdList(lines, i, mdIndent(line));
      fragment.append(list);
      i = next;
    } else {
      const parts = [];
      for (; i < lines.length && lines[i].trim() && (!parts.length || !mdBlockStart(lines, i)); i++) parts.push(lines[i].trim());
      fragment.append(mdElement('p', parts.join(' ')));
    }
  }
  return fragment;
}

// Excerpts for list pages: Markdown symbols removed, one line of plain text.
function plainText(markdown) {
  return markdown.split('\n')
    .filter((line) => !MD_TABLE_RULE.test(line) && !/^\s*(-{3,}|\*{3,})\s*$/.test(line))
    .map((line) => line.replace(/^\s*(#{1,6}|>|[-*]|\d+\.)\s+/, '').replace(/\|/g, ' ').replace(/\*\*|`/g, '').trim())
    .filter(Boolean).join(' ').replace(/\s+/g, ' ');
}

function fillMarkdown(root, fields) {
  for (const [name, value] of Object.entries(fields)) {
    root.querySelectorAll(`[data-markdown="${name}"]`).forEach((node) => { node.replaceChildren(renderMarkdown(value || '')); });
  }
}

function fill(root, fields) {
  for (const [name, value] of Object.entries(fields)) {
    root.querySelectorAll(`[data-field="${name}"]`).forEach((node) => { node.textContent = value ?? ''; });
    root.querySelectorAll(`[data-show="${name}"]`).forEach((node) => { node.hidden = !value; });
  }
  return root;
}

function fromTemplate(id, fields = {}, href) {
  const node = document.getElementById(id).content.firstElementChild.cloneNode(true);
  if (href) node.setAttribute('href', href);
  return fill(node, fields);
}

function renderList(container, items, templateId, toFields, toHref) {
  container.append(...(items.length
    ? items.map((item) => fromTemplate(templateId, toFields(item), toHref(item)))
    : [fromTemplate(templateId + '-empty')]));
}

function renderPagination(path, { page, total_pages: totalPages }, filters = {}) {
  const box = document.getElementById('pagination');
  if (!box || totalPages <= 1) return;
  const query = Object.fromEntries(Object.entries(filters).filter(([, value]) => value));
  const link = (number, text) => {
    const anchor = document.createElement('a');
    anchor.href = path + '?' + new URLSearchParams({ ...query, page: number });
    anchor.textContent = text;
    return anchor;
  };
  const nav = document.createElement('nav');
  nav.className = 'pagination';
  nav.setAttribute('aria-label', '頁面導覽');
  if (page > 1) nav.append(link(page - 1, '← 上一頁'));
  const label = document.createElement('span');
  label.textContent = `第 ${page} / ${totalPages} 頁`;
  nav.append(label);
  if (page < totalPages) nav.append(link(page + 1, '下一頁 →'));
  box.append(nav);
}

function showError(error) {
  const [title, message] = ERRORS[error.status] || ['無法完成請求', error.message];
  const main = document.getElementById('main-content');
  main.replaceChildren(fromTemplate('tpl-error', { status: error.status || '', title, message }));
}

function showAlert(text) {
  const alert = document.querySelector('.alert');
  alert.textContent = text;
  alert.hidden = !text;
}

function userFields(user) {
  return { name: user.name, initial: user.name.slice(0, 1), email: user.email,
    department: user.department.name, role: ROLE_LABELS[user.role] || '員工' };
}

function renderUser(user) {
  document.querySelectorAll('[data-user]').forEach((node) => { fill(node, userFields(user)); node.hidden = false; });
  document.querySelectorAll('[data-logout]').forEach((node) => { node.hidden = false; });
  // Only a shortcut: every /api/admin/* call is still checked by require_admin.
  document.querySelectorAll('[data-admin-link]').forEach((node) => { node.hidden = user.role !== 'admin'; });
  if (user.role === 'admin') showPendingReviews();
}

// The number of submissions waiting for review, next to 後台 in the sidebar. A failure only hides the badge.
async function showPendingReviews() {
  const badges = document.querySelectorAll('[data-admin-badge]');
  try {
    const { data } = await api('/api/admin/submissions/count');
    badges.forEach((badge) => {
      badge.textContent = data.pending;
      badge.hidden = !data.pending;
      badge.setAttribute('aria-label', `${data.pending} 筆文件待審核`);
    });
  } catch { badges.forEach((badge) => { badge.hidden = true; }); }
}

function documentFields(doc) {
  return { title: doc.title, document_code: doc.document_code, category: doc.category, summary: doc.summary,
    department: doc.department_name || '全公司', visibility: VISIBILITY_LABELS[doc.visibility] || '',
    date: taipeiDate(doc.updated_at), updated: '更新 ' + taipeiDate(doc.updated_at) };
}

async function loadDepartments(select, selected) {
  const { data } = await api('/api/departments', { auth: false });
  for (const department of data) select.append(new Option(department.name, department.id, false, department.id === selected));
}

/* Citation links open /documents/{id}?section=<heading>: compare headings with whitespace removed after NFKC,
   the same normalisation the router uses, so no slug rules are needed on either side. */
function sectionKey(text) { return text.normalize('NFKC').replace(/\s+/g, ''); }

function jumpToSection(article, section) {
  if (!section) return;
  const wanted = sectionKey(section);
  const heading = [...article.querySelectorAll('.markdown h2, .markdown h3')].find((node) => sectionKey(node.textContent) === wanted);
  if (!heading) return;
  heading.tabIndex = -1;
  heading.classList.add('section-flash');
  // aria-busy hides the page until loading ends, so scroll on the next frame.
  requestAnimationFrame(() => { heading.scrollIntoView({ block: 'start' }); heading.focus({ preventScroll: true }); });
}

/* "My memory" on the profile page: one row per key the server offers, values only from its options. */
const MEMORY_SOURCES = { manual: '手動設定', chat: '對話中設定' };

function memoryEditor(section) {
  const list = section.querySelector('#memory-list');
  const clear = section.querySelector('[data-clear-memory]');
  const alert = section.querySelector('[data-memory-alert]');
  const status = section.querySelector('[data-memory-status]');
  const report = (text, isError = false) => {
    alert.textContent = isError ? text : '';
    alert.hidden = !isError;
    status.textContent = isError ? '' : text;
  };

  function row(key, label, options, item) {
    const li = el('li', 'memory-row' + (item && !item.valid ? ' is-invalid' : ''));
    const id = 'memory-' + key;
    const name = el('label', 'memory-label', label);
    name.htmlFor = id;
    const select = el('select', 'memory-select');
    select.id = id;
    select.append(new Option('未設定', ''));
    if (item && !item.valid) select.append(new Option(`${item.value}（已失效）`, item.value, true, true));
    for (const value of options) select.append(new Option(value, value, false, item?.valid && item.value === value));
    const meta = el('span', 'memory-meta', item ? (item.valid ? MEMORY_SOURCES[item.source] || '' : '已失效，請重新選擇') : '');
    const remove = el('button', 'text-button memory-remove', '刪除');
    remove.type = 'button';
    remove.hidden = !item;
    remove.setAttribute('aria-label', `刪除${label}`);
    select.addEventListener('change', () => (select.value ? save(key, label, select.value) : drop(key, label)));
    remove.addEventListener('click', () => drop(key, label));
    li.append(name, select, meta, remove);
    return li;
  }

  function render({ items, options, labels }) {
    const stored = Object.fromEntries(items.map((item) => [item.key, item]));
    const keys = [...new Set([...Object.keys(options), ...Object.keys(stored)])];
    list.replaceChildren(...keys.map((key) => row(key, labels[key] || stored[key]?.label || key, options[key] || [], stored[key])));
    clear.hidden = items.length === 0;
  }

  async function run(request, done) {
    try {
      const result = await request();
      render(result.data);
      report(done);
    } catch (error) {
      report(error.message, true);
      await load().catch(() => {});
    }
  }

  const save = (key, label, value) => run(() => api('/api/memories/' + encodeURIComponent(key), { method: 'PUT', body: { value } }),
    `已記住${label}：${value}。`);
  const drop = (key, label) => run(() => api('/api/memories/' + encodeURIComponent(key), { method: 'DELETE' }), `已刪除${label}。`);

  async function load() {
    const { data } = await api('/api/memories');
    render(data);
  }

  clear.addEventListener('click', () => {
    if (!window.confirm('確定要清除全部記憶嗎？清除後無法復原。')) return;
    run(() => api('/api/memories', { method: 'DELETE' }), '已清除全部記憶。');
  });
  return { load };
}

/* Pages; page scripts such as assistant.js add theirs to window.KP_PAGES before app.js runs. */
const PAGES = {
  async index(user) {
    const [announcements, documents] = await Promise.all([api('/api/announcements'), api('/api/documents')]);
    const main = document.getElementById('main-content');
    fill(main, { announcement_count: announcements.total, document_count: documents.total,
      department: user.department.name, role: ROLE_LABELS[user.role] || '員工' });
    main.querySelector('[data-document-count]').dataset.documentCount = documents.total;
    renderList(document.getElementById('announcement-preview'), announcements.data.slice(0, 3), 'tpl-announcement-preview',
      (item) => ({ category: item.category, is_pinned: item.is_pinned, date: taipeiDate(item.published_at),
        title: item.title, excerpt: truncate(plainText(item.content), 85) }),
      (item) => '/announcements/' + item.id);
    renderList(document.getElementById('recent-documents'), documents.data.slice(0, 4), 'tpl-document-row',
      (doc) => ({ ...documentFields(doc), version: 'v' + doc.version }), (doc) => '/documents/' + doc.id);
  },

  async announcements() {
    const page = new URLSearchParams(location.search).get('page') || 1;
    const result = await api('/api/announcements?' + new URLSearchParams({ page }));
    fill(document.getElementById('main-content'), { total: result.total });
    renderList(document.getElementById('announcement-list'), result.data, 'tpl-announcement',
      (item) => ({ category: item.category, is_pinned: item.is_pinned, title: item.title,
        excerpt: truncate(plainText(item.content), 100), date: taipeiDate(item.published_at) }),
      (item) => '/announcements/' + item.id);
    renderPagination('/announcements', result);
  },

  async announcement_detail() {
    const article = document.querySelector('[data-id]');
    const { data: item } = await api('/api/announcements/' + article.dataset.id);
    document.title = item.title + ' · KnowPilot';
    fill(article, { category: item.category, is_pinned: item.is_pinned, title: item.title,
      published: '發布時間 ' + taipeiDate(item.published_at), author: item.author_name || '系統管理員' });
    fillMarkdown(article, { content: item.content });
  },

  async documents() {
    const params = new URLSearchParams(location.search);
    const filters = { q: (params.get('q') || '').trim(), category: params.get('category') || '' };
    const result = await api('/api/documents?' + new URLSearchParams({ ...filters, page: params.get('page') || 1 }));
    const main = document.getElementById('main-content');
    main.querySelector('#q').value = filters.q;
    const select = main.querySelector('#category');
    for (const value of result.categories) select.append(new Option(value, value, false, value === filters.category));
    fill(main, { total: result.total });
    renderList(document.getElementById('document-list'), result.data, 'tpl-document',
      (doc) => ({ ...documentFields(doc), version: 'v' + doc.version }), (doc) => '/documents/' + doc.id);
    main.querySelectorAll('[data-show-filtered]').forEach((node) => { node.hidden = !(filters.q || filters.category); });
    renderPagination('/documents', result, filters);
  },

  async document_detail() {
    const article = document.querySelector('[data-id]');
    const { data: doc } = await api('/api/documents/' + article.dataset.id);
    document.title = doc.title + ' · KnowPilot';
    fill(article, { ...documentFields(doc), version: '版本 ' + doc.version });
    fillMarkdown(article, { content: doc.content });
    // Shortcuts only: the submissions API checks the department and pending revisions again.
    article.querySelector('[data-revise]').hidden = !doc.can_edit;
    article.querySelector('[data-revision-pending]').hidden = !doc.revision_pending;
    article.querySelector('[data-document-actions]').hidden = !(doc.can_edit || doc.revision_pending);
    jumpToSection(article, new URLSearchParams(location.search).get('section'));
  },

  async profile(user) {
    const main = document.getElementById('main-content');
    fill(main, userFields(user));
    const form = document.getElementById('department-form');
    const select = form.querySelector('select');
    const status = main.querySelector('.form-status');
    const memory = memoryEditor(document.getElementById('memory'));
    await Promise.all([loadDepartments(select, user.department.id), memory.load()]);
    if (location.hash === '#memory') document.getElementById('memory').scrollIntoView();
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      showAlert('');
      status.textContent = '';
      const departmentId = Number(select.value);
      if (departmentId === user.department.id) return;
      // Demo feature: switching department hides earlier conversations, deletes this user's memory and
      // withdraws their pending document submissions.
      if (!window.confirm('切換部門會隱藏你先前的對話，並清除你的記憶。\n審核中的文件提交也會一併撤回。\n\n確定要切換部門嗎？')) {
        select.value = user.department.id;
        return;
      }
      try {
        const result = await api('/api/auth/me/department', { method: 'PATCH',
          body: { department_id: departmentId, confirm_clear_history: true } });
        user = result.data;
        fill(main, userFields(user));
        renderUser(user);
        status.textContent = `已切換至${user.department.name}。`
          + (result.deleted_memories ? `已清除 ${result.deleted_memories} 項記憶。` : '')
          + (result.withdrawn_submissions ? `已撤回 ${result.withdrawn_submissions} 筆待審提交。` : '');
        await memory.load();
      } catch (error) {
        showAlert(error.message);
      }
    });
  },
};

async function initLogin() {
  const next = safeNext(new URLSearchParams(location.search).get('next'));
  if (token.get()) {
    try { await api('/api/auth/me', { redirect: false }); location.replace(next); return; } catch { /* stale token was cleared */ }
  }
  const form = document.getElementById('login-form');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    showAlert('');
    const fields = Object.fromEntries(new FormData(form));
    if (!fields.email.trim() || !fields.password) { showAlert('請輸入電子信箱與密碼。'); return; }
    try {
      const result = await api('/api/auth/login', { method: 'POST', auth: false, body: fields });
      token.set(result.token);
      location.replace(next);
    } catch (error) {
      showAlert(error.message);
    }
  });
}

async function initRegister() {
  const form = document.getElementById('register-form');
  try { await loadDepartments(form.querySelector('select')); } catch (error) { showAlert(error.message); }
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    showAlert('');
    const fields = Object.fromEntries(new FormData(form));
    if (!fields.name.trim() || !fields.email.trim() || !fields.department_id) { showAlert('請填寫所有欄位。'); return; }
    if (fields.password.length < 8) { showAlert('密碼至少需要 8 個字元。'); return; }
    const credentials = { email: fields.email, password: fields.password };
    try {
      await api('/api/auth/register', { method: 'POST', auth: false,
        body: { ...fields, department_id: Number(fields.department_id) } });
      const result = await api('/api/auth/login', { method: 'POST', auth: false, body: credentials });
      token.set(result.token);
      location.replace('/');
    } catch (error) {
      showAlert(error.message);
    }
  });
}

async function initPortal(name) {
  const main = document.getElementById('main-content');
  const load = PAGES[name] || (window.KP_PAGES || {})[name];
  if (!load) {
    main.removeAttribute('aria-busy');  // Server-rendered pages such as error.html.
    return;
  }
  if (!token.get()) { location.replace(loginUrl()); return; }
  try {
    const { data: user } = await api('/api/auth/me');
    renderUser(user);
    await load(user);
  } catch (error) {
    if (error.status === 401) return;
    showError(error);
  }
  main.removeAttribute('aria-busy');
}

document.querySelectorAll('[data-logout]').forEach((button) => button.addEventListener('click', () => {
  token.clear();
  location.replace('/login');
}));

const pageName = document.body.dataset.page;
if (pageName === 'login') initLogin();
else if (pageName === 'register') initRegister();
else if (pageName) initPortal(pageName);
