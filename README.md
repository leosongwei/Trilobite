# Trilobite

A coding agent with a web UI, powered by DeepSeek.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url> trilobite
cd trilobite
uv sync
```

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

## How it works

- Create a session with a name and working directory
- Type a message — the agent reads, writes files, and runs bash commands in that directory
- If the working directory has an `AGENTS.md`, it's automatically included as context
- Todo list tracks progress with `pending / in_progress / done` states
- Context automatically compacts via LLM summarization when approaching the token limit
- Stop button (■) cancels current thinking while preserving partial output
- Press Enter to send, type while the agent is running to steer
