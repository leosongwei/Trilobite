import { sendMessage } from './chat.js';
import { loadSessions, createSession, selectSession, deleteSession } from './session.js';

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

document.getElementById('sessionList').addEventListener('click', (event) => {
    const sessionEl = event.target.closest('[data-session]');
    if (sessionEl) selectSession(sessionEl.dataset.session);
    const deleteEl = event.target.closest('[data-delete]');
    if (deleteEl) {
        event.stopPropagation();
        deleteSession(deleteEl.dataset.delete);
    }
});

loadSessions();
