const chatState = {
  sessions: [],
  activeSessionId: null,
  messages: [],
  sending: false,
};

function chatSelectedProjectId() {
  return $('chatProject').value || null;
}

async function loadChatProjects() {
  $('chatProject').innerHTML = '<option value="">General chat — no workspace</option>'
    + state.projects.map(p => `<option value="${esc(p.id)}">${esc(p.name)} — workspace tools</option>`).join('');
  updateChatCapabilityStatus();
}

function updateChatCapabilityStatus() {
  const capability = chatSelectedProjectId() ? 'Workspace tools enabled' : 'General chat — model only';
  const model = $('chatModelSwitcher').value;
  $('chatModelStatus').textContent = model ? `${capability} · ${model}` : capability;
}

async function loadChatModels() {
  const modelData = await api('/api/models');
  $('chatModelSwitcher').innerHTML = `<option value="auto" ${modelData.active === 'auto' ? 'selected' : ''}>Auto — recommended</option>`
    + modelData.models.map(model => `<option value="${esc(model.name)}" ${model.name === modelData.active ? 'selected' : ''}>${esc(model.name)}</option>`).join('');
  updateChatCapabilityStatus();
}

async function loadChatSessions() {
  const projectId = chatSelectedProjectId();
  const archived = $('chatShowArchived').checked;
  const path = projectId ? `/projects/${projectId}/chat/sessions` : '/chat/sessions';
  chatState.sessions = await api(`${path}?archived=${archived}`);
  if (!chatState.sessions.some(session => session.id === chatState.activeSessionId)) {
    chatState.activeSessionId = null;
    chatState.messages = [];
    renderChatThread();
  }
  renderChatSessions();
  if (!chatState.activeSessionId && chatState.sessions.length) {
    await openChatSession(chatState.sessions[0].id);
  }
}

function renderChatSessions() {
  const list = $('chatSessionList');
  if (!chatState.sessions.length) {
    list.innerHTML = `<div class="empty-state"><p>No chats yet.</p></div>`;
    return;
  }
  list.innerHTML = chatState.sessions.map(s => `<article class="chat-session-item ${s.id === chatState.activeSessionId ? 'active' : ''} ${s.archived_at ? 'archived' : ''}" data-session="${s.id}">
    <span class="chat-session-title">${esc(s.title)}</span>
    <button type="button" class="ghost small" data-archive-toggle="${s.id}">${s.archived_at ? 'Unarchive' : 'Archive'}</button>
    <button type="button" class="ghost small danger" data-session-delete="${s.id}" title="Delete chat">✕</button>
  </article>`).join('');
  list.querySelectorAll('[data-session]').forEach(el => el.onclick = e => {
    if (e.target.closest('[data-archive-toggle]') || e.target.closest('[data-session-delete]')) return;
    openChatSession(el.dataset.session);
  });
  list.querySelectorAll('[data-archive-toggle]').forEach(btn => btn.onclick = async e => {
    e.stopPropagation();
    const id = btn.dataset.archiveToggle;
    const session = chatState.sessions.find(s => s.id === id);
    await api(`/chat/sessions/${id}/${session.archived_at ? 'unarchive' : 'archive'}`, { method: 'POST' });
    await loadChatSessions();
  });
  list.querySelectorAll('[data-session-delete]').forEach(btn => btn.onclick = async e => {
    e.stopPropagation();
    const id = btn.dataset.sessionDelete;
    const session = chatState.sessions.find(s => s.id === id);
    if (!confirm(`Delete chat "${session ? session.title : id}"? This cannot be undone.`)) return;
    await api(`/chat/sessions/${id}`, { method: 'DELETE' });
    if (chatState.activeSessionId === id) chatState.activeSessionId = null;
    await loadChatSessions();
  });
}

async function openChatSession(sessionId) {
  chatState.activeSessionId = sessionId;
  renderChatSessions();
  chatState.messages = await api(`/chat/sessions/${sessionId}/messages`);
  renderChatThread();
}

function renderChatThread() {
  const thread = $('chatThread');
  if (!chatState.activeSessionId) {
    thread.innerHTML = `<div class="empty-state"><span>💬</span><h3>No chat selected</h3><p>Create or open a chat session to start talking with Ultron.</p></div>`;
    return;
  }
  thread.innerHTML = chatState.messages.map(chatMessageHtml).join('');
  thread.scrollTop = thread.scrollHeight;
}

function chatMessageHtml(message) {
  if (message.role === 'tool') {
    return `<div class="chat-tool-indicator">🔧 ${esc(message.tool_name || 'tool')}</div>`;
  }
  const cls = message.role === 'user' ? 'user' : 'assistant';
  const pending = message.pending ? ' pending' : '';
  return `<div class="chat-bubble ${cls}${pending}">${esc(message.content)}${message.pending ? '<span class="thinking-dots"><i></i><i></i><i></i></span>' : ''}</div>`;
}

