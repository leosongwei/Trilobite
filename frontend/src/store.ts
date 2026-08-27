import { computed, reactive } from 'vue'
import type { Session, ChatItem, ToolDisplay, SubagentChild, HistoryMessage, SSEEvent, TurnItem, PendingRequest, Project, ModelOption } from './types'
import * as api from './api'

interface State {
  sessions: Session[]
  projects: Project[]
  models: ModelOption[]
  currentSession: string | null
  chatItems: ChatItem[]
  isStreaming: boolean
  tokenCount: number
  maxTokens: number
  statusText: string | null
  streamTick: number
  // Bumped when a file-changing tool returns (edit/write/bash/task), or on
  // subagent state changes / run ends - the moments files can actually
  // change. The sidebar "Session files" tree watches it to reload so files
  // the agent created/modified show up promptly.
  fsRefreshTick: number
  planMode: boolean
  additionalDirs: string[]
  // Global fixed allowed dirs from the config (init event): granted to every
  // session, shown in the sidebar in gray with no remove button.
  globalDirs: string[]
  // Pending approval requests (directory grants + plan-exit), one entry per
  // requesting session. Concurrent requests from the main session and several
  // subagents never overwrite each other; the banner and the sidebar Requests
  // list both render from this array. Entries are pruned when the requesting
  // session stops running (an unanswered request blocks its run).
  pendingRequests: PendingRequest[]
  isSubagent: boolean
  sealed: boolean
  subagentType: string | null
  subagentDescription: string
}

const state = reactive<State>({
  sessions: [],
  projects: [],
  models: [],
  currentSession: null,
  chatItems: [],
  isStreaming: false,
  tokenCount: 0,
  maxTokens: 0,
  statusText: null,
  streamTick: 0,
  fsRefreshTick: 0,
  planMode: false,
  additionalDirs: [],
  globalDirs: [],
  pendingRequests: [],
  isSubagent: false,
  sealed: false,
  subagentType: null,
  subagentDescription: '',
})

// Whether the current session's model supports visual input. Derived from the
// session's model × the configured models list instead of being pushed once:
// any change to the session's model (own apply or another tab's, via the
// sessions poll) flips the upload button immediately, so it always follows
// the model the session will actually talk to.
const enableVl = computed(() => {
  const s = state.sessions.find((x) => x.id === state.currentSession)
  const opt = s?.model ? state.models.find((m) => m.name === s.model) : undefined
  return (opt ?? state.models[0])?.enable_vl ?? false
})

let currentTurnIdx = -1
let currentToolIdx = -1

// Tool returns that can change files in the workspace; the sidebar "Session
// files" tree refreshes on these. read/glob/grep/todo never modify the
// working tree (read's VLM images go to the session dir, not the workspace),
// so their returns skip the reload.
const FILE_CHANGING_TOOLS = new Set(['edit', 'write', 'bash', 'task'])

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

