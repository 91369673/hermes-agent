---
name: honcho-local-embeddings
description: Use when setting up, repairing, or verifying a local OpenAI-compatible embedding service for self-hosted Honcho semantic search on Giampiero's Hermes Mac.
version: 1.0.0
author: Carmen / Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [honcho, memory, embeddings, semantic-search, fastembed, launchagent, macos]
    related_skills: [hermes-agent, apple-silicon-local-llms]
---

# Honcho Local Embeddings

## Overview

Self-hosted Honcho needs embeddings for semantic search tools such as `search_memory` and `search_messages`. The LLM used for Honcho reasoning can be DeepSeek, OpenAI, Anthropic, etc.; that is separate from embeddings. Embeddings only convert text into vectors for pgvector similarity search.

On Giampiero's M1 Hermes Mac, the validated setup is a small local FastEmbed service exposing an OpenAI-compatible `/v1/embeddings` endpoint. Honcho calls it from its Docker container via `host.docker.internal`.

Validated production shape:

- Service directory: `~/.hermes/services/local-embeddings/`
- LaunchAgent: `~/Library/LaunchAgents/sg.tva.local-embeddings.plist`
- LaunchAgent label: `sg.tva.local-embeddings`
- Host endpoint: `http://127.0.0.1:18080/v1/embeddings`
- Docker/Honcho endpoint: `http://host.docker.internal:18080/v1`
- Model: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- Native dimensions: `384`
- Served dimensions: `1536` via zero-padding for Honcho pgvector schema compatibility

## When to Use

Use this skill when:

- Honcho logs show `openai.AuthenticationError`, `invalid_api_key`, or `401` from embedding calls.
- `search_memory` or `search_messages` fails while Honcho context/reasoning still works.
- You need to configure local semantic search for Honcho without external OpenAI embedding spend.
- You need to verify or restart Giampiero's local M1 embedding service.
- You need to decide whether a flagship local LLM, oMLX, or M4 offload is required for Honcho embeddings.

Do not use this for:

- Honcho LLM/deriver/dialectic model configuration. That is separate.
- General local chat-LLM serving. Use `apple-silicon-local-llms`.
- M4 batch offload workflows. Use `m4-offload-workflow`.

## Key Principle

Do not solve embeddings with a flagship chat model. Honcho semantic search needs an embedding model, not a reasoning model.

A small multilingual embedding model is enough for memory retrieval. The tested MiniLM model is fast and low-RAM on M1/16 GB, and it correctly ranks German/English Sellercentral/Amazon-DE/BOWLIO relaunch texts above unrelated control text.

## Health Checks

Check the local embedding service:

```bash
launchctl print gui/$(id -u)/sg.tva.local-embeddings | grep -E 'state =|pid =|runs =|path =|program ='
curl -sS http://127.0.0.1:18080/health
```

Expected health response:

```json
{"ok":true,"model":"sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2","target_dim":1536}
```

Check Honcho health:

```bash
curl -sS http://127.0.0.1:8000/health
```

Expected:

```json
{"status":"ok"}
```

Check the embedding endpoint directly from the host:

```bash
python3 - <<'PY'
from openai import OpenAI
c = OpenAI(api_key='dummy', base_url='http://127.0.0.1:18080/v1')
r = c.embeddings.create(
    model='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
    input=['Honcho semantic search test'],
    dimensions=1536,
)
emb = r.data[0].embedding
print('dim', len(emb), 'nonzero', sum(1 for x in emb if abs(x) > 1e-12))
PY
```

Expected:

```text
dim 1536 nonzero 384
```

Check that Honcho can reach the endpoint from inside Docker:

```bash
docker exec -i honcho-api-1 /app/.venv/bin/python - <<'PY'
import json, urllib.request
payload = {
    'model': 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
    'input': ['test'],
    'dimensions': 1536,
}
req = urllib.request.Request(
    'http://host.docker.internal:18080/v1/embeddings',
    data=json.dumps(payload).encode(),
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer dummy'},
)
with urllib.request.urlopen(req, timeout=10) as r:
    obj = json.load(r)
print('OK', len(obj['data'][0]['embedding']))
PY
```

Expected:

```text
OK 1536
```

## Honcho Configuration

Honcho service directory:

