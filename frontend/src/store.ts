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
    case 'turn':
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
      } else {
        turn.tools.push({ name: event.tool, status: 'running', args: '' })
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

    case 'done':
    case 'cancelled':
      closeTurn()
      break

    case 'error': {
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
          turn.tools.push({
            name: tc.function.name,
            status: 'done',
            args: tc.function.arguments,
          })
        }

        i++
        let toolIdx = 0
        while (i < history.length && history[i].role === 'tool') {
          if (toolIdx < turn.tools.length) {
            turn.tools[toolIdx].result = history[i].content || ''
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

export function useStore() {
  async function loadSessions() {
    state.sessions = await api.getSessions()
  }

  async function selectSession(name: string) {
    state.currentSession = name
    closeTurn()
    state.statusText = null
    await loadSessions()
    await loadHistory()
    const info = await api.getSessionInfo(name)
    state.planMode = info.plan_mode
    state.additionalDirs = info.additional_dirs ?? []
  }

  async function createSession(name: string, workingDir: string) {
    await api.createSession(name, workingDir)
    state.currentSession = name
    closeTurn()
    state.chatItems = []
    state.tokenCount = 0
    state.statusText = null
    state.planMode = false
    state.additionalDirs = []
    await loadSessions()
    const info = await api.getSessionInfo(name)
    state.maxTokens = info.max_context_tokens
  }

  async function deleteSession(name: string) {
    await api.deleteSession(name)
    if (state.currentSession === name) {
      state.currentSession = null
      state.chatItems = []
      state.tokenCount = 0
      state.maxTokens = 0
      state.statusText = null
      state.planMode = false
      state.additionalDirs = []
      closeTurn()
    }
    await loadSessions()
  }

  async function loadHistory() {
    if (!state.currentSession) return
    const history = await api.getHistory(state.currentSession)
    state.chatItems = parseHistory(history)

    const info = await api.getSessionInfo(state.currentSession)
    state.maxTokens = info.max_context_tokens
    state.tokenCount = info.token_count
    state.planMode = info.plan_mode
    state.additionalDirs = info.additional_dirs ?? []
  }

  async function sendMessage(message: string) {
    if (!state.currentSession) return

    state.chatItems.push({ kind: 'user', content: message })

    if (state.isStreaming) {
      await api.sendMessageSteer(state.currentSession, message)
      return
    }

    state.isStreaming = true
    state.statusText = null
    closeTurn()

    try {
      const stream = api.sendMessageStream(state.currentSession, message)
      for await (const event of stream) {
        handleSSEEvent(event)
      }
    } catch (e) {
      state.chatItems.push({
        kind: 'error',
        content: `Connection error: ${e instanceof Error ? e.message : String(e)}`,
      })
    }

    closeTurn()
    state.isStreaming = false
    state.statusText = null
    state.streamTick++
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

  return {
    state,
    loadSessions,
    selectSession,
    createSession,
    deleteSession,
    loadHistory,
    sendMessage,
    stopAgent,
    setMode,
    addDir,
    removeDir,
    approvePlanExit,
    rejectPlanExit,
  }
}