function parseSubagentsFromResult(result: string): SubagentChild[] {
  // Recover the subagent tree from a persisted <task_result> on reconnect.
  const children: SubagentChild[] = []
  const re = /<subagent\s+([^>]*)>/g
  let m: RegExpExecArray | null
  while ((m = re.exec(result)) !== null) {
    const attrs = m[1]
    const get = (k: string) => {
      const mm = attrs.match(new RegExp(`${k}="([^"]*)"`))
      return mm ? mm[1] : ''
    }
    children.push({
      session: get('session'),
      type: get('type'),
      description: get('description'),
      state: get('state') || 'completed',
    })
  }
  return children
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

// When the main session ends (especially on cancel), the backend hard-stops
// running subagents. But _run_subagents only emits `subagent_state` after the
// gather completes; a cancel tears down the gather so that loop never runs and
// the terminal event is never sent. Mark still-running children stopped here so
// the sidebar reflects the interruption immediately instead of waiting for the
// next session poll.
function markRunningSubagentsStopped(endType: string) {
  const terminal = endType === 'done' ? 'completed' : 'interrupted'
  const turn = getCurrentTurn()
  if (turn) {
    for (const t of turn.tools) {
      if (t.subagents) {
        for (const c of t.subagents) {
          if (c.state === 'running') c.state = terminal
        }
      }
    }
  }
  for (const s of state.sessions) {
    if (s.parent_session === state.currentSession && s.is_running) {
      s.is_running = false
    }
  }
}

function requestKey(kind: PendingRequest['kind'], session: string, path?: string): string {
  return `${session}:${kind}:${path ?? ''}`
}

function addPendingRequest(req: Omit<PendingRequest, 'key'>) {
  const key = requestKey(req.kind, req.session, req.path)
  if (!state.pendingRequests.some((r) => r.key === key)) {
    state.pendingRequests.push({ ...req, key })
  }
}

function removePendingRequest(req: PendingRequest) {
  state.pendingRequests = state.pendingRequests.filter((r) => r.key !== req.key)
}

// An unanswered request blocks its agent's run, so a session that is no
// longer running can no longer be awaiting approval. Drop requests for the
// current session (its run just ended) and for any session that is not
// running (hard-stopped children on a main-session cancel).
function dropEndedSessionRequests() {
  state.pendingRequests = state.pendingRequests.filter(
    (r) =>
      r.session !== state.currentSession &&
      state.sessions.some((s) => s.id === r.session && s.is_running),
  )
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
      state.globalDirs = event.global_dirs ?? []
      state.isSubagent = event.is_subagent ?? false
      state.sealed = event.sealed ?? false
      state.subagentType = event.subagent_type ?? null
      state.subagentDescription = event.description ?? ''
      closeTurn()
      break
    }

    case 'user': {
      // A wake-up opens with the synthetic "⏰ 定时唤醒" line; render a
      // run-boundary divider before it so the suspension boundary stays
      // visible (matches parseHistory on reconnect).
      if (event.text.startsWith('⏰')) {
        state.chatItems.push({ kind: 'divider', text: '定时唤醒' })
      }
      state.chatItems.push({
        kind: 'user',
        content: event.text,
        images: event.images,
        userSeq: event.user_seq,
        id: event.id,
      })
      break
    }

    case 'user_edit': {
      const item = state.chatItems.find(
        (it) => it.kind === 'user' && it.id === event.message_id,
      )
      if (item && item.kind === 'user') {
        item.content = event.text
        item.images = event.images ?? undefined
      }
      break
    }

    case 'turn':
      state.isStreaming = true
      // A new turn retires any leftover retry banner from the previous turn
      // (the backend also clears it on success; this is the reconnect-safe
      // fallback).
      state.statusText = null
      newTurn()
      break

    case 'turn_restart':
      // A failed stream attempt is being retried: throw away the partial turn
      // (broken thinking / truncated text / tool-call fragments streamed so
      // far) so the retried attempt's deltas land in a clean bubble.
      if (currentTurnIdx >= 0 && currentTurnIdx < state.chatItems.length) {
        state.chatItems.splice(currentTurnIdx, 1)
      }
      closeTurn()
      newTurn()
      break

    case 'compact': {
      // Compaction just finished: close the streamed summary turn and insert
      // the divider so the live view matches the rebuilt history on refresh.
      closeTurn()
      state.chatItems.push({ kind: 'compact' })
      break
    }

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
        streaming.toolCallId = event.tool_call_id
      } else {
        turn.tools.push({ name: event.tool, status: 'running', args: '', startArgs: event.args, toolCallId: event.tool_call_id })
      }
      break
    }

    case 'tool_output': {
      // Live stdout/stderr line from a running bash command. Match by
      // tool_call_id (set on tool_start) and append to liveOutput.
      const turn = getCurrentTurn()
      if (!turn) break
      const tool = turn.tools.find((t) => t.toolCallId === event.tool_call_id)
        ?? turn.tools.find((t) => t.name === 'bash' && t.status === 'running')
      if (tool) {
        tool.liveOutput = (tool.liveOutput ?? '') + event.text + '\n'
      }
      break
    }

    case 'tool_result': {
      // Only file-changing tools trigger a tree refresh.
      if (event.tool && FILE_CHANGING_TOOLS.has(event.tool)) {
        state.fsRefreshTick++
      }
      const turn = getCurrentTurn()
      if (!turn) break
      const running = turn.tools.find(
        (t) => t.name === event.tool && t.status === 'running',
      )
      if (running) {
        running.status = 'done'
        running.result = event.result
        // Replace the live streamed output with the truncated final result
        // so the window matches what is persisted in history.
        running.liveOutput = undefined
        if (event.diff) {
          running.diff = event.diff
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
      addPendingRequest({ kind: 'plan_exit', session: event.session })
      break

    case 'permission_request':
      addPendingRequest({
        kind: 'dir',
        session: event.session,
        path: event.path,
        tool: event.tool,
        message: event.message,
      })
      break

    case 'subagents': {
      // Attach the spawned subagent list to the currently running `task` tool.
      const turn = getCurrentTurn()
      if (turn) {
        const taskTool = turn.tools.find((t) => t.name === 'task' && t.status !== 'done')
        if (taskTool) taskTool.subagents = event.children
      }
      break
    }

    case 'subagent_state': {
      // Subagents share the workspace and may have written files.
      state.fsRefreshTick++
      // Update a child's state on the task tool node, and reflect running
      // state in the session list so the sidebar stays in sync.
      const turn = getCurrentTurn()
      if (turn) {
        for (const t of turn.tools) {
          if (t.subagents) {
            const child = t.subagents.find((c) => c.session === event.session)
            if (child) child.state = event.state
          }
        }
      }
      if (event.state !== 'running') {
        const s = state.sessions.find((x) => x.id === event.session)
        if (s) s.is_running = false
        // A finished subagent can no longer be awaiting approval.
        state.pendingRequests = state.pendingRequests.filter(
          (r) => r.session !== event.session,
        )
      }
      break
    }

    case 'subagent_permission_request':
      addPendingRequest({
        kind: 'dir',
        session: event.child_session,
        childType: event.child_type,
        childDescription: event.child_description,
        path: event.path,
        tool: event.tool,
        message: event.message,
      })
      break

    case 'sleep_start': {
      // sleep_until armed: light up the sidebar's blue dot immediately (the
      // sessions poll would catch up within 3s anyway).
      const s = state.sessions.find((x) => x.id === event.session)
      if (s) {
        s.has_sleep = true
        s.sleep_until = event.until
      }
      break
    }

    case 'sleep_end': {
      // The suspension ended (timer wake-up, early user message, or wake
      // from the UI); clear the dot. The sessions poll is the fallback.
      const s = state.sessions.find((x) => x.id === event.session)
      if (s) {
        s.has_sleep = false
        s.sleep_until = null
      }
      break
    }

    case 'done':
    case 'cancelled':
    case 'interrupted':
      state.isStreaming = false
      state.statusText = null
      state.fsRefreshTick++
      if (event.type === 'interrupted' && state.isSubagent) state.sealed = true
      markRunningSubagentsStopped(event.type)
      dropEndedSessionRequests()
      closeTurn()
      break

    case 'error': {
      state.isStreaming = false
      state.fsRefreshTick++
      dropEndedSessionRequests()
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
  let userSeq = 0
  // A wake-up boundary renders from the synthetic "⏰" user message; a
  // compact marker renders as the compaction divider. When the two are
  // adjacent (legacy scheduled-session history stores a marker right before
  // each "⏰" fire message) only one divider is drawn.
  let lastWasDivider = false

  while (i < history.length) {
    const msg = history[i]

    // System messages: the initial prompt is invisible; a compact marker
    // renders as a divider line.
    if (msg.role === 'system') {
      if (msg.compact_marker) {
        items.push({ kind: 'compact' })
        lastWasDivider = true
      }
      i++
      continue
    }

    if (msg.role === 'user') {
      // A compact summary is stored as role:user so the API sees it as user
      // content after the marker. It only serves the API context (the streamed
      // assistant summary is the visible record), so it is not rendered here.
      // It must not receive a user_seq, matching _count_user_messages.
      if (msg.compact_summary) {
        i++
        continue
      }
      // A mode-change notice is persisted as role:user for API prefix/cache
      // stability but is not a real user turn, so it is hidden here and does
      // not receive a user_seq (matching _count_user_messages).
      if (msg.is_mode_notification) {
        i++
        continue
      }
      // A wake-up message ("⏰ 定时唤醒") draws a divider before it, unless
      // the preceding marker already drew one.
      if ((msg.content || '').startsWith('⏰')) {
        if (!lastWasDivider) items.push({ kind: 'divider', text: '定时唤醒' })
      }
      lastWasDivider = false
      items.push({
        kind: 'user',
        content: msg.content || '',
        images: msg.images,
        userSeq: userSeq++,
        id: msg.id,
      })
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
            const tw = turn.tools[toolIdx]
            tw.result = history[i].content || ''
            if ((history[i] as any).diff) {
              tw.diff = (history[i] as any).diff
            } else if ((history[i] as any).diff_prev) {
              tw.diffPrev = (history[i] as any).diff_prev
              tw.diffCurrent = (history[i] as any).diff_current
            }
            if (tw.name === 'task' && tw.result) {
              tw.subagents = parseSubagentsFromResult(tw.result)
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
// Every connectStream() bumps this generation. runStream captures the
// generation it was started with and exits as soon as it is stale -- so a
// repeated click on the same session cannot leave the old runStream alive to
// open a second, orphaned SSE connection (which would eat a browser
// per-host connection and, across tabs, exhaust the pool).
let streamGen = 0
// Set by the tab-hidden listener: an abort caused by hiding (not by a session
// switch) must make runStream wait for the tab to become visible again
// instead of exiting.
let hiddenAbort = false

async function connectStream(name: string) {
  if (streamAbort) streamAbort.abort()
  streamSession = name
  streamGen++
  void runStream(name, streamGen)
}

function disconnectStream() {
  streamSession = null
  streamGen++
  if (streamAbort) streamAbort.abort()
  streamAbort = null
}

// 后台 tab 不占用 SSE 连接。切到后台时断开流、释放连接；切回前台立即
// 重连，init + 回放本来就会重建完整状态，不丢内容。多开时同一 session
// 的同 URL 并发 GET 会在 Firefox 缓存层被 single-flight 合并（见
// api.ts 的随机 query 参数），少一个挂着的连接就少一分被合并等待的风险。
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden' && streamAbort) {
    hiddenAbort = true
    streamAbort.abort()
  }
})

// Resolve on the next visibility change; the caller re-checks the state.
function waitNextVisibilityChange(): Promise<void> {
  return new Promise((resolve) => {
    const onVis = () => {
      document.removeEventListener('visibilitychange', onVis)
      resolve()
    }
    document.addEventListener('visibilitychange', onVis)
  })
}

async function runStream(name: string, gen: number) {
  while (streamSession === name && streamGen === gen) {
    // Hidden tab: no live stream. Wait until the tab is visible again (the
    // abort listener above dropped the old connection when hiding).
    if (document.visibilityState === 'hidden') {
      hiddenAbort = false
      await waitNextVisibilityChange()
      continue
    }
    hiddenAbort = false
    const ac = new AbortController()
    streamAbort = ac
    try {
      const stream = api.subscribeStream(name, ac.signal)
      for await (const event of stream) {
        if (streamSession !== name || streamGen !== gen) break
        try {
          handleSSEEvent(event)
        } catch (err) {
          console.error('handleSSEEvent threw for', event.type, err)
        }
      }
      if (streamSession !== name || streamGen !== gen) return
    } catch (e) {
      if (streamSession !== name || streamGen !== gen) return
      if (ac.signal.aborted) {
        // Aborted because the tab went hidden: wait for it to become visible
        // again. Any other abort (session switch / disconnect) has already
        // bumped the generation, so the check above returned.
        if (!hiddenAbort) return
        continue
      }
      // Network error - retry after a short backoff.
      console.error('stream error:', e instanceof Error ? e.message : String(e))
    }
    if (streamSession !== name || streamGen !== gen) return
    // Background tabs throttle setTimeout to ~1/min, which would delay the
    // reconnect for a long time; wake up early when the tab becomes visible
    // again so the stream reconnects immediately on return to the tab.
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        document.removeEventListener('visibilitychange', onVis)
        resolve()
      }, 1000)
      const onVis = () => {
        if (document.visibilityState === 'visible') {
          clearTimeout(timer)
          document.removeEventListener('visibilitychange', onVis)
          resolve()
        }
      }
      document.addEventListener('visibilitychange', onVis)
    })
  }
}

