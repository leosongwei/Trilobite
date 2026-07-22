export type SSEEvent =
  | { type: 'init'; history: HistoryMessage[]; is_running: boolean; token_count: number; max_context_tokens: number; plan_mode: boolean; additional_dirs: string[] }
  | { type: 'user'; text: string; user_seq: number }
  | { type: 'user_edit'; user_seq: number; text: string }
  | { type: 'turn' }
  | { type: 'thinking'; text: string }
  | { type: 'text'; text: string }
  | { type: 'tool_stream'; tool_name: string; args: string; complete: boolean }
  | { type: 'tool_start'; tool: string; args: Record<string, unknown> }
  | { type: 'tool_result'; tool: string; result: string; diff_prev?: string; diff_current?: string }
  | { type: 'usage'; token_count: number; max_context_tokens: number }
  | { type: 'status'; text: string }
  | { type: 'plan_exit_request' }
  | { type: 'permission_request'; path: string; tool: string; message: string }
  | { type: 'done' }
  | { type: 'cancelled' }
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
  role: 'user' | 'assistant' | 'tool'
  content?: string
  reasoning_content?: string
  tool_calls?: ToolCall[]
  tool_call_id?: string
}

export interface Session {
  name: string
  working_dir: string
  is_running: boolean
  history_length: number
  plan_mode: boolean
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

export interface ToolDisplay {
  name: string
  status: ToolStatus
  args: string
  startArgs?: Record<string, unknown>
  result?: string
  diffPrev?: string
  diffCurrent?: string
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

export type ChatItem = UserItem | TurnItem | ErrorItem
