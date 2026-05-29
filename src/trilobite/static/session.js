import { loadHistory, setSession, getSession } from './chat.js';

export async function loadSessions() {
    const res = await fetch('/api/sessions');
    const sessions = await res.json();
    const list = document.getElementById('sessionList');
    const current = getSession();
    list.innerHTML = sessions.map(s =>
        `<div class="session-item ${s.name === current ? 'active' : ''}" data-session="${s.name}">
            <span>${s.name}</span>
            <span class="delete" data-delete="${s.name}">&times;</span>
        </div>`
    ).join('');
}

export async function createSession() {
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
    setSession(name);
    await loadSessions();
    await loadHistory();
}

export async function selectSession(name) {
    setSession(name);
    await loadSessions();
    await loadHistory();
}

export async function deleteSession(name) {
    if (!confirm(`Delete session "${name}"?`)) return;
    await fetch(`/api/sessions/${name}`, { method: 'DELETE' });
    if (getSession() === name) {
        setSession(null);
        document.getElementById('chat').innerHTML = '<div class="empty-state">Select or create a session</div>';
    }
    await loadSessions();
}
