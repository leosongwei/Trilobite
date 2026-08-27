export interface ImageMeta {
  filename: string
  mime_type: string
  original_name: string
  date?: string
}

// A predefined model definition from the backend config's ``models`` list
// (frontend shape; the api_key never leaves the server).
export interface ModelOption {
  name: string
  model: string
  api_url: string
  enable_vl: boolean
  max_context: number
  max_tokens: number
  compaction_trigger_ratio: number
}

// A session-grouping folder. Projects are lightweight: they only record a
// name and a working directory (the default for sessions created from the
// project); member sessions keep their own working_dir and reference the
// project by id.
export interface Project {
  id: string
  name: string
  working_dir: string
  created_at?: number
}

// A pending approval request: a directory grant (main session or subagent)
// or a plan-exit switch request. Multiple requests can be pending at once
// (main session + several subagents), so they live in a list keyed by
// requesting session instead of a single banner slot.
export type PendingRequestKind = 'dir' | 'plan_exit'

export interface PendingRequest {
  /** Unique dedupe key: `${session}:${kind}:${path ?? ''}`. */
  key: string
  kind: PendingRequestKind
  /** Session that made the request (main session id or subagent session id). */
  session: string
  /** Subagent display info, set for subagent directory requests. */
  childType?: string
  childDescription?: string
  path?: string
  tool?: string
  message?: string
}

export type SSEEvent =
  | { type: 'init'; history: HistoryMessage[]; is_running: boolean; token_count: number; max_context_tokens: number; plan_mode: boolean; additional_dirs: string[]; global_dirs?: string[]; is_subagent?: boolean; kind?: string; sealed?: boolean; subagent_type?: string | null; description?: string; enable_vl?: boolean }
  | { type: 'user'; id: string; text: string; user_seq: number; images?: ImageMeta[] }
  | { type: 'user_edit'; message_id: string; text: string }
  | { type: 'turn' }
  | { type: 'turn_restart' }
  | { type: 'compact' }
  | { type: 'thinking'; text: string }
  | { type: 'text'; text: string }
  | { type: 'tool_stream'; tool_name: string; args: string; complete: boolean }
  | { type: 'tool_start'; tool: string; args: Record<string, unknown>; tool_call_id?: string }
  | { type: 'tool_output'; tool_call_id: string; stream: 'stdout' | 'stderr'; text: string }
  | { type: 'tool_result'; tool: string; result: string; tool_call_id?: string; diff?: DiffRow[]; diff_prev?: string; diff_current?: string }
  | { type: 'usage'; token_count: number; max_context_tokens: number }
  | { type: 'status'; text: string }
  | { type: 'plan_exit_request'; session: string }
  | { type: 'permission_request'; session: string; path: string; tool: string; message: string }
  | { type: 'subagents'; parent: string; children: SubagentChild[] }
  | { type: 'subagent_state'; session: string; state: string }
  | { type: 'subagent_permission_request'; child_session: string; child_type: string; child_description: string; path: string; tool: string; message: string }
  | { type: 'sleep_start'; session: string; until: number }
  | { type: 'sleep_end'; session: string }
  | { type: 'done' }
  | { type: 'cancelled' }
  | { type: 'interrupted' }
  | { type: 'error'; text: string; status_code?: number; error_type?: string; error_code?: string }

export interface ToolCall {
  id: string
  type: 'function'
  function: {
    name: string
    arguments: string
  }
}

export interface HistoryMessage {
  /** Message id (v3 history); used by revert to address a specific user message. */
  id?: string
  role: 'user' | 'assistant' | 'tool' | 'system'
  content?: string
  images?: ImageMeta[]
  reasoning_content?: string
  tool_calls?: ToolCall[]
  tool_call_id?: string
  compact_marker?: boolean
  compact_summary?: boolean
  is_compact_prompt?: boolean
  is_mode_notification?: boolean
}

export interface Session {
  id: string
  name: string
  working_dir: string
  is_running: boolean
  history_length: number
  plan_mode: boolean
  parent_session?: string
  subagent_type?: string
  kind?: string
  description?: string
  sealed?: boolean
  additional_dirs?: string[]
  /** The session's chosen model (display name from the config's models list). */
  model?: string
  /** Project (sidebar grouping) this main session belongs to. */
  project_id?: string
  created_at?: number
  updated_at?: number
  // Whether the session is suspended via sleep_until (the sidebar shows a
  // blue dot and sorts such sessions to the top). sleep_until is the armed
  // target (epoch seconds) for the banner/tooltip; both are refreshed by the
  // sessions poll and kept live by the sleep_start/sleep_end SSE events.
  has_sleep?: boolean
  sleep_until?: number | null
}

export interface SessionInfo {
  name: string
  working_dir: string
  is_running: boolean
  token_count: number
  max_context_tokens: number
  plan_mode: boolean
  additional_dirs: string[]
}

export type ToolStatus = 'streaming' | 'running' | 'done'

export interface SubagentChild {
  session: string
  type: string
  description: string
  state: string
}

export interface DiffRow {
  type: 'equal' | 'added' | 'removed'
  /** 1-based line number in the original file (null for added lines). */
  old: number | null
  /** 1-based line number in the resulting file (null for removed lines). */
  new: number | null
  text: string
}

export interface ToolDisplay {
  name: string
  status: ToolStatus
  args: string
  startArgs?: Record<string, unknown>
  toolCallId?: string
  result?: string
  liveOutput?: string
  diff?: DiffRow[]
  /** @deprecated legacy string fragments from older sessions; used as fallback. */
  diffPrev?: string
  diffCurrent?: string
  subagents?: SubagentChild[]
}

export interface UserItem {
  kind: 'user'
  content: string
  images?: ImageMeta[]
  userSeq?: number
  /** Message id (v3 history); present when the backend sent it. */
  id?: string
}

export interface TurnItem {
  kind: 'turn'
  thinking: string
  text: string
  tools: ToolDisplay[]
}

export interface ErrorItem {
  kind: 'error'
  content: string
}

export interface CompactItem {
  kind: 'compact'
}

/** Run boundary inside a scheduled agent's session (a cron fire divider). */
export interface DividerItem {
  kind: 'divider'
  text: string
}

export type ChatItem = UserItem | TurnItem | ErrorItem | CompactItem | DividerItem
