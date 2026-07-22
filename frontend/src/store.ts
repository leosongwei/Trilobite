import { reactive } from 'vue'
import type { Session, ChatItem, ToolDisplay, HistoryMessage, SSEEvent, TurnItem } from './types'
import * as api from './api'

interface State {
  sessions: Session[]
  currentSession: string | null
  chatItems: ChatItem[]
  isStreaming: boolean
  tokenCount: number
  maxTokens: number
  statusText: string | null
  streamTick: number
  planMode: boolean
  additionalDirs: string[]
  planExitRequest: boolean
  permissionRequest: { path: string; tool: string; message: string } | null
}

const state = reactive<State>({
  sessions: [],
  currentSession: null,
  chatItems: [],
  isStreaming: false,
  tokenCount: 0,
  maxTokens: 0,
  statusText: null,
  streamTick: 0,
  planMode: false,
  additionalDirs: [],
  planExitRequest: false,
  permissionRequest: null,
})

let currentTurnIdx = -1
let currentToolIdx = -1

function getCurrentTurn(): TurnItem | null {
  if (currentTurnIdx < 0) return null
  const item = state.chatItems[currentTurnIdx]
  if (item && item.kind === 'turn') return item
  return null
}

function getCurrentTool(): ToolDisplay | null {
  const turn = getCurrentTurn()
  if (!turn) return null
  if (currentToolIdx < 0) return null
  return turn.tools[currentToolIdx] ?? null
}

function newTurn() {
  closeTurn()
  const turn: TurnItem = { kind: 'turn', thinking: '', text: '', tools: [] }
  state.chatItems.push(turn)
  currentTurnIdx = state.chatItems.length - 1
}

function closeTurn() {
  currentTurnIdx = -1
  currentToolIdx = -1
}

function handleSSEEvent(event: SSEEvent) {
  switch (event.type) {
    case 'init': {
      state.chatItems = parseHistory(event.history)
      state.isStreaming = event.is_running
      state.tokenCount = event.token_count
      state.maxTokens = event.max_context_tokens
      state.planMode = event.plan_mode
      state.additionalDirs = event.additional_dirs ?? []
      closeTurn()
      break
    }

    case 'user': {
      state.chatItems.push({ kind: 'user', content: event.text })
      break
    }

    case 'turn':
      state.isStreaming = true
      newTurn()
      break

    case 'thinking': {
      const turn = getCurrentTurn()
      if (turn) turn.thinking += event.text
      break
    }

    case 'text': {
      const turn = getCurrentTurn()
      if (turn) turn.text += event.text
      break
    }

    case 'tool_stream': {
      const turn = getCurrentTurn()
      if (!turn) break
      const tool = getCurrentTool()
      if (!tool || tool.name !== event.tool_name) {
        const newTool: ToolDisplay = { name: event.tool_name, status: 'streaming', args: '' }
        turn.tools.push(newTool)
        currentToolIdx = turn.tools.length - 1
      }
      const current = getCurrentTool()
      if (current && event.args) {
        current.args = event.args
      }
      if (event.complete) {
        currentToolIdx = -1
      }
      break
    }

    case 'tool_start': {
      const turn = getCurrentTurn()
      if (!turn) break
      const streaming = turn.tools.find(
        (t) => t.name === event.tool && t.status === 'streaming',
      )
      if (streaming) {
        streaming.status = 'running'
        streaming.startArgs = event.args
      } else {
        turn.tools.push({ name: event.tool, status: 'running', args: '', startArgs: event.args })
      }
      break
    }

    case 'tool_result': {
      const turn = getCurrentTurn()
      if (!turn) break
      const running = turn.tools.find(
        (t) => t.name === event.tool && t.status === 'running',
      )
      if (running) {
        running.status = 'done'
        running.result = event.result
        if (event.diff_prev) {
          running.diffPrev = event.diff_prev
          running.diffCurrent = event.diff_current
        }
      }
      break
    }

    case 'usage':
      state.tokenCount = event.token_count
      state.maxTokens = event.max_context_tokens
      break

    case 'status':
      state.statusText = event.text
      break

    case 'plan_exit_request':
      state.planExitRequest = true
      break

    case 'permission_request':
      state.permissionRequest = {
        path: event.path,
        tool: event.tool,
        message: event.message,
      }
      break

    case 'done':
    case 'cancelled':
      state.isStreaming = false
      closeTurn()
      break

    case 'error': {
      state.isStreaming = false
      let content = ''
      if (event.status_code) {
        content += `[HTTP ${event.status_code}] `
      }
      if (event.error_type) {
        content += `[${event.error_type}] `
      }
      content += event.text
      state.chatItems.push({ kind: 'error', content })
      closeTurn()
      break
    }
  }

  state.streamTick++
}

function parseHistory(history: HistoryMessage[]): ChatItem[] {
  const items: ChatItem[] = []
  let i = 0

  while (i < history.length) {
    const msg = history[i]

    if (msg.role === 'user') {
      items.push({ kind: 'user', content: msg.content || '' })
      i++
      continue
    }

    if (msg.role === 'assistant') {
      const turn: TurnItem = {
        kind: 'turn',
        thinking: msg.reasoning_content || '',
        text: msg.content || '',
        tools: [],
      }

      if (msg.tool_calls && msg.tool_calls.length > 0) {
        for (const tc of msg.tool_calls) {
          let startArgs: Record<string, unknown> | undefined
          try {
            startArgs = JSON.parse(tc.function.arguments)
          } catch {}
          turn.tools.push({
            name: tc.function.name,
            status: 'done',
            args: tc.function.arguments,
            startArgs,
          })
        }

        i++
        let toolIdx = 0
        while (i < history.length && history[i].role === 'tool') {
          if (toolIdx < turn.tools.length) {
            turn.tools[toolIdx].result = history[i].content || ''
            if ((history[i] as any).diff_prev) {
              turn.tools[toolIdx].diffPrev = (history[i] as any).diff_prev
              turn.tools[toolIdx].diffCurrent = (history[i] as any).diff_current
            }
          }
          toolIdx++
          i++
        }

        items.push(turn)
        continue
      }

      items.push(turn)
      i++
      continue
    }

    i++
  }

  return items
}