```bash
cd /Users/giampierosirianni/.hermes/services/honcho
```

The Honcho `.env` must contain:

```text
EMBED_MESSAGES=true
EMBEDDING_MODEL_CONFIG__TRANSPORT=openai
EMBEDDING_MODEL_CONFIG__MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
EMBEDDING_MODEL_CONFIG__OVERRIDES__BASE_URL=http://host.docker.internal:18080/v1
EMBEDDING_MODEL_CONFIG__OVERRIDES__API_KEY=dummy-local-embeddings
EMBEDDING_MODEL_CONFIG__DIMENSIONS_MODE=always
EMBEDDING_VECTOR_DIMENSIONS=1536
EMBEDDING_MAX_INPUT_TOKENS=512
```

Notes:

- `EMBED_MESSAGES=true` is required for message semantic search to index new messages.
- The API key is a dummy value because the local server accepts OpenAI-compatible requests without real auth.
- `DIMENSIONS_MODE=always` makes Honcho send `dimensions=1536`.
- The local server pads the native 384-dim vector to 1536 dimensions for the existing Honcho pgvector schema.

After changing `.env`, restart only the Honcho API container:

```bash
cd /Users/giampierosirianni/.hermes/services/honcho
docker compose up -d --force-recreate api
for i in $(seq 1 30); do
  s=$(curl -sS --max-time 3 http://127.0.0.1:8000/health 2>/dev/null || true)
  if [ -n "$s" ]; then echo "$s"; exit 0; fi
  sleep 2
done
docker logs --tail 80 honcho-api-1
exit 1
```

## Honcho Internal Verification

Run inside the Honcho API container:

```bash
docker exec -i honcho-api-1 /app/.venv/bin/python - <<'PY'
import asyncio
from src.config import settings, resolve_embedding_model_config
from src.embedding_client import EmbeddingClient

cfg = resolve_embedding_model_config(settings.EMBEDDING.MODEL_CONFIG)
print('transport', cfg.transport)
print('model', cfg.model)
print('base_url', cfg.base_url)
print('vector_dim', settings.EMBEDDING.VECTOR_DIMENSIONS)
print('send_dimensions', settings.EMBEDDING.resolve_send_dimensions())

async def main():
    v = await EmbeddingClient().embed('Amazon DE historische Verkaufsdaten EU Relaunch BOWLIO Modelle Größen')
    print('embed_dim', len(v), 'nonzero', sum(1 for x in v if abs(x) > 1e-12))

asyncio.run(main())
PY
```

Expected:

```text
transport openai
model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
base_url http://host.docker.internal:18080/v1
vector_dim 1536
send_dimensions True
embed_dim 1536 nonzero 384
```

## Semantic Search Smoke Test

Create two messages and search for the relevant one:

```bash
python3 - <<'PY'
import json, urllib.request, time, uuid

base = 'http://127.0.0.1:8000/v3'
ws = 'embedding-smoke-carmen'
sess = 'semantic-search-smoke-' + uuid.uuid4().hex[:8]

messages = {
    'messages': [
        {
            'peer_id': 'user',
            'content': 'BOWLIO Amazon Deutschland historische Verkaufsdaten helfen beim EU Relaunch die richtigen Modelle und Größen zu wählen. Sellercentral lifetime sales data.',
        },
        {
            'peer_id': 'assistant',
            'content': 'Der Kontrolltext handelt vom Wetter in Rom und ist für Sellercentral irrelevant.',
        },
    ]
}

req = urllib.request.Request(
    f'{base}/workspaces/{ws}/sessions/{sess}/messages',
    data=json.dumps(messages).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST',
)
with urllib.request.urlopen(req, timeout=20) as r:
    created = json.load(r)
    print('create_status', r.status, 'session', sess, 'created', len(created))

for wait in [1, 2, 4, 8]:
    time.sleep(wait)
    body = {
        'query': 'Welche Größen und Modelle anhand Amazon DE lifetime sales für Europa Relaunch?',
        'limit': 5,
    }
    req = urllib.request.Request(
        f'{base}/workspaces/{ws}/search',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
        print('search_status', r.status, 'after_s', wait, 'results', len(data))
        for item in data[:3]:
            print('-', item.get('content', '')[:180])
        if data:
            break
PY
```

Expected:

- `create_status 201`
- `search_status 200`
- at least one result
- the BOWLIO/Amazon-DE historical sales text appears in the result set

## Log Verification

After restart and smoke test, confirm no old OpenAI embedding errors remain:

```bash
docker logs --since 5m honcho-api-1 2>&1 \
  | grep -Ei 'invalid_api_key|AuthenticationError|search_memory failed|search_messages failed|Traceback| - ERROR - |embedding.*failed|HTTP/1\.1" 401' \
  || true
```

Expected output: empty.

## Creating the Service From Scratch

If `~/.hermes/services/local-embeddings/` is missing, recreate it:

```bash
BASE=/Users/giampierosirianni/.hermes/services/local-embeddings
mkdir -p "$BASE"
python3 -m venv "$BASE/.venv"
"$BASE/.venv/bin/python" -m pip install --upgrade pip
"$BASE/.venv/bin/python" -m pip install fastembed fastapi uvicorn openai
```

Create `openai_embed_server.py` in that directory. It must:

- expose `GET /health`
- expose `GET /v1/models`
- expose `POST /v1/embeddings`
- use `fastembed.TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')`
- accept OpenAI-style `input`, `model`, and `dimensions`
- return OpenAI-style `data: [{object, index, embedding}]`
- pad 384-dim vectors with zeros to 1536 when requested

Use a LaunchAgent rather than an ad-hoc background process so it survives restarts/login sessions:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>sg.tva.local-embeddings</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/giampierosirianni/.hermes/services/local-embeddings/start.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>/Users/giampierosirianni/.hermes/services/local-embeddings</string>
  <key>StandardOutPath</key>
  <string>/Users/giampierosirianni/.hermes/services/local-embeddings/service.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/giampierosirianni/.hermes/services/local-embeddings/service.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LOCAL_EMBED_MODEL</key>
    <string>sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2</string>
    <key>LOCAL_EMBED_TARGET_DIM</key>
    <string>1536</string>
  </dict>
</dict>
</plist>
```

Start/restart it:

```bash
LABEL=sg.tva.local-embeddings
PLIST=/Users/giampierosirianni/Library/LaunchAgents/$LABEL.plist
launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PLIST"
launchctl kickstart -k gui/$(id -u)/$LABEL
```

## Common Pitfalls

1. **Confusing chat models with embedding models.** oMLX serving `Qwen3.6-35B-A3B-6bit` can answer chat completions but returns `400` for `/v1/embeddings` because it is not an embedding model.

2. **Forgetting Docker networking.** From the host use `127.0.0.1:18080`; from Honcho's Docker container use `host.docker.internal:18080`.

3. **Leaving `EMBED_MESSAGES=false`.** Search may embed the query but no message vectors are created for new messages, so semantic message search returns zero results.

4. **Changing vector dimensions without DB migration.** Existing Honcho pgvector columns are 1536-dim. Use zero-padding unless deliberately running Honcho's embedding schema migration.

5. **Using a real secret for local embeddings.** The dummy local API key is sufficient; do not type or store real API keys unnecessarily.

6. **Interpreting old logs as current failure.** Use `docker logs --since 5m honcho-api-1` after restart and smoke tests.

7. **Assuming a stronger model is always better.** Larger embedding models may improve retrieval but are not required for stable Honcho memory. Start with the small validated model; upgrade only if retrieval quality is demonstrably insufficient.

## Verification Checklist

- [ ] LaunchAgent `sg.tva.local-embeddings` is running.
- [ ] `curl http://127.0.0.1:18080/health` returns OK with target dim 1536.
- [ ] Host OpenAI-client embedding probe returns `dim 1536 nonzero 384`.
- [ ] Honcho `.env` points embeddings at `http://host.docker.internal:18080/v1`.
- [ ] `EMBED_MESSAGES=true` is set.
- [ ] Honcho API restarted and `/health` returns `{"status":"ok"}`.
- [ ] Honcho internal `EmbeddingClient().embed(...)` returns `embed_dim 1536 nonzero 384`.
- [ ] Semantic-search smoke test returns the relevant BOWLIO/Amazon-DE message.
- [ ] Logs contain no `invalid_api_key`, `AuthenticationError`, `search_memory failed`, or `search_messages failed` after the restart.
