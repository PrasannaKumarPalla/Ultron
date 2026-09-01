const bujjiState = {
  messages: [],
  sending: false,
};

function renderBujjiThread() {
  const thread = $('bujjiThread');
  if (!bujjiState.messages.length) {
    thread.innerHTML = `<div class="empty-state"><span>◈</span><h3>Say hello to Bujji</h3><p>Your local assistant module, running on the same Ollama runtime as Ultron.</p></div>`;
    return;
  }
  thread.innerHTML = bujjiState.messages.map(bujjiMessageHtml).join('');
  thread.scrollTop = thread.scrollHeight;
}

function bujjiMessageHtml(message) {
  const cls = message.role === 'user' ? 'user' : 'assistant';
  const pending = message.pending ? ' pending' : '';
  return `<div class="chat-bubble ${cls}${pending}">${esc(message.content)}${message.pending ? '<span class="thinking-dots"><i></i><i></i><i></i></span>' : ''}</div>`;
}

function removePendingBujjiMessage() {
  bujjiState.messages = bujjiState.messages.filter(message => !message.pending);
}

async function loadBujjiStatus() {
  try {
    const status = await api('/api/bujji/status');
    if (!status.available) throw new Error(status.detail || 'unavailable');
    $('bujjiStatus').textContent = `Bujji ${status.version || ''} · engine: ${status.engines[0] || 'ollama'}`;
    $('bujjiModelSwitcher').innerHTML = `<option value="">Auto — Bujji default (${esc(status.models[0] || 'default')})</option>`
      + status.models.map(model => `<option value="${esc(model)}">${esc(model)}</option>`).join('');
  } catch (err) {
    $('bujjiStatus').textContent = 'Bujji unavailable — check Ollama runtime';
    $('bujjiModelSwitcher').innerHTML = '<option value="">Auto</option>';
  }
}

async function sendBujjiMessage(text) {
  if (bujjiState.sending) return;
  bujjiState.sending = true;
  $('bujjiSend').disabled = true;
  $('bujjiSend').textContent = 'Thinking…';
  bujjiState.messages.push({ role: 'user', content: text });
  const pendingIndex = bujjiState.messages.push({ role: 'assistant', content: '', pending: true }) - 1;
  renderBujjiThread();

  let streamed = '';
  try {
    const response = await fetch('/api/bujji/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: text, model: $('bujjiModelSwitcher').value || null }),
    });
    if (!response.ok || !response.body) {
      let detail = 'Bujji request failed';
      try { detail = (await response.json()).detail || detail; } catch {}
      throw new Error(detail);
    }

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
        const result = handleBujjiSseBlock(block, pendingIndex, streamed);
        if (result !== undefined) streamed = result;
      }
    }
  } catch (err) {
    removePendingBujjiMessage();
    bujjiState.messages.push({ role: 'assistant', content: `I couldn't complete that response: ${err.message}` });
    toast(err.message);
  } finally {
    removePendingBujjiMessage();
    renderBujjiThread();
    bujjiState.sending = false;
    $('bujjiSend').disabled = false;
    $('bujjiSend').textContent = 'Send';
  }
}

function handleBujjiSseBlock(block, pendingIndex, streamed) {
  const lines = block.split('\n');
  const eventLine = lines.find(l => l.startsWith('event: '));
  const dataLine = lines.find(l => l.startsWith('data: '));
  if (!eventLine || !dataLine) return undefined;
  const eventName = eventLine.slice('event: '.length).trim();
  const data = JSON.parse(dataLine.slice('data: '.length));

  if (eventName === 'bujji-token') {
    streamed += data.token;
    bujjiState.messages[pendingIndex] = { role: 'assistant', content: streamed, pending: true };
    renderBujjiThread();
    return streamed;
  }
  if (eventName === 'bujji-error') {
    removePendingBujjiMessage();
    bujjiState.messages.push({ role: 'assistant', content: `I couldn't complete that response: ${data.error || 'Bujji error'}` });
    toast(data.error || 'Bujji error');
  }
  return undefined;
}

document.addEventListener('DOMContentLoaded', () => {
  $('bujjiForm').onsubmit = e => {
    e.preventDefault();
    const input = $('bujjiInput');
    const text = input.value.trim();
    if (!text || bujjiState.sending) return;
    input.value = '';
    sendBujjiMessage(text);
  };
  loadBujjiStatus();
});