// ── stream subscription ────────────────────────────────────────────────────
// One SSE subscription tracks the current session. Switching sessions aborts
// the old stream and opens a new one; the init event plus the replayed buffer
// reconstruct state, so closing/reopening a tab or opening a second window
// always shows the live (or in-progress) output.
let streamAbort: AbortController | null = null
let streamSession: string | null = null

async function connectStream(name: string) {
  if (streamAbort) streamAbort.abort()
  streamSession = name
  void runStream(name)
}

function disconnectStream() {
  streamSession = null
  if (streamAbort) streamAbort.abort()
  streamAbort = null
}

async function runStream(name: string) {
  while (streamSession === name) {
    const ac = new AbortController()
    streamAbort = ac
    try {
      const stream = api.subscribeStream(name, ac.signal)
      for await (const event of stream) {
        if (streamSession !== name) break
        handleSSEEvent(event)
      }
    } catch (e) {
      // Aborted (session switch / disconnect) - stop the loop.
      if (ac.signal.aborted || streamSession !== name) return
      // Network error - retry after a short backoff.
    }
    if (streamSession !== name) return
    await new Promise((r) => setTimeout(r, 1000))
  }
}

// ── session list polling ────────────────────────────────────────────────────
// The SSE stream is per-session, so it carries nothing about *other* sessions.
// The sidebar lists every session, so it is refreshed on a short poll: cheap
// (the endpoint just reads session.json files) and enough to make new/deleted
// sessions and running state appear across multiple browsers.
let sessionPollTimer: ReturnType<typeof setInterval> | null = null

function ensureSessionPolling() {
  if (sessionPollTimer !== null) return
  sessionPollTimer = setInterval(async () => {
    try {
      state.sessions = await api.getSessions()
    } catch {
      // transient error; the next tick retries
    }
  }, 3000)
}

export function useStore() {
  async function loadSessions() {
    state.sessions = await api.getSessions()
    ensureSessionPolling()
  }

  async function selectSession(name: string) {
    state.currentSession = name
    closeTurn()
    state.chatItems = []
    state.statusText = null
    state.isStreaming = false
    await loadSessions()
    connectStream(name)
  }

  async function createSession(name: string, workingDir: string) {
    const actualName = await api.createSession(name, workingDir)
    state.currentSession = actualName
    closeTurn()
    state.chatItems = []
    state.tokenCount = 0
    state.statusText = null
    state.planMode = false
    state.additionalDirs = []
    state.isStreaming = false
    await loadSessions()
    connectStream(actualName)
  }

  async function deleteSession(name: string) {
    await api.deleteSession(name)
    if (state.currentSession === name) {
      disconnectStream()
      state.currentSession = null
      state.chatItems = []
      state.tokenCount = 0
      state.maxTokens = 0
      state.statusText = null
      state.planMode = false
      state.additionalDirs = []
      state.isStreaming = false
      closeTurn()
    }
    await loadSessions()
  }

  async function sendMessage(message: string) {
    if (!state.currentSession) return
    // The user message is rendered from the "user" stream event (emitted by
    // the agent on start/steer), not pushed here, so reconnects stay
    // consistent with server-side history.
    try {
      await api.sendMessage(state.currentSession, message)
    } catch (e) {
      state.chatItems.push({
        kind: 'error',
        content: `Failed to send: ${e instanceof Error ? e.message : String(e)}`,
      })
      state.streamTick++
    }
  }

  async function stopAgent() {
    if (!state.currentSession) return
    await api.cancelSession(state.currentSession)
  }

  async function setMode(mode: 'plan' | 'build') {
    if (!state.currentSession) return
    await api.setMode(state.currentSession, mode)
    state.planMode = mode === 'plan'
  }

  async function addDir(path: string) {
    if (!state.currentSession) return
    state.additionalDirs = await api.addDir(state.currentSession, path)
  }

  async function removeDir(path: string) {
    if (!state.currentSession) return
    state.additionalDirs = await api.removeDir(state.currentSession, path)
  }

  async function approvePlanExit() {
    if (!state.currentSession) return
    state.planExitRequest = false
    state.planMode = false
    await api.planExit(state.currentSession, true)
  }

  async function rejectPlanExit() {
    if (!state.currentSession) return
    state.planExitRequest = false
    await api.planExit(state.currentSession, false)
  }

  async function approvePermission() {
    if (!state.currentSession || !state.permissionRequest) return
    const permPath = state.permissionRequest.path
    state.permissionRequest = null
    // Add the directory to additional_dirs
    if (!state.additionalDirs.includes(permPath)) {
      state.additionalDirs.push(permPath)
      await api.addDir(state.currentSession, permPath)
    }
    await api.resolvePermission(state.currentSession, true)
  }

  async function rejectPermission() {
    if (!state.currentSession || !state.permissionRequest) return
    state.permissionRequest = null
    await api.resolvePermission(state.currentSession, false)
  }

  return {
    state,
    loadSessions,
    selectSession,
    createSession,
    deleteSession,
    sendMessage,
    stopAgent,
    setMode,
    addDir,
    removeDir,
    approvePlanExit,
    rejectPlanExit,
    approvePermission,
    rejectPermission,
  }
}
