import type { Session, SessionInfo, HistoryMessage, SSEEvent } from './types'

function encode(name: string): string {
  return encodeURIComponent(name)
}

export async function getSessions(): Promise<Session[]> {
  const res = await fetch('/api/sessions')
  return res.json()
}

export async function createSession(name: string, workingDir: string): Promise<void> {
  const res = await fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, working_dir: workingDir }),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.detail || 'Failed to create session')
  }
}

export async function deleteSession(name: string): Promise<void> {
  await fetch(`/api/sessions/${encode(name)}`, { method: 'DELETE' })
}

export async function sendMessageSteer(name: string, message: string): Promise<void> {
  await fetch(`/api/sessions/${encode(name)}/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  })
}

export async function cancelSession(name: string): Promise<void> {
  await fetch(`/api/sessions/${encode(name)}/cancel`, { method: 'POST' })
}

export async function setMode(name: string, mode: 'plan' | 'build'): Promise<void> {
  await fetch(`/api/sessions/${encode(name)}/mode`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  })
}

export async function addDir(name: string, path: string): Promise<string[]> {
  const res = await fetch(`/api/sessions/${encode(name)}/dirs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  const data = await res.json()
  return data.additional_dirs ?? []
}

export async function removeDir(name: string, path: string): Promise<string[]> {
  const res = await fetch(`/api/sessions/${encode(name)}/dirs`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  const data = await res.json()
  return data.additional_dirs ?? []
}

export async function getSessionInfo(name: string): Promise<SessionInfo> {
  const res = await fetch(`/api/sessions/${encode(name)}/info`)
  if (!res.ok) throw new Error('Session not found')
  return res.json()
}

export async function getHistory(name: string): Promise<HistoryMessage[]> {
  const res = await fetch(`/api/sessions/${encode(name)}/history`)
  if (!res.ok) throw new Error('Session not found')
  return res.json()
}

export async function* sendMessageStream(
  name: string,
  message: string,
): AsyncGenerator<SSEEvent> {
  const res = await fetch(`/api/sessions/${encode(name)}/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  })

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
      if (!line.startsWith('data: ')) continue
      try {
        yield JSON.parse(line.slice(6)) as SSEEvent
      } catch {
        // skip malformed lines
      }
    }
  }

  if (buffer.startsWith('data: ')) {
    try {
      yield JSON.parse(buffer.slice(6)) as SSEEvent
    } catch {
      // ignore incomplete buffer
    }
  }
}
