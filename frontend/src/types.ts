export type SSEEvent =
  | { type: 'turn' }
  | { type: 'thinking'; text: string }
  | { type: 'text'; text: string }
  | { type: 'tool_stream'; tool_name: string; args: string; complete: boolean }
  | { type: 'tool_start'; tool: string; args: Record<string, unknown> }
  | { type: 'tool_result'; tool: string; result: string }
  | { type: 'usage'; token_count: number; max_context_tokens: number }
  | { type: 'status'; text: string }
  | { type: 'done' }
  | { type: 'cancelled' }
  | { type: 'error'; text: string }

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
}

export type ToolStatus = 'streaming' | 'running' | 'done'

export interface ToolDisplay {
  name: string
  status: ToolStatus
  args: string
  result?: string
}

export interface UserItem {
  kind: 'user'
  content: string
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
