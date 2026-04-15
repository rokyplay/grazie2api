# grazie2api

Expose Grazie AI as standard OpenAI / Anthropic / Responses API endpoints.

Bring your own credentials. Get a local API server with 50+ models including Claude, GPT, Gemini, Grok, Llama, DeepSeek, Qwen, and more.

## Features

- **Three API formats**: OpenAI Chat Completions, Anthropic Messages, OpenAI Responses
- **50+ models**: All Grazie-supported models via dynamic profile listing
- **One-click credential paste**: Auto-detects JSON, key=value, or raw token formats
- **Auto-refresh**: JWT and tokens refresh automatically in the background
- **Multi-credential pool**: Round-robin, least-used, or most-quota rotation
- **Tool/function calling**: Full support across all three API formats
- **Parameter forwarding**: temperature, top_p, reasoning_effort (for thinking models)
- **Quota monitoring**: Real-time quota tracking per credential
- **Webhook API**: External services can inject credentials programmatically
- **SillyTavern compatible**: Handles prefill, trailing assistant messages, content-role quirks

## Quick Start

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
python main.py serve
```

Server starts at `http://127.0.0.1:8800`.

## Adding Credentials

### Option 1: Browser OAuth (interactive)

```bash
python main.py login
```

Opens a browser window for OAuth login. Credentials are saved automatically.

### Option 2: Paste via Web UI

Open `http://127.0.0.1:8800/credentials` and paste your credential in any format:

```json
{"jwt": "eyJ...", "refresh_token": "1//...", "license_id": "ABC123"}
```

or key=value:

```
jwt=eyJ...
refresh_token=1//...
license_id=ABC123
```

or just paste raw tokens — they're auto-detected.

### Option 3: API / Webhook

```bash
# Paste endpoint (auto-parse any format)
curl http://127.0.0.1:8800/api/credentials/paste \
  -H "Content-Type: application/json" \
  -d '{"blob": "jwt=eyJ... refresh_token=1//... license_id=ABC123"}'

# Webhook (structured, for external activation services)
curl http://127.0.0.1:8800/api/credentials/webhook \
  -H "Content-Type: application/json" \
  -d '{"jwt": "eyJ...", "refresh_token": "1//...", "license_id": "ABC123"}'
```

### Option 4: Config file

Add accounts in `config.yaml` for automatic OAuth login on startup:

```yaml
accounts:
  - email: "your@email.com"
    password: "your_password"
```

## API Usage

### OpenAI Chat Completions

```bash
curl http://127.0.0.1:8800/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-4.6-sonnet",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

### Anthropic Messages

```bash
curl http://127.0.0.1:8800/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-4.6-sonnet",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 1024
  }'
```

### OpenAI Responses

```bash
curl http://127.0.0.1:8800/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.4",
    "input": "What is 2+2?"
  }'
```

### Model List

```bash
curl http://127.0.0.1:8800/v1/models
```

## Model Aliases

Use friendly names or full profile names:

| Alias | Profile |
|-------|---------|
| claude-4.6-opus | anthropic-claude-4-6-opus |
| claude-4.6-sonnet | anthropic-claude-4-6-sonnet |
| gpt-5.4 | openai-gpt-5-4 |
| gemini-3.1-pro | google-gemini-3-1-pro |
| grok-4 | xai-grok-4 |
| deepseek-r1 | deepseek-r1 |

Run `GET /v1/models` for the full list.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `server.host` | 127.0.0.1 | Listen address |
| `server.port` | 8800 | Listen port |
| `strategy` | round_robin | Credential rotation strategy |
| `tokens.jwt_margin_seconds` | 300 | Refresh JWT this many seconds before expiry |
| `credentials.cooldown_seconds` | 600 | Cooldown period after errors |
| `quota.refresh_interval_seconds` | 300 | Quota check interval |

See `config.example.yaml` for all options.

## CLI

```bash
python main.py serve            # Start the API server
python main.py login            # Interactive browser OAuth login
python main.py serve --port 9000 --api-key my-secret
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/messages` | Anthropic Messages |
| `POST /v1/responses` | OpenAI Responses |
| `GET /v1/models` | List available models |
| `POST /api/credentials/paste` | Add credential (auto-parse) |
| `POST /api/credentials/webhook` | Add credential (structured) |
| `GET /api/credentials` | List credentials (admin) |
| `DELETE /api/credentials/{id}` | Remove credential |
| `GET /health` | Health check |
| `GET /credentials` | Credential management UI |

## How It Works

1. **OAuth PKCE**: Uses RFC 7636 PKCE flow with localhost callback (RFC 8252) for browser-based login
2. **License Discovery**: Automatically finds your active subscription license
3. **JWT Lifecycle**: `refresh_token` → `id_token` (1h) → `JWT` (24h) — all auto-refreshed
4. **Format Conversion**: Translates between OpenAI/Anthropic/Responses formats and the upstream Grazie chat API
5. **Message Sanitization**: Fixes orphaned tool messages, trailing assistant prefills, and SillyTavern quirks

## Supported Parameters

| Parameter | Forwarded | Notes |
|-----------|-----------|-------|
| temperature | Yes | Mutually exclusive with top_p |
| top_p | Yes | Only sent if no temperature |
| reasoning_effort | Yes | Only for thinking models (o3, o4) |
| tools / functions | Yes | Full tool calling support |
| stream | Yes | SSE streaming |
| top_k | No | Causes upstream 400 |
| seed | No | Causes upstream 400 |
| max_tokens | No | Managed by upstream |
| stop | No | Not supported by upstream |

## License

AGPL-3.0 — see [LICENSE](LICENSE).

If you use this code in a network service, you must make the complete source code available to all users of that service under the same license.