// ── session list polling ────────────────────────────────────────────────────
// The SSE stream is per-session, so it carries nothing about *other* sessions.
// The sidebar lists every session, so it is refreshed on a short poll: cheap
// (the endpoint just reads session.json files) and enough to make new/deleted
// sessions and running state appear across multiple browsers.
let sessionPollTimer: ReturnType<typeof setInterval> | null = null

function stopSessionPolling() {
  if (sessionPollTimer !== null) {
    clearInterval(sessionPollTimer)
    sessionPollTimer = null
  }
}

// 后台 tab 暂停轮询（省掉每 3s 一次的请求，也避免后台 tab 与前台抢占
// 浏览器每主机的连接配额）；切回前台立即恢复并马上刷一次列表。
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    stopSessionPolling()
  } else if (state.sessions.length > 0 && sessionPollTimer === null) {
    ensureSessionPolling()
    void loadSessionsRefresher()
  }
})

async function loadSessionsRefresher() {
  try {
    const [list, projects] = await Promise.all([api.getSessions(), api.getProjects()])
    state.sessions = list
    state.projects = projects
  } catch {
    // transient error; the next tick retries
  }
}

function ensureSessionPolling() {
  if (sessionPollTimer !== null) return
  sessionPollTimer = setInterval(async () => {
    await loadSessionsRefresher()
    // Unresolved requests block their agent's run; a session that stopped
    // running (e.g. cancelled from another tab) can no longer be awaiting
    // approval, so prune those entries.
    const running = new Set(state.sessions.filter((s) => s.is_running).map((s) => s.id))
    state.pendingRequests = state.pendingRequests.filter((r) => running.has(r.session))
  }, 3000)
}

