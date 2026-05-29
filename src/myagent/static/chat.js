function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

let currentSession = null;
let isStreaming = false;

export function setSession(name) { currentSession = name; }
export function getSession() { return currentSession; }
export function setStreaming(v) {
    isStreaming = v;
    const btn = document.getElementById('stopBtn');
    if (btn) btn.disabled = !v;
}
export function getStreaming() { return isStreaming; }

export async function stopAgent() {
    if (!currentSession) return;
    await fetch(`/api/sessions/${currentSession}/cancel`, { method: 'POST' });
}

function createTurnBlock() {
    const block = document.createElement('div');
    block.className = 'turn-block';
    return block;
}

function createThinkingBlock(content) {
    const details = document.createElement('details');
    details.className = 'thinking';
    details.open = true;
    details.innerHTML = `<summary>thinking...</summary><span>${escapeHtml(content)}</span>`;
    return details;
}

function createTextBlock(content) {
    const div = document.createElement('div');
    div.className = 'message assistant';
    div.textContent = content;
    return div;
}

function createToolBlock(name, result) {
    const div = document.createElement('div');
    div.className = 'tool-entry';
    div.innerHTML = `<div class="tool-action">[${name}]</div><pre class="tool-result">${escapeHtml(result || '...')}</pre>`;
    return div;
}

export async function loadHistory() {
    if (!currentSession) return;
    const res = await fetch(`/api/sessions/${currentSession}/history`);
    const history = await res.json();
    const chat = document.getElementById('chat');
    chat.innerHTML = '';

    let i = 0;
    while (i < history.length) {
        const msg = history[i];

        if (msg.role === 'user') {
            const div = document.createElement('div');
            div.className = 'message user';
            div.textContent = msg.content;
            chat.appendChild(div);
            i++;
            continue;
        }

        if (msg.role === 'assistant' && msg.tool_calls) {
            const block = createTurnBlock();

            if (msg.reasoning_content) {
                const thinking = createThinkingBlock(msg.reasoning_content);
                thinking.open = false;
                block.appendChild(thinking);
            }

            if (msg.content) {
                block.appendChild(createTextBlock(msg.content));
            }

            for (const tc of msg.tool_calls) {
                const toolDiv = document.createElement('div');
                toolDiv.className = 'tool-entry';
                toolDiv.innerHTML = `<div class="tool-action">[${tc.function.name}]</div>`;
                block.appendChild(toolDiv);
            }

            // Consume subsequent tool messages
            i++;
            let toolIdx = 0;
            while (i < history.length && history[i].role === 'tool') {
                const toolMsg = history[i];
                const entries = block.querySelectorAll('.tool-entry');
                const entry = entries[toolIdx];
                if (entry) {
                    const pre = entry.querySelector('.tool-result');
                    if (pre) {
                        pre.textContent = toolMsg.content;
                    } else {
                        entry.innerHTML += `<pre class="tool-result">${escapeHtml(toolMsg.content)}</pre>`;
                    }
                }
                toolIdx++;
                i++;
            }

            chat.appendChild(block);
            continue;
        }

        if (msg.role === 'assistant') {
            const block = createTurnBlock();
            if (msg.reasoning_content) {
                const thinking = createThinkingBlock(msg.reasoning_content);
                thinking.open = false;
                block.appendChild(thinking);
            }
            if (msg.content) {
                block.appendChild(createTextBlock(msg.content));
            }
            chat.appendChild(block);
            i++;
            continue;
        }

        i++;
    }

    chat.scrollTop = chat.scrollHeight;
}

