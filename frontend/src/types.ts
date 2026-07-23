export type SSEEvent =
  | { type: 'init'; history: HistoryMessage[]; is_running: boolean; token_count: number; max_context_tokens: number; plan_mode: boolean; additional_dirs: string[]; is_subagent?: boolean; sealed?: boolean; subagent_type?: string | null; description?: string }
  | { type: 'user'; text: string; user_seq: number }
  | { type: 'user_edit'; user_seq: number; text: string }
  | { type: 'turn' }
  | { type: 'compact' }
  | { type: 'thinking'; text: string }
  | { type: 'text'; text: string }
  | { type: 'tool_stream'; tool_name: string; args: string; complete: boolean }
  | { type: 'tool_start'; tool: string; args: Record<string, unknown> }
  | { type: 'tool_result'; tool: string; result: string; diff_prev?: string; diff_current?: string }
  | { type: 'usage'; token_count: number; max_context_tokens: number }
  | { type: 'status'; text: string }
  | { type: 'plan_exit_request' }
  | { type: 'permission_request'; path: string; tool: string; message: string }
  | { type: 'subagents'; parent: string; children: SubagentChild[] }
  | { type: 'subagent_state'; session: string; state: string }
  | { type: 'subagent_permission_request'; child_session: string; child_type: string; child_description: string; path: string; tool: string; message: string }
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
  reasoning_content?: string
  tool_calls?: ToolCall[]
  tool_call_id?: string
  compact_marker?: boolean
  compact_summary?: boolean
}

export interface Session {
  name: string
  working_dir: string
  is_running: boolean
  history_length: number
  plan_mode: boolean
  parent_session?: string
  subagent_type?: string
  description?: string
  sealed?: boolean
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

export interface ToolDisplay {
  name: string
  status: ToolStatus
  args: string
  startArgs?: Record<string, unknown>
  result?: string
  diffPrev?: string
  diffCurrent?: string
  subagents?: SubagentChild[]
}

export interface UserItem {
  kind: 'user'
  content: string
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

export type ChatItem = UserItem | TurnItem | ErrorItem | CompactItem