export function useStore() {
  async function loadSessions() {
    const [list, projects, models] = await Promise.all([api.getSessions(), api.getProjects(), api.getModels()])
    state.sessions = list
    state.projects = projects
    state.models = models
    ensureSessionPolling()
  }

  async function selectSession(id: string) {
    state.currentSession = id
    closeTurn()
    state.chatItems = []
    state.statusText = null
    state.isStreaming = false
    state.isSubagent = false
    state.sealed = false
    state.subagentType = null
    state.subagentDescription = ''
    await loadSessions()
    connectStream(id)
  }

  async function createSession(name: string, workingDir: string, projectId?: string) {
    const actualId = await api.createSession(name, workingDir, projectId)
    state.currentSession = actualId
    closeTurn()
    state.chatItems = []
    state.tokenCount = 0
    state.statusText = null
    state.planMode = false
    state.additionalDirs = []
    state.globalDirs = []
    state.isStreaming = false
    await loadSessions()
    connectStream(actualId)
  }

  async function createProject(name: string, workingDir: string) {
    await api.createProject(name, workingDir)
    await loadSessions()
  }

  async function deleteProject(id: string) {
    await api.deleteProject(id)
    await loadSessions()
  }

  async function setSessionProject(projectId: string | null) {
    if (!state.currentSession) return
    await api.setSessionProject(state.currentSession, projectId)
    const s = state.sessions.find((x) => x.id === state.currentSession)
    if (s) {
      if (projectId) s.project_id = projectId
      else delete s.project_id
    }
  }

  async function deleteSession(id: string) {
    await api.deleteSession(id)
    if (state.currentSession === id) {
      disconnectStream()
      state.currentSession = null
      state.chatItems = []
      state.tokenCount = 0
      state.maxTokens = 0
      state.statusText = null
      state.planMode = false
      state.additionalDirs = []
      state.globalDirs = []
      state.isStreaming = false
      closeTurn()
    }
    await loadSessions()
  }

  async function sendMessage(message: string, images: api.ImageAttachment[] = []) {
    if (!state.currentSession) return
    // The user message is rendered from the "user" stream event (emitted by
    // the agent on start/steer), not pushed here, so reconnects stay
    // consistent with server-side history.
    try {
      await api.sendMessage(state.currentSession, message, images)
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

  async function selectModel(modelName: string) {
    if (!state.currentSession) return
    await api.setSessionModel(state.currentSession, modelName)
    const s = state.sessions.find((x) => x.id === state.currentSession)
    if (s) s.model = modelName
    // enableVl derives from the session's model × the models list, so the
    // upload button follows the switch automatically.
  }

  async function addDir(path: string) {
    if (!state.currentSession) return
    state.additionalDirs = await api.addDir(state.currentSession, path)
  }

  async function removeDir(path: string) {
    if (!state.currentSession) return
    state.additionalDirs = await api.removeDir(state.currentSession, path)
  }

  async function renameSession(name: string) {
    if (!state.currentSession) return
    await api.renameSession(state.currentSession, name)
    const s = state.sessions.find((x) => x.id === state.currentSession)
    if (s) s.name = name
  }

  async function approveRequest(req: PendingRequest) {
    // Remove the entry first so the banner/list update immediately; the API
    // call unblocks the waiting agent.
    removePendingRequest(req)
    if (req.kind === 'plan_exit') {
      if (req.session === state.currentSession) state.planMode = false
      await api.planExit(req.session, true)
    } else {
      if (!req.path) return
      if (req.session === state.currentSession) {
        state.additionalDirs = await api.addDir(req.session, req.path)
      } else {
        // Approved for a session we are not viewing (e.g. a subagent while
        // browsing the main session): refresh its dirs in the polled list so
        // the sidebar Allowed directories updates right away.
        const dirs = await api.addDir(req.session, req.path)
        const s = state.sessions.find((x) => x.id === req.session)
        if (s) s.additional_dirs = dirs
      }
      await api.resolvePermission(req.session, true)
    }
  }

  async function rejectRequest(req: PendingRequest) {
    removePendingRequest(req)
    if (req.kind === 'plan_exit') {
      await api.planExit(req.session, false)
    } else {
      await api.resolvePermission(req.session, false)
    }
  }

  async function interruptSubagent(name: string) {
    await api.interruptSession(name)
  }

  async function revert(
    messageId: string,
    message: string,
    opts: { keepImages?: string[]; newImages?: api.ImageAttachment[] } = {},
  ) {
    if (!state.currentSession) return
    try {
      const res = await api.revert(state.currentSession, messageId, message, opts)
      if (res.status === 'rerun') {
        // Reconnect so init rebuilds chatItems from the truncated history and
        // the replayed buffer carries the new user message + fresh run.
        connectStream(state.currentSession)
      }
      // 'queued': the user_edit event updates the message in place; no reconnect.
    } catch (e) {
      state.chatItems.push({
        kind: 'error',
        content: `Failed to revert: ${e instanceof Error ? e.message : String(e)}`,
      })
      state.streamTick++
    }
  }

  return {
    state,
    enableVl,
    loadSessions,
    selectSession,
    createSession,
    createProject,
    deleteProject,
    setSessionProject,
    deleteSession,
    sendMessage,
    stopAgent,
    setMode,
    selectModel,
    addDir,
    removeDir,
    renameSession,
    approveRequest,
    rejectRequest,
    interruptSubagent,
    revert,
  }
}
