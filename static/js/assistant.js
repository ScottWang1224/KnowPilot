'use strict';
/* AI assistant page. Loaded before app.js, which runs KP_PAGES.assistant after login; it uses
   app.js helpers (api, el, taipeiDate) only at call time. Text always goes through textContent. */
window.KP_PAGES = window.KP_PAGES || {};

const SLOW_AFTER_MS = 30000;
const REVEAL_STEP_MS = 150;
const SEPARATOR = ' › ';

function citationLabel(citation) {
  if (citation.doc_code.startsWith('ANN:')) return '公告｜' + citation.doc_title;
  return citation.doc_code + '｜' + citation.section_path.split(SEPARATOR).pop();
}

function citationList(citations) {
  const box = el('span', 'citations');
  for (const citation of citations) {
    const chip = el(citation.url ? 'a' : 'span', 'citation', citationLabel(citation));
    chip.title = citation.url ? citation.doc_title + '（在新分頁開啟）' : citation.doc_title + '（你的部門無法開啟這份文件）';
    if (citation.url) { chip.href = citation.url; chip.target = '_blank'; chip.rel = 'noopener'; }
    box.append(chip);
  }
  return box;
}

function safetyBox(text, citations) {
  const box = el('div', 'safety-note');
  box.setAttribute('role', 'note');
  box.append(el('strong', '', '安全提示'), el('span', '', text));
  if (citations && citations.length) box.append(citationList(citations));
  return box;
}

function relatedDocs(docs) {
  const list = el('ul', 'related-docs');
  for (const doc of docs) {
    const item = el('li');
    const label = doc.doc_code.startsWith('ANN:') ? '公告｜' + doc.doc_title : `${doc.doc_code} ${doc.doc_title}`;
    const link = el(doc.url ? 'a' : 'span', doc.url ? 'text-link' : '', label);
    if (doc.url) { link.href = doc.url; link.target = '_blank'; link.rel = 'noopener'; }
    item.append(link);
    list.append(item);
  }
  return list;
}

/* The blocks of one assistant reply, in reading order; each one fades in on its own. */
function replyBlocks(message, { latest, onOption, onFeedback }) {
  const payload = message.payload || {};
  const blocks = [];
  if (message.kind === 'clarify') {
    if (payload.safety_note) blocks.push(safetyBox(payload.safety_note));
    blocks.push(el('p', 'reply-text', payload.question));
    const options = el('div', 'clarify-options');
    for (const option of payload.options || []) {
      const button = el('button', 'option-button', option);
      button.type = 'button';
      button.disabled = !latest;
      button.addEventListener('click', () => onOption(option));
      options.append(button);
    }
    blocks.push(options);
  } else if (message.kind === 'answer') {
    if (payload.safety_note) blocks.push(safetyBox(payload.safety_note.text, payload.safety_note.citations));
    if (payload.status === 'not_found') blocks.push(el('p', 'reply-text', payload.text));
    for (const point of payload.points || []) {
      const item = el('div', 'answer-point');
      if (point.condition) item.append(el('span', 'condition', point.condition));
      item.append(el('span', '', point.text), citationList(point.citations));
      blocks.push(item);
    }
    // The model writes `missing` as a full sentence ("文件沒有寫明…"), so it is shown as is, without a label.
    if (payload.missing) blocks.push(el('p', 'answer-missing', payload.missing));
    blocks.push(feedbackBar(message, onFeedback));
  } else {
    blocks.push(el('p', 'reply-text', payload.text || message.content));
    if (payload.related_docs && payload.related_docs.length) blocks.push(relatedDocs(payload.related_docs));
  }
  return blocks;
}

function feedbackBar(message, onFeedback) {
  const bar = el('div', 'feedback');
  const up = el('button', 'feedback-button', '有幫助');
  const down = el('button', 'feedback-button', '沒幫助');
  const form = el('form', 'feedback-form');
  const comment = el('textarea');
  const status = el('span', 'feedback-status');
  up.type = down.type = 'button';
  comment.maxLength = 500;
  comment.rows = 2;
  comment.placeholder = '哪裡不夠好？（選填，最多 500 字）';
  comment.setAttribute('aria-label', '回饋意見');
  const submit = el('button', 'button button-outline', '送出意見');
  submit.type = 'submit';
  form.append(comment, submit);
  form.hidden = true;

  const show = (feedback) => {
    up.setAttribute('aria-pressed', String(feedback?.rating === 'up'));
    down.setAttribute('aria-pressed', String(feedback?.rating === 'down'));
    if (feedback?.comment) comment.value = feedback.comment;
  };
  const save = async (rating, text) => {
    status.textContent = '';
    try {
      const { data } = await onFeedback(message.id, rating, text);
      show(data);
      status.textContent = text ? '已收到你的意見，謝謝！' : '謝謝回饋！';
      return true;
    } catch (error) {
      status.textContent = error.message;
      return false;
    }
  };
  up.addEventListener('click', async () => { if (await save('up', null)) form.hidden = true; });
  down.addEventListener('click', async () => { if (await save('down', comment.value.trim() || null)) form.hidden = false; });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (await save('down', comment.value.trim() || null)) form.hidden = true;
  });
  show(message.feedback);
  bar.append(el('span', 'feedback-label', '這個回答有幫助嗎？'), up, down, status, form);
  return bar;
}

