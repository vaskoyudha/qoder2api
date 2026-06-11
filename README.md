# qoder2api

An OpenAI-compatible API proxy that wraps [qodercli](https://qoder.com/cli) with multi-account round-robin rotation. Turn 115 free Qoder accounts into a 23,000 requests/day coding agent API that works with [opencode](https://github.com/opencode-ai/opencode), Cursor, or any OpenAI-compatible client.

## Features

- **OpenAI-compatible API** — `POST /v1/chat/completions` with streaming and non-streaming support
- **115-account round-robin rotation** — each request uses a different account, no shared auth files
- **Automatic retry on rate limit** — when an account hits a rate limit, the proxy retries with the next account
- **Tool execution** — qodercli executes tools internally (Read, Write, Bash, WebSearch, WebFetch, Grep, Glob, Edit)
- **Tool visibility in stream** — streaming responses show tool usage (e.g., `> **Reading** /etc/hostname`)
- **Image support** — base64 and URL images passed as `--attachment` to qodercli
- **System prompt processing** — strips tool definitions, preserves project instructions, injects tool summary
- **Per-request auth isolation** — `QODER_CONFIG_DIR` env var for true concurrent parallel requests
- **Real usage stats** — extracts token counts from qodercli's stream-json output

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  opencode / Cursor / any OpenAI client                   │
│  POST /v1/chat/completions (streaming)                   │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────┐
│  qoder2api proxy (http://127.0.0.1:8963)                 │
│                                                          │
│  1. Extract messages + tools from OpenAI format          │
│  2. Process system prompt (strip tool defs, inject       │
│     tool summary so qodercli knows client expectations)  │
│  3. Pick next account from round-robin pool              │
│  4. Set QODER_CONFIG_DIR=/tmp/qoder_slots/{idx}         │
│  5. Spawn qodercli -p -o stream-json                     │
│     --permission-mode bypass_permissions                 │
│  6. Parse stream-json events in real-time                │
│  7. Convert text deltas → OpenAI SSE chunks              │
│  8. Convert tool_use events → status messages            │
│  9. On rate limit → mark exhausted, retry next account   │
└────────────────────────┬─────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
   ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
   │ slot 0      │ │ slot 1      │ │ slot N      │
   │ .auth/      │ │ .auth/      │ │ .auth/      │
   │  machine_id │ │  machine_id │ │  machine_id │
   │  user       │ │  user       │ │  user       │
   └─────┬───────┘ └─────┬───────┘ └─────┬───────┘
         │               │               │
         ▼               ▼               ▼
   ┌─────────────────────────────────────────────┐
   │  Qoder API (api3.qoder.sh)                  │
   │  Each slot has isolated auth credentials    │
   └─────────────────────────────────────────────┘
```

## How Tool Calling Works

Unlike typical OpenAI tool calling (model returns `tool_calls`, client executes, feeds back results), qodercli executes tools internally:

1. Client sends tools in OpenAI format → proxy extracts tool names/descriptions
2. Tool summary injected into system prompt: "Use these tools proactively..."
3. qodercli runs with `--permission-mode bypass_permissions` — auto-executes all tools
4. qodercli's stream-json emits `tool_use` events when tools are called
5. Proxy converts tool events to visible status messages in the stream:
   ```
   > **Reading** `/path/to/file`
   > **Running** `ls -la`
   > **Searching web** for "latest AI news"
   ```
6. Final text response includes results from all tool executions

This is similar to how [9Router](https://github.com/decolua/9router) proxies providers — but simpler since qodercli handles execution internally.

## Installation

### Quick Install

```bash
git clone https://github.com/vaskoyudha/qoder2api.git
cd qoder2api
chmod +x install.sh
./install.sh
```

### Manual Install

```bash
# 1. Clone and install dependencies
git clone https://github.com/vaskoyudha/qoder2api.git
cd qoder2api
pip install -r requirements.txt

# 2. Start the proxy
python3 proxy.py
```

### Prerequisites

- **Python 3.8+** with `pycryptodome` package
- **qodercli** installed and in PATH — [install guide](https://qoder.com/cli)
- **9Router** with Qoder accounts configured — [9Router repo](https://github.com/decolua/9router)

The proxy reads account tokens from 9Router's SQLite database at `~/.9router/db/data.sqlite`.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `QODER_PORT` | `8963` | Proxy listen port |
| `QODER_HOST` | `0.0.0.0` | Proxy listen address |
| `QODERCLI_BIN` | `qodercli` | Path to qodercli binary |
| `QODER_TIMEOUT` | `300` | Max seconds per request |
| `PROACTIVE_LIMIT` | `190` | Requests before marking account exhausted |

### opencode Configuration

Add to `~/.config/opencode/opencode.json` under `provider`:

```json
{
  "qoder-cli": {
    "npm": "@ai-sdk/openai-compatible",
    "name": "Qoder CLI (115 accounts, 23K/day)",
    "options": {
      "baseURL": "http://127.0.0.1:8963/v1",
      "apiKey": "not-needed",
      "timeout": 300000,
      "chunkTimeout": 120000
    },
    "models": {
      "qoder-unlimited": {
        "name": "Qoder Qwen3.7-Max Unlimited",
        "id": "qoder-unlimited",
        "modalities": {
          "input": ["text", "image"],
          "output": ["text"]
        },
        "limit": { "context": 128000, "output": 32000 }
      }
    }
  }
}
```

## Account Rotation

The proxy pre-creates 115 isolated auth directories at `/tmp/qoder_slots/`:

```
/tmp/qoder_slots/
├── 0/.auth/{machine_id, user}   # Account 0
├── 1/.auth/{machine_id, user}   # Account 1
├── ...
└── 114/.auth/{machine_id, user} # Account 114
```

Each request:
1. Picks the next account via atomic round-robin counter
2. Sets `QODER_CONFIG_DIR=/tmp/qoder_slots/{idx}` for isolated auth
3. Spawns qodercli with that environment
4. If rate-limited, marks account exhausted and retries with next account

Daily reset at midnight clears all exhaustion flags.

## API Endpoints

### `POST /v1/chat/completions`
Standard OpenAI chat completions with streaming support.

### `GET /v1/models`
Returns available model aliases.

### `GET /health`
Returns proxy status including:
- Active/exhausted account counts
- Requests today / daily capacity
- Total requests served

## How It Compares to 9Router

| Feature | qoder2api | 9Router |
|---------|-----------|---------|
| Tool execution | qodercli internal | Client-side loop |
| Protocol | OpenAI SSE | OpenAI SSE |
| Auth | Device tokens via 9Router DB | COSY signing |
| Providers | Qoder only | 40+ providers |
| Complexity | Single Python file | Full Next.js app |
| Account rotation | Built-in round-robin | Per-connection |

## Troubleshooting

**"Error: upstream returned an error"**
- Check proxy logs: `tail -f /tmp/qoder-proxy.log`
- Likely a rate-limited account — proxy should auto-retry

**Proxy not starting**
- Check if port 8963 is in use: `lsof -i :8963`
- Verify qodercli is installed: `qodercli --version`
- Check 9Router DB exists: `ls ~/.9router/db/data.sqlite`

**Slow responses**
- qodercli stream-json has ~10-30s startup overhead for model initialization
- Increase `QODER_TIMEOUT` if complex tasks need more time

## License

MIT
