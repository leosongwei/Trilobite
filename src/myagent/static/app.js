let currentSession = null;
let isStreaming = false;

async function loadSessions() {
    const res = await fetch('/api/sessions');
    const sessions = await res.json();
    const list = document.getElementById('sessionList');
    list.innerHTML = sessions.map(s =>
        `<div class="session-item ${s.name === currentSession ? 'active' : ''}" data-session="${s.name}">
            <span>${s.name}</span>
            <span class="delete" data-delete="${s.name}">&times;</span>
        </div>`
    ).join('');
}

async function createSession() {
    const name = document.getElementById('sessionName').value.trim();
    const workingDir = document.getElementById('workingDir').value.trim();
    if (!name || !workingDir) return alert('Please fill in both fields');

    const res = await fetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, working_dir: workingDir }),
    });
    if (res.status === 400) {
        const err = await res.json();
        return alert(err.detail);
    }
    currentSession = name;
    await loadSessions();
    await loadHistory();
}

async function selectSession(name) {
    currentSession = name;
    await loadSessions();
    await loadHistory();
}

async function deleteSession(name) {
    if (!confirm(`Delete session "${name}"?`)) return;
    await fetch(`/api/sessions/${name}`, { method: 'DELETE' });
    if (currentSession === name) {
        currentSession = null;
        document.getElementById('chat').innerHTML = '<div class="empty-state">Select or create a session</div>';
    }
    await loadSessions();
}

async function loadHistory() {
    if (!currentSession) return;
    const res = await fetch(`/api/sessions/${currentSession}/history`);
    const history = await res.json();
    const chat = document.getElementById('chat');
    chat.innerHTML = '';
    for (const msg of history) {
        renderMessage(msg);
    }
    chat.scrollTop = chat.scrollHeight;
}

function renderMessage(msg) {
    const chat = document.getElementById('chat');
    let content = msg.content || '';
    const role = msg.role;

    if (role === 'tool') {
        const div = document.createElement('div');
        div.className = 'message tool';
        div.textContent = `[tool result]\n${content.slice(0, 2000)}`;
        chat.appendChild(div);
        return;
    }

    const div = document.createElement('div');
    div.className = `message ${role}`;

    if (msg.tool_calls) {
        let prefix = content ? content + '\n\n' : '';
        for (const tc of msg.tool_calls) {
            prefix += `[calling: ${tc.function.name}]\n`;
        }
        div.textContent = prefix;
    } else {
        div.textContent = content;
    }

    chat.appendChild(div);
}

function createStreamingMessage(role) {
    const chat = document.getElementById('chat');
    const div = document.createElement('div');
    div.className = `message ${role}`;
    div.id = 'streaming-msg';
    chat.appendChild(div);
    return div;
}

async function sendMessage() {
    if (!currentSession || isStreaming) return;
    const input = document.getElementById('messageInput');
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    input.style.height = 'auto';
    isStreaming = true;

    const chat = document.getElementById('chat');
    const userDiv = document.createElement('div');
    userDiv.className = 'message user';
    userDiv.textContent = message;
    chat.appendChild(userDiv);

    const streamingDiv = createStreamingMessage('assistant');
    let streamingContent = '';

    const sendBtn = document.getElementById('sendBtn');
    sendBtn.disabled = true;

    try {
        const res = await fetch(`/api/sessions/${currentSession}/message`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message }),
        });

        const reader = res.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const text = decoder.decode(value, { stream: true });
            const lines = text.split('\n');

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const data = JSON.parse(line.slice(6));

                switch (data.type) {
                    case 'text':
                        streamingContent += data.text;
                        streamingDiv.textContent = streamingContent;
                        break;
                    case 'tool_start':
                        const toolDiv = document.createElement('div');
                        toolDiv.className = 'message tool';
                        toolDiv.textContent = `[running: ${data.tool}...]`;
                        toolDiv.id = `tool-${data.tool}`;
                        chat.appendChild(toolDiv);
                        break;
                    case 'tool_result':
                        const td = document.getElementById(`tool-${data.tool}`);
                        if (td) {
                            td.textContent = `[${data.tool} result]\n${data.result.slice(0, 2000)}`;
                        }
                        break;
                    case 'done':
                        streamingDiv.removeAttribute('id');
                        break;
                    case 'error':
                        const errDiv = document.createElement('div');
                        errDiv.className = 'message error';
                        errDiv.textContent = `Error: ${data.text}`;
                        chat.appendChild(errDiv);
                        break;
                }
            }
        }
    } catch (e) {
        const errDiv = document.createElement('div');
        errDiv.className = 'message error';
        errDiv.textContent = `Connection error: ${e.message}`;
        chat.appendChild(errDiv);
    }

    streamingDiv.removeAttribute('id');
    chat.scrollTop = chat.scrollHeight;
    isStreaming = false;
    sendBtn.disabled = false;
}

// Event listeners
document.getElementById('createSessionBtn').addEventListener('click', createSession);
document.getElementById('sendBtn').addEventListener('click', sendMessage);
document.getElementById('messageInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
});
document.getElementById('messageInput').addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

// Delegate click for session list (select/delete)
document.getElementById('sessionList').addEventListener('click', (event) => {
    const sessionEl = event.target.closest('[data-session]');
    if (sessionEl) {
        selectSession(sessionEl.dataset.session);
    }
    const deleteEl = event.target.closest('[data-delete]');
    if (deleteEl) {
        event.stopPropagation();
        deleteSession(deleteEl.dataset.delete);
    }
});

loadSessions();
