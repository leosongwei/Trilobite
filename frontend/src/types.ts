export interface ImageMeta {
  filename: string
  mime_type: string
  original_name: string
  date?: string
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
  | { type: 'init'; history: HistoryMessage[]; is_running: boolean; token_count: number; max_context_tokens: number; plan_mode: boolean; additional_dirs: string[]; is_subagent?: boolean; kind?: string; sealed?: boolean; subagent_type?: string | null; description?: string; enable_vl?: boolean }
  | { type: 'user'; text: string; user_seq: number; images?: ImageMeta[] }
  | { type: 'user_edit'; user_seq: number; text: string }
  | { type: 'turn' }
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
  | { type: 'cron_fire'; schedule_id: string; session: string; description: string; state: string }
  | { type: 'cron_fire_end'; session: string; state: string }
  | { type: 'cron_missed'; schedule_id: string; session: string }
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
  created_at?: number
  updated_at?: number
  // Scheduled sessions (kind === 'scheduled'): live schedule state, refreshed
  // by the session poll. The sidebar dot renders from is_running/last_state:
  // pending (never fired) -> light blue, running -> pulsing green,
  // last_state error -> red, finished -> grey. schedule_active flips false
  // after cron_delete or a one-shot's single fire.
  schedule_id?: string
  schedule_active?: boolean
  recurring?: boolean
  deleted?: boolean
  cron?: string
  run_count?: number
  last_state?: string
  next_fire_at?: string
  prompt?: string
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