function removePendingChatMessage() {
  chatState.messages = chatState.messages.filter(message => !message.pending);
}

async function createChatSession() {
  const projectId = chatSelectedProjectId();
  const title = `Chat ${new Date().toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`;
  const path = projectId ? `/projects/${projectId}/chat/sessions` : '/chat/sessions';
  const session = await api(path, { method: 'POST', body: JSON.stringify({ title }) });
  await loadChatSessions();
  await openChatSession(session.id);
  return session;
}

async function sendChatMessage(text) {
  if (!chatState.activeSessionId || chatState.sending) return;
  chatState.sending = true;
  $('chatSend').disabled = true;
  $('chatSend').textContent = 'Thinking…';
  chatState.messages.push({ role: 'user', content: text });
  chatState.messages.push({ role: 'assistant', content: 'Ultron is thinking', pending: true });
  renderChatThread();

  try {
    const response = await fetch(`/chat/sessions/${chatState.activeSessionId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: text }),
    });
    if (!response.ok || !response.body) throw new Error('Chat request failed');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replaceAll('\r\n', '\n');
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        handleChatSseBlock(block);
      }
    }
  } catch (err) {
    removePendingChatMessage();
    chatState.messages.push({ role: 'assistant', content: `I couldn't complete that response: ${err.message}` });
    renderChatThread();
    toast(err.message);
  } finally {
    chatState.sending = false;
    $('chatSend').disabled = false;
    $('chatSend').textContent = 'Send';
  }
}

function handleChatSseBlock(block) {
  const lines = block.split('\n');
  const eventLine = lines.find(l => l.startsWith('event: '));
  const dataLine = lines.find(l => l.startsWith('data: '));
  if (!eventLine || !dataLine) return;
  const eventName = eventLine.slice('event: '.length).trim();
  const data = JSON.parse(dataLine.slice('data: '.length));

  if (eventName === 'chat-tool') {
    removePendingChatMessage();
    chatState.messages.push({ role: 'tool', tool_name: data.tool_name, content: data.content });
    chatState.messages.push({ role: 'assistant', content: 'Ultron is thinking', pending: true });
    renderChatThread();
  } else if (eventName === 'chat-message') {
    removePendingChatMessage();
    chatState.messages.push({ role: 'assistant', content: data.content });
    renderChatThread();
  } else if (eventName === 'chat-error') {
    removePendingChatMessage();
    chatState.messages.push({ role: 'assistant', content: `I couldn't complete that response: ${data.error || 'Chat error'}` });
    renderChatThread();
    toast(data.error || 'Chat error');
  } else if (eventName === 'chat-done') {
    removePendingChatMessage();
    renderChatThread();
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('chatNewSession').onclick = createChatSession;
  $('chatProject').onchange = () => { updateChatCapabilityStatus(); loadChatSessions(); };
  $('chatShowArchived').onchange = loadChatSessions;
  $('chatForm').onsubmit = e => {
    e.preventDefault();
    const input = $('chatInput');
    const text = input.value.trim();
    if (!text) return;
    (async () => {
      if (!chatState.activeSessionId && !await createChatSession()) return;
      input.value = '';
      await sendChatMessage(text);
    })().catch(err => toast(err.message));
  };
  $('chatModelSwitcher').onchange = async e => {
    await api('/api/models/active', { method: 'PUT', body: JSON.stringify({ model: e.target.value }) });
    await loadAll();
    toast(`${e.target.value} is now active for chat and missions`);
  };

  // chat.js loads before app.js, so `loadAll` (a hoisted function declaration in
  // app.js) does not exist yet at chat.js's own top-level execution time — wrapping
  // it there throws "loadAll is not defined". By DOMContentLoaded, app.js has
  // already run in full (both scripts execute synchronously during HTML parsing,
  // which completes before DOMContentLoaded fires), so `loadAll` is safe to wrap here.
  //
  // Note: app.js's own bottom-of-file `loadAll().catch(...)` call has already
  // started (and is still awaiting its fetches) by the time this handler runs, so
  // reassigning `loadAll` here does not affect that already in-flight invocation —
  // it only takes effect for later calls (Refresh button, model switch, etc). To
  // populate the chat selector on first paint we wait for `state.projects` to be
  // filled by that in-flight call instead of depending on interception.
  const chatOriginalLoadAll = loadAll;
  loadAll = async function patchedLoadAll() {
    await chatOriginalLoadAll();
    await loadChatProjects();
    await loadChatSessions();
    await loadChatModels();
  };

  chatWaitForInitialProjects().then(() => {
    loadChatProjects().then(loadChatSessions).then(loadChatModels);
  });
});

function chatWaitForInitialProjects(maxAttempts = 40, delayMs = 100) {
  return new Promise(resolve => {
    let attempts = 0;
    const check = () => {
      if (state.projects.length || ++attempts >= maxAttempts) { resolve(); return; }
      setTimeout(check, delayMs);
    };
    check();
  });
}
