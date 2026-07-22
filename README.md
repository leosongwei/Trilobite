# Trilobite

A coding agent with a web UI, powered by DeepSeek.

## Install

Requires Python 3.12+ and [uv](https://docs.ustral.sh/uv/).

```bash
git clone <repo-url> trilobite
cd trilobite
uv sync
```

### Frontend

The web UI is a Vue 3 + TypeScript app in `frontend/`. Build it once after install:

```bash
cd frontend
npm install
npm run build
```

This compiles to `src/trilobite/static/` (gitignored). Runtime only needs FastAPI.

## Configure

Copy the example config and edit it:

```bash
cp config_example/config.yaml config/config.yaml
```

Or just start the server — it will auto-copy `config_example/` to `config/` on first run.

Edit `config/config.yaml`:

```yaml
model: "deepseek-chat"           # or deepseek-reasoner
api_key: "sk-your-key-here"
api_url: "https://api.deepseek.com/v1"
reasoning_effort: "max"          # high or max
max_context_tokens: 1048576      # 1M
compaction_trigger_ratio: 0.7    # compact at 70% of context
```

You can also customize:

- `config/system_prompt.txt` — the system prompt
- `config/compaction_prompt.txt` — instructions for context compaction

## Run

```bash
uv run trilobite
```

Then open http://localhost:8000.

### Frontend development

For hot-reload during frontend development:

```bash
# Terminal 1: backend
uv run trilobite

# Terminal 2: Vite dev server (proxies /api to :8000)
cd frontend && npm run dev
```

Open http://localhost:5173. Changes to `.vue`/`.ts` files update instantly.

## How it works

- Create a session with a name and working directory
- Type a message — the agent reads, writes files, and runs bash commands in that directory
- If the working directory has an `AGENTS.md`, it's automatically included as context
- Todo list tracks progress with `pending / in_progress / done` states
- Context automatically compacts via LLM summarization when approaching the token limit
- Stop button (■) cancels current thinking while preserving partial output
- Press Enter to send, type while the agent is running to steer

### System prompt assembly

Each API request sends the following message sequence:

```
[
  { "role": "system",    "content": system_prompt + working_context },
  ...conversation history (user / assistant / tool messages)...
]
```

Where the system message is concatenated as:

| Part | Source | Description |
|------|--------|-------------|
| `system_prompt` | `config/system_prompt.txt` | Base instructions for the agent |
| `working_context` | `<working_dir>/AGENTS.md` | If present, wrapped in `<AGENTS.md>...</AGENTS.md>` and appended |

On context compaction, the system prompt + working context is reused, and the compacted history begins with a `[Context summary]` followed by `[Working context - project rules]`.

### Agent loop

Each user message triggers an agentic loop that runs until the model produces a final answer with no tool calls:

1. **Compaction check** — if token usage exceeds the trigger ratio, summarize older history via LLM and replace it with a compact summary.
2. **Build messages** — concatenate `system_prompt + working_context` as the system message, followed by the full conversation history.
3. **Stream API call** — send the messages with tool definitions and thinking mode enabled; stream back thinking tokens, text content, and tool call arguments.
4. **Accumulate response** — collect streamed `reasoning_content`, `content`, and `tool_calls` into complete pieces.
5. **Branch**:
   - **If tool calls returned** — execute each tool sequentially in the working directory, append tool results to history, then check for steering input (see below) and loop back to step 1.
   - **If no tool calls** — the text content is the final answer; save it to history, emit `done`, and exit the loop.

**Steering**: while the loop is running, the user can type new messages. These are queued and injected into history between tool-call rounds, so the next API call sees the new input without interrupting the current stream.

**Cancellation**: the stop button cancels the current task. Partial thinking and text are saved to history before exiting.