export async function sendMessage() {
    if (!currentSession || isStreaming) return;
    const input = document.getElementById('messageInput');
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    input.style.height = 'auto';
    setStreaming(true);

    const chat = document.getElementById('chat');
    const userDiv = document.createElement('div');
    userDiv.className = 'message user';
    userDiv.textContent = message;
    chat.appendChild(userDiv);

    const sendBtn = document.getElementById('sendBtn');
    sendBtn.disabled = true;

    let currentBlock = null;
    let thinkingEl = null;
    let thinkingContent = '';
    let textEl = null;
    let textContent = '';
    let pendingTools = [];
    let toolStreamEl = null;
    let toolStreamName = '';

    function closeTurn() {
        if (thinkingEl && thinkingContent) {
            thinkingEl.querySelector('span').textContent = thinkingContent;
            thinkingEl.open = false;
        }
        if (textEl) textEl.removeAttribute('id');
        currentBlock = null;
        thinkingEl = null;
        thinkingContent = '';
        textEl = null;
        textContent = '';
        pendingTools = [];
        toolStreamEl = null;
        toolStreamName = '';
    }

    function newTurn() {
        closeTurn();
        currentBlock = createTurnBlock();
        chat.appendChild(currentBlock);
    }

    try {
        const res = await fetch(`/api/sessions/${currentSession}/message`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message }),
        });

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const text = buffer + decoder.decode(value, { stream: true });
            const lines = text.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const data = JSON.parse(line.slice(6));

                switch (data.type) {
                    case 'turn':
                        newTurn();
                        break;

                    case 'thinking':
                        if (!thinkingEl) {
                            thinkingEl = createThinkingBlock('');
                            currentBlock.appendChild(thinkingEl);
                        }
                        thinkingContent += data.text;
                        thinkingEl.querySelector('span').textContent = thinkingContent;
                        break;

                    case 'text':
                        if (!textEl) {
                            textEl = createTextBlock('');
                            textEl.id = 'streaming-text';
                            currentBlock.appendChild(textEl);
                        }
                        textContent += data.text;
                        textEl.textContent = textContent;
                        break;

                    case 'tool_stream':
                        if (!toolStreamEl || toolStreamName !== data.tool_name) {
                            toolStreamEl = document.createElement('div');
                            toolStreamEl.className = 'tool-entry';
                            toolStreamEl.innerHTML = `<div class="tool-action">[${data.tool_name}]</div><pre class="tool-result"></pre>`;
                            toolStreamEl.dataset.tool = data.tool_name;
                            currentBlock.appendChild(toolStreamEl);
                            toolStreamName = data.tool_name;
                        }
                        if (data.args) {
                            toolStreamEl.querySelector('.tool-result').textContent = data.args;
                        }
                        if (data.complete) {
                            toolStreamName = '';
                        }
                        break;

                    case 'tool_start':
                        const existing = currentBlock.querySelector(`.tool-entry[data-tool="${data.tool}"]`);
                        if (existing) {
                            existing.querySelector('.tool-action').textContent = `[${data.tool}] running...`;
                            pendingTools.push(existing);
                        } else {
                            const toolEntry = createToolBlock(data.tool, 'running...');
                            toolEntry.dataset.tool = data.tool;
                            currentBlock.appendChild(toolEntry);
                            pendingTools.push(toolEntry);
                        }
                        break;

                    case 'tool_result':
                        const match = pendingTools.find(t => t.dataset.tool === data.tool);
                        if (match) {
                            match.querySelector('.tool-action').textContent = `[${data.tool}]`;
                            match.querySelector('.tool-result').textContent = data.result;
                        }
                        break;

                    case 'done':
                        closeTurn();
                        break;

                    case 'cancelled':
                        closeTurn();
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
        // Process any remaining partial line from buffer
        if (buffer && buffer.startsWith('data: ')) {
            try {
                const data = JSON.parse(buffer.slice(6));
                // Only process final events (done/error)
                if (data.type === 'done') closeTurn();
                else if (data.type === 'error') {
                    const errDiv = document.createElement('div');
                    errDiv.className = 'message error';
                    errDiv.textContent = `Error: ${data.text}`;
                    document.getElementById('chat').appendChild(errDiv);
                }
            } catch {}
        }
    } catch (e) {
        const errDiv = document.createElement('div');
        errDiv.className = 'message error';
        errDiv.textContent = `Connection error: ${e.message}`;
        chat.appendChild(errDiv);
    }

    closeTurn();
    chat.scrollTop = chat.scrollHeight;
    setStreaming(false);
    sendBtn.disabled = false;
}
