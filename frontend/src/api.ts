import type { Session, SessionInfo, HistoryMessage, SSEEvent, DiffRow } from './types'

export interface ImageAttachment {
  mime_type: string
  data_url: string
  original_name: string
}

function encode(id: string): string {
  return encodeURIComponent(id)
}

// All API calls go through authFetch: when the server rejects with 401 (e.g.
// after a server restart invalidated the cookie), the app is told to show the
// access-key dialog again.
export function authFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return fetch(input, init).then((res) => {
    if (res.status === 401 && !String(input).startsWith('/api/auth/')) {
      window.dispatchEvent(new Event('trilobite:unauthorized'))
    }
    return res
  })
}

export async function getAuthStatus(): Promise<{ authenticated: boolean }> {
  const res = await authFetch('/api/auth/status')
  return res.json()
}

export async function login(key: string): Promise<void> {
  const res = await authFetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  })
  if (!res.ok) throw new Error('Invalid key')
}

export async function getCwd(): Promise<string> {
  const res = await authFetch('/api/cwd')
  const data = await res.json()
  return data.cwd
}

export async function getVersion(): Promise<string> {
  const res = await authFetch('/api/version')
  const data = await res.json()
  return data.version
}

export async function getSessions(): Promise<Session[]> {
  const res = await authFetch('/api/sessions')
  return res.json()
}

export async function createSession(name: string, workingDir: string): Promise<string> {
  const res = await authFetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, working_dir: workingDir }),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.detail || 'Failed to create session')
  }
  const data = await res.json()
  return data.id
}

export async function renameSession(id: string, name: string): Promise<void> {
  await authFetch(`/api/sessions/${encode(id)}/rename`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}

export async function deleteSession(id: string): Promise<void> {
  await authFetch(`/api/sessions/${encode(id)}`, { method: 'DELETE' })
}

export async function sendMessage(
  id: string,
  message: string,
  images: ImageAttachment[] = [],
): Promise<{ status: string }> {
  const res = await authFetch(`/api/sessions/${encode(id)}/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, images: images.length ? images : undefined }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || 'Failed to send message')
  }
  return res.json()
}

export async function revert(id: string, userSeq: number, message: string): Promise<{ status: string }> {
  const res = await authFetch(`/api/sessions/${encode(id)}/revert`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_seq: userSeq, message }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || 'Failed to revert')
  }
  return res.json()
}

export async function cancelSession(id: string): Promise<void> {
  await authFetch(`/api/sessions/${encode(id)}/cancel`, { method: 'POST' })
}

export async function interruptSession(id: string): Promise<void> {
  await authFetch(`/api/sessions/${encode(id)}/interrupt`, { method: 'POST' })
}

export async function setMode(id: string, mode: 'plan' | 'build'): Promise<void> {
  await authFetch(`/api/sessions/${encode(id)}/mode`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  })
}

export async function addDir(id: string, path: string): Promise<string[]> {
  const res = await authFetch(`/api/sessions/${encode(id)}/dirs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  const data = await res.json()
  return data.additional_dirs ?? []
}

export async function removeDir(id: string, path: string): Promise<string[]> {
  const res = await authFetch(`/api/sessions/${encode(id)}/dirs`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  const data = await res.json()
  return data.additional_dirs ?? []
}

export async function planExit(id: string, approved: boolean): Promise<void> {
  await authFetch(`/api/sessions/${encode(id)}/plan_exit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved }),
  })
}

export async function resolvePermission(id: string, approved: boolean): Promise<void> {
  await authFetch(`/api/sessions/${encode(id)}/permission`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved }),
  })
}

export async function getSessionInfo(id: string): Promise<SessionInfo> {
  const res = await authFetch(`/api/sessions/${encode(id)}/info`)
  if (!res.ok) throw new Error('Session not found')
  return res.json()
}

export async function getHistory(id: string): Promise<HistoryMessage[]> {
  const res = await authFetch(`/api/sessions/${encode(id)}/history`)
  if (!res.ok) throw new Error('Session not found')
  return res.json()
}

export async function* subscribeStream(
  id: string,
  signal: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const res = await authFetch(`/api/sessions/${encode(id)}/stream`, { signal })
  if (!res.ok) throw new Error('Stream connection failed')
  if (!res.body) throw new Error('No response body')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    const text = buffer + decoder.decode(value, { stream: true })
    const lines = text.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      // SSE comments (e.g. ": keepalive") don't start with "data: "
      if (!line.startsWith('data: ')) continue
      try {
        yield JSON.parse(line.slice(6)) as SSEEvent
      } catch {
        // skip malformed lines
      }
    }
  }
}

// ── file manager ────────────────────────────────────────────────────────────

export interface FsEntry {
  name: string
  is_dir: boolean
  size?: number
  mtime?: number
  status?: string
}

export interface FsListing {
  path: string
  name: string
  is_git_repo: boolean
  current_branch: string
  branches: string[]
  entries: FsEntry[]
  truncated: boolean
}

export interface FsDiff {
  rows: DiffRow[]
  base: string
  untracked: boolean
}

async function fsRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(url, init)
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || 'Request failed')
  }
  return res.json()
}

export function getFileList(id: string, path: string): Promise<FsListing> {
  return fsRequest(`/api/sessions/${encode(id)}/fs/list?path=${encodeURIComponent(path)}`)
}

export function getFileContent(id: string, path: string): Promise<{ path: string; content: string }> {
  return fsRequest(`/api/sessions/${encode(id)}/fs/file?path=${encodeURIComponent(path)}`)
}

export function getFileDiff(id: string, path: string, base: string): Promise<FsDiff> {
  return fsRequest(
    `/api/sessions/${encode(id)}/fs/diff?path=${encodeURIComponent(path)}&base=${encodeURIComponent(base)}`,
  )
}

export function saveFile(id: string, path: string, content: string): Promise<{ ok: boolean }> {
  return fsRequest(`/api/sessions/${encode(id)}/fs/file`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content }),
  })
}