KP_PAGES.assistant = async function assistantPage() {
  const root = document.getElementById('assistant');
  const log = document.getElementById('chat-log');
  const welcome = log.querySelector('[data-welcome]');
  const form = document.getElementById('chat-form');
  const input = document.getElementById('chat-input');
  const send = form.querySelector('.send-button');
  const notice = form.querySelector('.chat-notice');
  const counter = form.querySelector('[data-char-count]');
  const list = document.getElementById('conversation-list');
  const empty = root.querySelector('[data-conversation-empty]');
  const toggle = root.querySelector('.conversation-toggle');
  const hideButton = root.querySelector('[data-hide-chat]');
  const quotaBox = root.querySelector('[data-quota]');
  const maxChars = Number(root.dataset.maxChars) || 500;
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let conversationId = null;
  let sending = false;

  const showNotice = (text) => { notice.textContent = text; notice.hidden = !text; };

  function updateCount() {
    const length = input.value.length;
    counter.textContent = `${length} / ${maxChars}`;
    counter.classList.toggle('is-over', length > maxChars);
    send.disabled = sending || !input.value.trim() || length > maxChars;
  }

  function showQuota(quota) {
    quotaBox.hidden = false;
    const bar = quotaBox.querySelector('progress');
    const text = quotaBox.querySelector('[data-quota-text]');
    bar.hidden = quota.unlimited;
    if (quota.unlimited) { text.textContent = '管理員不限額度'; return; }
    bar.value = quota.percent_used;
    quotaBox.classList.toggle('is-high', quota.percent_used >= 80);
    // The server refuses only once the allowance is spent; "0 questions" is just an estimate from the average cost.
    if (quota.percent_used >= 100) text.textContent = '今日額度已用完，明天再來問吧';
    else if (quota.remaining_questions <= 0) text.textContent = `已用 ${quota.percent_used}%・剩餘額度可能不足 1 題`;
    else text.textContent = `已用 ${quota.percent_used}%・約可再問 ${quota.remaining_questions} 題`;
  }

  function scrollToMessage(node) {
    node.scrollIntoView({ block: 'start', behavior: reducedMotion.matches ? 'auto' : 'smooth' });
  }

  function disableOptions() {
    log.querySelectorAll('.option-button').forEach((button) => { button.disabled = true; });
  }

  function appendUser(text) {
    welcome.hidden = true;
    const bubble = el('div', 'message message-user', text);
    log.append(bubble);
    return bubble;
  }

  function appendThinking() {
    const box = el('div', 'message message-assistant thinking');
    const dots = el('span', 'thinking-dots');
    dots.setAttribute('aria-hidden', 'true');
    dots.append(el('span'), el('span'), el('span'));
    const label = el('span', '', '思考中…');
    box.append(dots, label);
    log.append(box);
    box.scrollIntoView({ block: 'nearest' });
    const slow = setTimeout(() => { label.textContent = '問題較複雜，仍在處理中…'; }, SLOW_AFTER_MS);
    return { remove() { clearTimeout(slow); box.remove(); } };
  }

  function appendReply(message, { animate = false, latest = false } = {}) {
    welcome.hidden = true;
    const box = el('div', 'message message-assistant');
    const blocks = replyBlocks(message, {
      latest,
      onOption: (option) => submit(option),
      onFeedback: (id, rating, comment) => api(`/api/messages/${id}/feedback`, { method: 'PUT', body: { rating, comment } }),
    });
    blocks.forEach((block, i) => {
      if (animate) { block.classList.add('reveal'); block.style.animationDelay = `${i * REVEAL_STEP_MS}ms`; }
      box.append(block);
    });
    log.append(box);
    return box;
  }

  function setUrl() {
    const url = conversationId ? `/assistant?c=${conversationId}` : '/assistant';
    history.replaceState(null, '', url);
  }

  function startNew() {
    conversationId = null;
    log.replaceChildren(welcome);
    welcome.hidden = false;
    hideButton.hidden = true;
    setUrl();
    markCurrent();
    showNotice('');
    input.focus();
  }

  function markCurrent() {
    list.querySelectorAll('button').forEach((button) => {
      button.setAttribute('aria-current', String(Number(button.dataset.id) === conversationId));
    });
  }

  function closePanel() {
    root.classList.remove('show-conversations');
    toggle.setAttribute('aria-expanded', 'false');
  }

  async function loadConversations() {
    const { data } = await api('/api/conversations');
    list.replaceChildren(...data.map((conversation) => {
      const item = el('li');
      const button = el('button');
      button.type = 'button';
      button.dataset.id = conversation.id;
      button.append(el('span', 'conversation-title', conversation.title), el('span', 'conversation-date', taipeiDate(conversation.updated_at)));
      button.addEventListener('click', () => { closePanel(); openConversation(conversation.id); });
      item.append(button);
      return item;
    }));
    empty.hidden = data.length > 0;
    markCurrent();
  }

  async function openConversation(id) {
    showNotice('');
    try {
      const { data } = await api(`/api/conversations/${id}/messages`);
      conversationId = data.conversation.id;
      log.replaceChildren(welcome);
      const messages = data.messages;
      messages.forEach((message, i) => {
        if (message.role === 'user') appendUser(message.content);
        else appendReply(message, { latest: i === messages.length - 1 });
      });
      hideButton.hidden = false;
      setUrl();
      markCurrent();
      const last = log.lastElementChild;
      if (last) last.scrollIntoView({ block: 'end' });
    } catch (error) {
      startNew();
      showNotice(error.status === 404 ? '找不到這段對話，可能已經隱藏或屬於其他部門。' : error.message);
    }
  }

  async function submit(text) {
    text = text.trim();
    if (!text || sending) return;
    if (text.length > maxChars) { showNotice(`問題超過 ${maxChars} 字上限`); return; }
    showNotice('');
    sending = true;
    disableOptions();
    const bubble = appendUser(text);
    const thinking = appendThinking();
    const draft = input.value;
    input.value = '';
    updateCount();
    try {
      const result = await api('/api/chat', { method: 'POST', body: { conversation_id: conversationId, message: text } });
      thinking.remove();
      const isNew = conversationId !== result.conversation.id;
      conversationId = result.conversation.id;
      hideButton.hidden = false;
      setUrl();
      scrollToMessage(appendReply(result.message, { animate: true, latest: true }));
      showQuota(result.quota);
      if (isNew) await loadConversations();
    } catch (error) {
      thinking.remove();
      if (error.status === 400 || error.status === 429 || error.status === 404) {
        // Nothing was stored: take the question back into the input box.
        bubble.remove();
        input.value = draft || text;
        if (!log.querySelector('.message')) welcome.hidden = false;
        showNotice(error.status === 404 ? '這段對話已無法繼續，請開新對話。' : error.message);
      } else if (error.status !== 401) {
        appendReply({ kind: 'error', payload: { text: error.message } });
        loadConversations().catch(() => {});
      }
    } finally {
      sending = false;
      updateCount();
    }
  }

  form.addEventListener('submit', (event) => { event.preventDefault(); submit(input.value); });
  input.addEventListener('input', () => { updateCount(); if (!notice.hidden) showNotice(''); });
  input.addEventListener('keydown', (event) => {
    // isComposing: Enter that confirms a Chinese IME candidate must not send the question.
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      if (!send.disabled) form.requestSubmit();
    }
  });
  root.querySelector('[data-new-chat]').addEventListener('click', () => { closePanel(); startNew(); });
  toggle.addEventListener('click', () => {
    const open = !root.classList.contains('show-conversations');
    root.classList.toggle('show-conversations', open);
    toggle.setAttribute('aria-expanded', String(open));
  });
  hideButton.addEventListener('click', async () => {
    if (!conversationId) return;
    if (!window.confirm('隱藏後，這段對話會從你的對話紀錄中移除，你將無法再查看（管理員仍可檢視）。\n\n確定要隱藏嗎？')) return;
    try {
      await api(`/api/conversations/${conversationId}/hide`, { method: 'POST' });
      startNew();
      await loadConversations();
    } catch (error) {
      showNotice(error.message);
    }
  });

  updateCount();
  const [{ data: quota }] = await Promise.all([api('/api/chat/quota'), loadConversations()]);
  showQuota(quota);
  const requested = Number(new URLSearchParams(location.search).get('c'));
  if (requested) await openConversation(requested);
};
