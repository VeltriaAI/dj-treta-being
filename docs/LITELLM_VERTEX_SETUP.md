# DJ Treta + LiteLLM + Google Vertex AI — Team Configuration Guide

This document describes how **DJ Treta** (DJClaw) talks to **Google Vertex AI** through a local **[LiteLLM](https://github.com/BerriAI/litellm)** OpenAI-compatible proxy. Share this with anyone setting up a dev machine or a shared environment.

---

## 0. Team credentials (internal — copy-paste)

**Treat this section as confidential.** Anyone with the LiteLLM `master_key` can use your local proxy if they reach `:4000`. GCP access still requires **your** Google account or service-account JSON (`gcloud` / `GOOGLE_APPLICATION_CREDENTIALS`).


| Item                                   | Value                                                                            |
| -------------------------------------- | -------------------------------------------------------------------------------- |
| **GCP project ID**                     | `${DJTRETA_VERTEX_PROJECT}`                                                                    |
| **Vertex region (Gemini via LiteLLM)** | `us-central1`                                                                    |
| **Vertex region (Lyria / producer)**   | `global`                                                                         |
| **LiteLLM `master_key`**               | `${DJTRETA_LLM_API_KEY}`                                                   |
| **LiteLLM URL**                        | `http://localhost:4000`                                                          |
| **Env var for DJ Treta**               | `DJTRETA_LLM_API_KEY=${DJTRETA_LLM_API_KEY}` (same string as `master_key`) |


**One-line env (LLM + optional relay token if you use one):**

```bash
export DJTRETA_LLM_API_KEY=${DJTRETA_LLM_API_KEY}
# Optional — only if you use relay; get value from team lead / vault:
# export DJTRETA_RELAY_TOKEN=
```

**Full `litellm_config.yaml` (repo root) — current team config:**

```yaml
model_list:
  - model_name: gemini-3-flash
    litellm_params:
      model: vertex_ai/gemini-2.0-flash
      vertex_project: ${DJTRETA_VERTEX_PROJECT}
      vertex_location: us-central1

  - model_name: gemini-3.1-pro
    litellm_params:
      model: vertex_ai/gemini-2.5-pro-preview-05-06
      vertex_project: ${DJTRETA_VERTEX_PROJECT}
      vertex_location: us-central1

general_settings:
  master_key: ${DJTRETA_LLM_API_KEY}
```

`**config.yaml` snippets that match this setup:**

```yaml
llm:
  model: "openai/gemini-3-flash"
  api_base: "http://localhost:4000"
  api_key: ""
  temperature: 0.7
  timeout: 30
```

```yaml
producer:
  enabled: true
  model: "lyria-3-pro-preview"
  vertex_project: "${DJTRETA_VERTEX_PROJECT}"
  vertex_location: "global"
  default_duration_seconds: 180
  genre_dir: "ai-generated"
```

**Google auth (each developer still needs Vertex access on `${DJTRETA_VERTEX_PROJECT}`):**

```bash
gcloud config set project ${DJTRETA_VERTEX_PROJECT}
gcloud auth application-default login
```

---

## 0.1 Vertex AI (Google Cloud) — team setup

Vertex does **not** use a separate “API key” string. Access is always:

- **User:** Application Default Credentials after `gcloud auth application-default login`, or  
- **Machine/CI:** `GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json`

Everything below is for project `**${DJTRETA_VERTEX_PROJECT}`** (same as `litellm_config.yaml` and `config.yaml` `producer`).

### Project & console (bookmark for the team)


| What                  | Value / link                                                                                                            |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| **GCP project ID**    | `${DJTRETA_VERTEX_PROJECT}`                                                                                                           |
| **Project dashboard** | [console.cloud.google.com — project `${DJTRETA_VERTEX_PROJECT}](https://console.cloud.google.com/home/dashboard?project=${DJTRETA_VERTEX_PROJECT})` |
| **Vertex AI**         | [Vertex AI — `${DJTRETA_VERTEX_PROJECT}](https://console.cloud.google.com/vertex-ai?project=${DJTRETA_VERTEX_PROJECT})`                             |
| **IAM**               | [IAM — `${DJTRETA_VERTEX_PROJECT}](https://console.cloud.google.com/iam-admin/iam?project=${DJTRETA_VERTEX_PROJECT})`                               |
| **APIs & services**   | [Enabled APIs — `${DJTRETA_VERTEX_PROJECT}](https://console.cloud.google.com/apis/dashboard?project=${DJTRETA_VERTEX_PROJECT})`                     |


### Regions we use (don’t mix these up)


| Use case                                      | Config file                | `vertex_project` | **Region / location** | Vertex model IDs (examples)                        |
| --------------------------------------------- | -------------------------- | ---------------- | --------------------- | -------------------------------------------------- |
| **Chat / DJ / Being / Planner** (via LiteLLM) | `litellm_config.yaml`      | `${DJTRETA_VERTEX_PROJECT}`    | `**us-central1`**     | `gemini-2.0-flash`, `gemini-2.5-pro-preview-05-06` |
| **Lyria music generation**                    | `config.yaml` → `producer` | `${DJTRETA_VERTEX_PROJECT}`    | `**global`**          | `lyria-3-pro-preview`, `lyria-3-clip-preview`      |


LiteLLM passes `vertex_location` per model; Lyria uses `producer.vertex_location` in code (`agent/tools/generation.py`).

### APIs to enable (admin / once per project)

Someone with **Owner** or **Service Usage Admin** should ensure these are **enabled** on `${DJTRETA_VERTEX_PROJECT}`:


| API                                             | Purpose                               |
| ----------------------------------------------- | ------------------------------------- |
| **Vertex AI API** (`aiplatform.googleapis.com`) | Gemini via LiteLLM + Vertex SDK paths |


**Enable from CLI (copy-paste):**

```bash
gcloud config set project ${DJTRETA_VERTEX_PROJECT}
gcloud services enable aiplatform.googleapis.com --project=${DJTRETA_VERTEX_PROJECT}
```

If Lyria / media APIs fail with “API not enabled”, open [API Library](https://console.cloud.google.com/apis/library?project=${DJTRETA_VERTEX_PROJECT}) and enable anything Google’s error message names (often still under the same project’s Vertex / Generative AI surface).

### Billing

Vertex Gemini and Lyria are **paid** once free tiers are exceeded. **Billing must be linked** to `${DJTRETA_VERTEX_PROJECT}` for production-style use: [Billing](https://console.cloud.google.com/billing/linkedaccount?project=${DJTRETA_VERTEX_PROJECT}).

### IAM — what each developer needs

Ask an admin to grant your **Google account** (or a **service account** you use) on project `${DJTRETA_VERTEX_PROJECT}`:


| Role               | ID                      | Why                                                                                      |
| ------------------ | ----------------------- | ---------------------------------------------------------------------------------------- |
| **Vertex AI User** | `roles/aiplatform.user` | Call Gemini + generative features from LiteLLM and from `google.genai` (minimum for dev) |


Optional stricter setups: custom role with `aiplatform.endpoints.predict` only — only if your org requires it.

**Console:** IAM → Grant access → principal = user email → role = *Vertex AI User*.

**gcloud (admin):**

```bash
# Replace TEAM_MEMBER@company.com
gcloud projects add-iam-policy-binding ${DJTRETA_VERTEX_PROJECT} \
  --member="user:TEAM_MEMBER@company.com" \
  --role="roles/aiplatform.user"
```

### Service account (for servers / CI / headless)

1. Create SA: e.g. `dj-treta-vertex@${DJTRETA_VERTEX_PROJECT}.iam.gserviceaccount.com`
2. Grant `roles/aiplatform.user` on `${DJTRETA_VERTEX_PROJECT}`
3. Create JSON key (or use Workload Identity on GKE) — **store JSON in vault, not in git**
4. On the machine:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/dj-treta-vertex-sa.json
export GOOGLE_CLOUD_PROJECT=${DJTRETA_VERTEX_PROJECT}   # optional; project is also in YAML
```

LiteLLM (when run locally) and DJ Treta both read **the same** ADC / JSON for Vertex.

### Quick sanity checks

```bash
gcloud config set project ${DJTRETA_VERTEX_PROJECT}
gcloud auth application-default login

# Vertex AI API enabled on the project?
gcloud services list --enabled --project=${DJTRETA_VERTEX_PROJECT} --filter="name:aiplatform.googleapis.com"

# Can you read the project? (403 = no access)
gcloud projects describe ${DJTRETA_VERTEX_PROJECT} --format="value(projectId)"
```

If you get **403** or **API not enabled** → fix IAM (`roles/aiplatform.user`) and run `gcloud services enable aiplatform.googleapis.com` as above.

**Note:** `gcloud ai models list` only shows **custom** models in the registry; Gemini publisher models may show **0 items** even when everything works — rely on DJ Treta + LiteLLM working, not an empty model list.

### Env summary (Vertex side — no secret key)


| Variable                         | Required?             | Meaning                                                               |
| -------------------------------- | --------------------- | --------------------------------------------------------------------- |
| `GOOGLE_APPLICATION_CREDENTIALS` | If not using user ADC | Path to service-account JSON                                          |
| `GOOGLE_CLOUD_PROJECT`           | Optional              | Defaults often inferred; our YAML already sets `${DJTRETA_VERTEX_PROJECT}` per call |


There is **no** `VERTEX_API_KEY` in this repo — authentication is always GCP identity (user or SA).

---

## 1. Architecture (what talks to what)

```
┌─────────────────────────────────────────────────────────────────┐
│  DJ Treta daemon (python -m agent / djclaw start)               │
│  Google ADK agents use LiteLlm(model, api_base, api_key)        │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP OpenAI-compatible API
                             │ (e.g. http://localhost:4000/v1)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  LiteLLM Proxy (local, port 4000)                                │
│  Reads: repo-root litellm_config.yaml                            │
│  Maps logical model names → vertex_ai/...                        │
└────────────────────────────┬────────────────────────────────────┘
                             │ Vertex AI API
                             │ (uses Application Default Credentials)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Google Cloud Vertex AI (Gemini models)                          │
│  Project + region from litellm_config per model                  │
└─────────────────────────────────────────────────────────────────┘
```

**Separate path — Lyria / music generation (not via LiteLLM for the main call):**

The `generate_track` tool uses `google.genai` with `vertexai=True` and `config.producer.vertex_*` from `config.yaml`. For this team that is project `**${DJTRETA_VERTEX_PROJECT}`**, location `**global**` for Lyria. Auth is **Application Default Credentials** (not the LiteLLM `master_key`).

---

## 2. Prerequisites


| Requirement              | Notes                                                                                                  |
| ------------------------ | ------------------------------------------------------------------------------------------------------ |
| **Python**               | 3.10+ (project uses `uv`; see `pyproject.toml`)                                                        |
| **Google Cloud project** | Vertex AI enabled; billing enabled if required for your models                                         |
| **IAM**                  | Account or service account with **Vertex AI User** (or broader for dev)                                |
| **Local auth**           | `gcloud auth application-default login` **or** `GOOGLE_APPLICATION_CREDENTIALS` pointing to a JSON key |
| **APIs**                 | Enable **Vertex AI API** on the project                                                                |


Optional: **Gemini / generative models** in the region you use (e.g. `us-central1`).

---

## 3. Files you must configure

### 3.1 `litellm_config.yaml` (repository root)

DJ Treta **auto-starts** LiteLLM if nothing is listening on `config.llm.api_base` (default `http://localhost:4000`). It runs:

```text
.venv/bin/litellm --config litellm_config.yaml --port 4000
```

Logs: `/tmp/litellm-local.log`

**Current team file** (same as §0; keep in sync):

```yaml
model_list:
  - model_name: gemini-3-flash
    litellm_params:
      model: vertex_ai/gemini-2.0-flash
      vertex_project: ${DJTRETA_VERTEX_PROJECT}
      vertex_location: us-central1

  - model_name: gemini-3.1-pro
    litellm_params:
      model: vertex_ai/gemini-2.5-pro-preview-05-06
      vertex_project: ${DJTRETA_VERTEX_PROJECT}
      vertex_location: us-central1

general_settings:
  master_key: ${DJTRETA_LLM_API_KEY}
```

**Fields:**


| Field                         | Purpose                                                                                                                        |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `model_name`                  | **Alias** LiteLLM exposes to clients. Must match the logical name you use from DJ Treta (see §4).                              |
| `litellm_params.model`        | Provider string: `vertex_ai/<Gemini model id>` per [LiteLLM Vertex docs](https://docs.litellm.ai/docs/providers/vertex).       |
| `vertex_project`              | GCP project ID.                                                                                                                |
| `vertex_location`             | Region (e.g. `us-central1`). Must support the chosen Gemini model.                                                             |
| `general_settings.master_key` | **LiteLLM API key**. Clients send `Authorization: Bearer <master_key>` to the proxy. **Treat as a secret** — rotate if leaked. |


**Notes:**

- Model IDs (`gemini-2.0-flash`, etc.) change over time; update when Google deprecates or renames models.
- You can add more entries to `model_list` for different speeds (flash vs pro).

---

### 3.2 `config.yaml` — `llm` section

DJ Treta reads this via `agent/config.py`. Relevant block:

```yaml
llm:
  model: "openai/gemini-3-flash"
  api_base: "http://localhost:4000"
  api_key: ""
  temperature: 0.7
  timeout: 30
```

**Fields:**


| Field      | Purpose                                                                                                                                                                                             |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `model`    | **OpenAI-compatible** id passed to ADK’s `LiteLlm`. Format is `openai/<alias>` where `<alias>` matches `**model_name`** in `litellm_config.yaml` (e.g. `gemini-3-flash` → `openai/gemini-3-flash`). |
| `api_base` | LiteLLM base URL **without** `/v1` (same as LiteLLM `--port`).                                                                                                                                      |
| `api_key`  | Prefer **environment variable** (below). If set in YAML, it overrides empty env.                                                                                                                    |


**Environment overrides (recommended for teams):**


| Variable              | Effect                                                      |
| --------------------- | ----------------------------------------------------------- |
| `DJTRETA_LLM_API_KEY` | If set, used as `llm.api_key` (highest priority in loader). |
| `LLM_API_KEY`         | Same, if `DJTRETA_LLM_API_KEY` is unset.                    |


Set this to the **same value** as `general_settings.master_key` in `litellm_config.yaml` so every HTTP call to LiteLLM includes:

```http
Authorization: Bearer <master_key>
```

Loader logic: `agent/config.py` (`load_config`).

---

### 3.3 `config.yaml` — `producer` (Lyria / Vertex for music generation)

Used by `agent/tools/generation.py` (`google.genai` + `vertexai=True`), **not** routed through LiteLLM:

```yaml
producer:
  enabled: true
  model: "lyria-3-pro-preview"
  vertex_project: "${DJTRETA_VERTEX_PROJECT}"
  vertex_location: "global"
  default_duration_seconds: 180
  genre_dir: "ai-generated"
```


| Field             | Purpose                                                                |
| ----------------- | ---------------------------------------------------------------------- |
| `vertex_project`  | GCP project for Lyria / Vertex generative media APIs.                  |
| `vertex_location` | Often `global` for some media models — follow Google’s docs for Lyria. |


Requires the same **Application Default Credentials** as Vertex (service account or user ADC).

---

### 3.4 `.env` (optional, repo root)

`agent/config.py` loads `.env` if present (`setdefault` — does not override existing shell env).

**Team `.env` example** (repo root; file is loaded by `agent/config.py`):

```bash
# LiteLLM — must match general_settings.master_key in litellm_config.yaml
DJTRETA_LLM_API_KEY=${DJTRETA_LLM_API_KEY}

# Optional: relay to dj.treta.life (value from team — not in committed config.yaml)
# DJTRETA_RELAY_TOKEN=

# Optional: service account instead of gcloud user ADC
# GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-${DJTRETA_VERTEX_PROJECT}.json
```

Do **not** commit `.env` to a public repo; keep it local or in an internal vault.

---

## 4. Model name mapping (why `openai/gemini-3-flash`?)

- **LiteLLM** exposes aliases under `model_list[].model_name` (e.g. `gemini-3-flash`).
- **ADK** uses `google.adk.models.lite_llm.LiteLlm` with a model string like `openai/<alias>` so the client speaks **OpenAI-compatible** paths to LiteLLM.
- The underlying **Vertex** model is whatever you set in `litellm_params.model` (e.g. `vertex_ai/gemini-2.0-flash`).

So the name `gemini-3-flash` is a **project-local alias**; it does not have to match Google’s public model name string exactly.

---

## 5. Google Cloud authentication (Vertex)

LiteLLM’s Vertex backend uses **Google auth from the environment**:

1. **Development (common):**
  ```bash
   gcloud config set project ${DJTRETA_VERTEX_PROJECT}
   gcloud auth application-default login
  ```
2. **CI / headless:** Create a service account with Vertex permissions, download JSON, set:
  ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
  ```

Ensure the **project** in `litellm_config.yaml` (`vertex_project`) matches the project your credentials can access.

---

## 6. Operations

### Start order

1. (Optional) Start LiteLLM manually, or let **DJ Treta** start it via `_ensure_litellm` in `agent/main.py`.
2. `djclaw start` or `python -m agent`.

### Health checks


| Check    | Command / URL                                                                                                        |
| -------- | -------------------------------------------------------------------------------------------------------------------- |
| LiteLLM  | `curl -s -o /dev/null -w "%{http_code}" http://localhost:4000/health` → expect `200` or `401` (401 if key required). |
| With key | `curl -H "Authorization: Bearer $DJTRETA_LLM_API_KEY" http://localhost:4000/health`                                  |


### Logs


| Component              | Path                       |
| ---------------------- | -------------------------- |
| LiteLLM (auto-started) | `/tmp/litellm-local.log`   |
| DJ Treta daemon        | `/tmp/dj-treta-daemon.log` |


### Stop everything

```bash
djclaw kill   # kills daemon, Mixxx, and litellm processes (see cli.py)
```

---

## 7. Security checklist for teams

- This doc contains **live team secrets** (§0). Store the file in **internal** channels only; do not publish publicly.
- Rotate `**master_key`** (`${DJTRETA_LLM_API_KEY}`) if it leaks outside the team.
- Prefer **per-developer** ADC on `${DJTRETA_VERTEX_PROJECT}` or a **team service account** with least-privilege Vertex roles.
- Restrict GCP IAM so only needed people can use project `${DJTRETA_VERTEX_PROJECT}`.

---

## 8. Troubleshooting


| Symptom                    | What to check                                                                                                         |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `401` from LiteLLM         | `DJTRETA_LLM_API_KEY` / `LLM_API_KEY` matches `master_key`.                                                           |
| Vertex permission errors   | APIs enabled, project ID, region, ADC / `GOOGLE_APPLICATION_CREDENTIALS`.                                             |
| `model not found`          | `config.yaml` `llm.model` alias matches `model_name` in `litellm_config.yaml`; LiteLLM restarted after config change. |
| LiteLLM won’t start        | `litellm` on PATH or `.venv/bin/litellm`; read `/tmp/litellm-local.log`.                                              |
| Lyria fails but chat works | Producer block `vertex_project` / `vertex_location`; Lyria model availability in that region.                         |


---

## 8.1 Optional: Shared LiteLLM on a VM (easier for the team)

**Idea:** Run **one** LiteLLM process on a small GCP VM (or any server), put `**litellm_config.yaml`** + **Vertex service-account JSON** only on that machine, and give the team **URL + `master_key`**. Each developer’s laptop only needs:

- `config.yaml` → `llm.api_base: "http://VM_INTERNAL_IP:4000"` (or `https://litellm.your-internal-domain` behind nginx/Caddy)
- `DJTRETA_LLM_API_KEY` = same `master_key` as on the server

**Why it’s easier**


| Local LiteLLM (every dev)                                           | Shared LiteLLM (one VM)                                       |
| ------------------------------------------------------------------- | ------------------------------------------------------------- |
| Har machine pe `gcloud auth application-default login` + Vertex IAM | Sirf **VM** pe SA / ADC — team ko GCP console ki zaroorat kam |
| Har restart pe local LiteLLM                                        | Ek jagah upgrade, logs, monitoring                            |
| Credentials spread                                                  | Credentials **sirf VM** pe                                    |


**DJ Treta behaviour:** `agent/main.py` pehle `config.llm.api_base` check karta hai — agar wahan already LiteLLM respond kar raha hai, **local LiteLLM auto-start nahi hota**. Toh VM chal raha ho to laptop par extra process nahi.

**Security (zaroori)**

- Port 4000 ko **internet pe open mat karo** — office IP, **Cloud VPN**, ya **Identity-Aware Proxy / internal LB**.
- `**master_key`** ko API key jaisa treat karo — rotate karo agar leak ho.
- Production-style: **HTTPS** (reverse proxy) + optional IP allowlist.

**Ek cheez yaad rakho (Lyria)**

- **Gemini (chat / DJ / planner)** → LiteLLM VM se ho sakta hai (sirf `api_base` point karo).
- `**generate_track` (Lyria)** → `agent/tools/generation.py` **direct** `google.genai` + Vertex use karta hai, **LiteLLM ke through nahi**. Jo machine pe **DJ Treta daemon** chal raha hai, **usko** bhi Vertex credentials chahiye (ADC ya `GOOGLE_APPLICATION_CREDENTIALS`), warna Lyria fail ho sakta hai jabki baaki sab chale.

**Options for Lyria:** (a) har developer ko `gcloud auth application-default login` on laptop, ya (b) DJ Treta sirf server/VM pe chalao jahan SA lagi ho, ya (c) future mein code change se Lyria bhi HTTP proxy se ho — abhi repo mein aisa nahi hai.

**VM pe minimal run (example)**

```bash
# On the VM — service account JSON at /opt/djtreta/vertex-sa.json
export GOOGLE_APPLICATION_CREDENTIALS=/opt/djtreta/vertex-sa.json
export GOOGLE_CLOUD_PROJECT=${DJTRETA_VERTEX_PROJECT}

litellm --config /opt/djtreta/litellm_config.yaml --host 0.0.0.0 --port 4000
```

Use `**--host 0.0.0.0**` only if clients other than `localhost` se connect karenge; firewall se restrict karo.

---

## 9. Code references (for maintainers)


| Topic                | Location                                                            |
| -------------------- | ------------------------------------------------------------------- |
| LiteLLM auto-start   | `agent/main.py` — `_ensure_litellm`, `_litellm_reachable`           |
| Config load + env    | `agent/config.py` — `load_config`                                   |
| ADK model wiring     | `agent/agents.py` — `LiteLlm(model=..., api_key=..., api_base=...)` |
| Lyria / Vertex genai | `agent/tools/generation.py` — `genai.Client(vertexai=True, ...)`    |
| Kill litellm         | `cli.py` — `pkill` for `litellm` on `djclaw kill`                   |


---

## 10. Example: minimal end-to-end env

```bash
gcloud config set project ${DJTRETA_VERTEX_PROJECT}
gcloud auth application-default login

export DJTRETA_LLM_API_KEY=${DJTRETA_LLM_API_KEY}

cd /path/to/dj-treta
uv sync
djclaw start "melodic techno"
```

---

*Document version: matches DJ Treta repo (`config.yaml`, `litellm_config.yaml`). **§0 contains shared team keys** — internal use only. Rotate keys if exposed. Update model IDs when GCP deprecates models.*