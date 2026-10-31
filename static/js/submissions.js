'use strict';
/* Document submissions: the editor (new document, revision, edit after rejection) and "my submissions".
   Loaded before app.js like the other page scripts; uses its helpers (api, el, fill, renderMarkdown, taipeiDate,
   token) at call time. Text always goes through textContent or renderMarkdown, never innerHTML. */
window.KP_PAGES = window.KP_PAGES || {};

const KIND_LABELS = { create: '新增', revise: '修改' };
const PREVIEW_DELAY_MS = 250;

function formFields(form) {
  const value = (name) => form.elements[name].value;
  return { title: value('title'), summary: value('summary'), category: value('category'), content: value('content'),
    visibility: form.querySelector('input[name="visibility"]:checked').value,
    change_note: value('change_note') || null, version_bump: form.querySelector('input[name="version_bump"]:checked').value };
}

function setFields(form, data) {
  for (const name of ['title', 'summary', 'content', 'change_note']) {
    if (data[name] !== undefined && data[name] !== null) form.elements[name].value = data[name];
  }
  if (data.category && [...form.elements.category.options].some((o) => o.value === data.category)) {
    form.elements.category.value = data.category;
  }
  for (const name of ['visibility', 'version_bump']) {
    const choice = data[name] && form.querySelector(`input[name="${name}"][value="${data[name]}"]`);
    if (choice) choice.checked = true;
  }
}

/* The upload goes as multipart form data, so it cannot use api(), which sends JSON. */
async function parseMarkdownFile(file) {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetch('/api/submissions/parse-markdown', { method: 'POST', body: formData,
    headers: { Authorization: 'Bearer ' + token.get() } });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error((data && data.message) || '無法讀取這個檔案');
  return data.data;
}

KP_PAGES.submission_form = async function submissionFormPage() {
  const form = document.getElementById('submission-form');
  const { mode, documentId, submissionId } = form.dataset;
  const alert = form.querySelector('.alert');
  const preview = form.querySelector('[data-preview]');
  const counter = form.querySelector('[data-char-count]');
  const upload = form.querySelector('[data-upload-input]');
  const submit = form.querySelector('button[type="submit"]');
  const showAlert = (text) => { alert.textContent = text; alert.hidden = !text; if (text) alert.scrollIntoView({ block: 'nearest' }); };
  let timer = null;

  const refresh = () => {
    preview.replaceChildren(renderMarkdown(form.elements.content.value));
    counter.textContent = `${form.elements.content.value.length.toLocaleString()} / 100,000 字`;
  };
  let kind = mode === 'resubmit' ? null : (mode === 'revise' ? 'revise' : 'create');
  if (mode === 'revise') {
    const { data: doc } = await api('/api/documents/' + documentId);
    if (!doc.can_edit) {
      showAlert(doc.revision_pending ? '這份文件已有修改在審核中，請等審核完成後再提出。' : '這份文件不開放由你提出修改。');
      submit.disabled = true;
    }
    setFields(form, doc);
    fill(form, { document_code: doc.document_code, live_version: doc.version });
    form.querySelector('[data-revise-meta]').hidden = false;
  } else if (mode === 'resubmit') {
    const { data: sub } = await api('/api/submissions/' + submissionId);
    kind = sub.kind;
    setFields(form, sub);
    fill(form, { document_code: sub.document_code, live_version: sub.live_version || '—', review_note: sub.review_note });
    form.querySelector('[data-revise-meta]').hidden = sub.kind !== 'revise';
    form.querySelector('[data-review-note]').hidden = !sub.review_note;
    if (sub.status !== 'rejected') {
      showAlert(`這筆提交目前是「${sub.status_label}」，只有被退回的提交可以修改後再送審。`);
      submit.disabled = true;
    }
  }
  form.querySelectorAll('[data-revise-only]').forEach((node) => { node.hidden = kind !== 'revise'; });
  refresh();

  form.elements.content.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(refresh, PREVIEW_DELAY_MS); });
  form.querySelector('[data-upload]').addEventListener('click', () => upload.click());
  upload.addEventListener('change', async () => {
    const [file] = upload.files;
    upload.value = '';
    if (!file) return;
    showAlert('');
    try {
      setFields(form, await parseMarkdownFile(file));
      refresh();
    } catch (error) {
      showAlert(error.message);
    }
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    showAlert('');
    const fields = formFields(form);
    if (kind !== 'revise') { fields.change_note = null; fields.version_bump = null; }
    submit.disabled = true;
    try {
      const result = mode === 'resubmit'
        ? await api('/api/submissions/' + submissionId, { method: 'PUT', body: fields })
        : await api('/api/submissions', { method: 'POST',
          body: { ...fields, kind, document_id: kind === 'revise' ? Number(documentId) : null } });
      location.assign('/submissions?sent=' + result.data.id);
    } catch (error) {
      showAlert(error.message);
      submit.disabled = false;
    }
  });
};

function submissionCard(sub, onWithdraw) {
  const card = el('article', `submission-card is-${sub.status}`);
  const head = el('div', 'submission-card-head');
  head.append(el('span', `status-tag is-${sub.status}`, sub.status_label), el('span', 'tag', KIND_LABELS[sub.kind]),
    el('span', 'document-code', sub.document_code));
  card.append(head, el('h2', '', sub.title), el('p', 'muted', sub.summary));
  const meta = el('p', 'submission-card-meta', `${sub.category}・送出 ${taipeiDate(sub.submitted_at)}`
    + (sub.reviewed_at ? `・審核 ${taipeiDate(sub.reviewed_at)}` : ''));
  card.append(meta);
  if (sub.status === 'rejected' && sub.review_note) {
    const note = el('p', 'review-note');
    note.append(el('strong', '', '退回原因：'), el('span', '', sub.review_note));
    card.append(note);
  }
  const actions = el('div', 'submission-card-actions');
  if (sub.status === 'pending') {
    const withdraw = el('button', 'button button-outline', '撤回');
    withdraw.type = 'button';
    withdraw.addEventListener('click', () => onWithdraw(sub, withdraw));
    actions.append(withdraw);
  }
  if (sub.status === 'rejected') {
    const edit = el('a', 'button button-primary', '修改後再送審');
    edit.href = `/submissions/${sub.id}/edit`;
    actions.append(edit);
  }
  if (sub.status === 'approved' && sub.document_id) {
    const open = el('a', 'text-link', '查看文件');
    open.href = '/documents/' + sub.document_id;
    actions.append(open);
  }
  if (actions.childElementCount) card.append(actions);
  return card;
}

KP_PAGES.submissions = async function submissionsPage() {
  const list = document.getElementById('submission-list');
  const status = document.querySelector('[data-status]');
  const sent = new URLSearchParams(location.search).get('sent');
  if (sent) status.textContent = '已送出審核，管理員審核後會在這裡更新狀態。';

  async function load() {
    const { data } = await api('/api/submissions');
    if (!data.length) {
      const empty = el('div', 'empty-state');
      empty.append(el('h2', '', '還沒有提交'), el('p', '', '在文件中心按「新增文件」，或在自己部門的文件頁按「提出修改」。'));
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(...data.map((sub) => submissionCard(sub, withdraw)));
  }

  async function withdraw(sub, button) {
    if (!window.confirm(`確定要撤回「${sub.title}」嗎？撤回後不會進入審核。`)) return;
    button.disabled = true;
    try {
      await api(`/api/submissions/${sub.id}/withdraw`, { method: 'POST' });
      status.textContent = `已撤回「${sub.title}」。`;
      await load();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  }
  await load();
};
