# LiteLLM proxy — API integration (team)

**DJ Treta application code is unchanged.** This document is only for **integrating other apps** (services, scripts, LangChain, etc.) with the **shared LiteLLM + Vertex** deployment on Google Cloud.

---

## 1. Endpoint

| Item | Value |
|------|--------|
| **Base URL (no path)** | `http://35.232.215.157:4000` |
| **OpenAI-compatible API root** | `http://35.232.215.157:4000/v1` |
| **Health** | `GET http://35.232.215.157:4000/health` |

**Authentication:** every request needs:

```http
Authorization: Bearer sk-litellm-vertex-serra-2026
```

(This is the LiteLLM `master_key`, not a Google API key. Vertex auth is handled **on the server** via the VM’s GCP service account.)

---

## 2. Models (aliases)

Use these **exact** `model` strings in the request body:

| Model ID (client sends this) | Backend (Vertex, on server) |
|------------------------------|-------------------------------|
| `gemini-3-flash` | `vertex_ai/gemini-2.0-flash` @ `us-central1` |
| `gemini-3.1-pro` | `vertex_ai/gemini-2.5-pro-preview-05-06` @ `us-central1` |

GCP project on the server: `fandorab2w3`.

---

## 3. Chat Completions (OpenAI-compatible)

**`POST /v1/chat/completions`**

### cURL

```bash
export LITELLM_URL="http://35.232.215.157:4000"
export LITELLM_KEY="sk-litellm-vertex-serra-2026"

curl -s "$LITELLM_URL/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 256
  }'
```

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://35.232.215.157:4000/v1",
    api_key="sk-litellm-vertex-serra-2026",
)

r = client.chat.completions.create(
    model="gemini-3-flash",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(r.choices[0].message.content)
```

### Node (openai package)

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://35.232.215.157:4000/v1",
  apiKey: "sk-litellm-vertex-serra-2026",
});

const r = await client.chat.completions.create({
  model: "gemini-3-flash",
  messages: [{ role: "user", content: "Hello!" }],
});
```

### Anything “OpenAI-compatible”

Point **`base_url`** to `http://35.232.215.157:4000/v1` and **`api_key`** to `sk-litellm-vertex-serra-2026`.

---

## 4. Health check

```bash
curl -s -H "Authorization: Bearer sk-litellm-vertex-serra-2026" \
  "http://35.232.215.157:4000/health"
```

Expect HTTP **200** when the proxy is up.

---

## 5. If requests time out from your laptop

The VM listens on **0.0.0.0:4000** and a firewall rule allows **tcp:4000** for instances tagged `litellm-proxy`. If you still get timeouts:

- Corporate network blocking non-standard ports → try another network or VPN.
- Ask a GCP admin to confirm firewall **`allow-djtreta-litellm-4000`** and your source IP / VPC path.
- As a fallback, use **SSH tunnel**:  
  `gcloud compute ssh djtreta-litellm --zone=us-central1-a --project=fandorab2w3 -- -L 4000:127.0.0.1:4000`  
  then call `http://127.0.0.1:4000` locally.

---

## 6. What this is **not**

- This is **not** a change to **DJ Treta** (`djclaw`, `agent/`, etc.). Teams integrate **directly** with this HTTP API.
- **Lyria / music generation** is not exposed here; that uses separate Vertex paths from apps that run `google.genai` — not part of this LiteLLM proxy.

---

## 7. Infra reference (read-only)

| Resource | Value |
|----------|--------|
| GCE VM name | `djtreta-litellm` |
| Zone | `us-central1-a` |
| Project | `fandorab2w3` |

---

*IP is an ephemeral public IP unless a static IP is attached; if it changes, update this doc and notify integrators.*
