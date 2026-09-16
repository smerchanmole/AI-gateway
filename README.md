# IA Gateway

IA Gateway is a lightweight control plane for [LiteLLM](https://docs.litellm.ai/). It exposes one OpenAI-compatible public endpoint while providing a browser dashboard for model discovery, configuration, guardrails, health checks, tests, metrics, and request logs.

The same Python application runs locally and as a Cloudera AI Workbench application. It does not require Docker, Nginx, PostgreSQL, or an external database.

![IA Gateway single-port architecture](static/ia-gateway-architecture.svg)

## What it provides

- One published port for both the dashboard and the OpenAI-compatible API.
- A Python streaming proxy based on `aiohttp`; no Nginx dependency.
- LiteLLM lifecycle management from the dashboard.
- Guided model CRUD plus an advanced `config.yaml` editor.
- YAML validation, download backup, and validated import/restore.
- OpenAI-compatible, Ollama, Cloudera AI Inference, and Cloudera AI Workbench models.
- Optional pre-request guardrail using any configured chat model supported by LiteLLM.
- Cloudera model discovery, endpoint probing, and token generation/renewal.
- SQLite-only persistence for Cloudera credentials, dynamic tokens, and structured request logs.
- Daily KPIs, hourly activity, origin/provider IPs, TTFT, duration, tokens, and Excel export.
- Spanish, English, and Italian dashboard languages, persisted per browser.
- Built-in administrator login with forced password change on first access.

## Architecture

```mermaid
flowchart LR
    C["Browser / application / agent"] -->|"HTTPS · one published port"| E["Python edge proxy"]
    E -->|"/ and /api/*"| D["FastAPI dashboard<br/>127.0.0.1:18080"]
    E -->|"/v1/* · streaming"| L["LiteLLM Proxy<br/>127.0.0.1:14000"]
    D --> Y["config.yaml"]
    D --> S[("SQLite")]
    L --> S
    L --> P["Cloudera · OpenAI · Ollama · other providers"]
```

The public listener routes by path:

| Public path | Internal target | Purpose |
|---|---|---|
| `/`, `/static/*`, `/api/*` | FastAPI on `127.0.0.1:18080` | Dashboard, authentication, configuration, discovery, and logs |
| `/v1/*` | LiteLLM on `127.0.0.1:14000` | OpenAI-compatible inference, including streaming |

Only the edge port is published. The dashboard and LiteLLM ports remain on loopback and are not exposed externally.

### Port selection

| Process | Environment variable | Local default |
|---|---|---:|
| Public Python edge proxy | `IA_GATEWAY_PORT`; otherwise the first defined value of `CDSW_READONLY_PORT`, `CDSW_APP_PORT`, or `CDSW_PUBLIC_PORT` | `8090` |
| Internal FastAPI dashboard | `IA_GATEWAY_DASHBOARD_PORT` | `18080` |
| Internal LiteLLM Proxy | `IA_GATEWAY_LITELLM_PORT` | `14000` |

In Cloudera, the platform terminates TLS and forwards traffic to its assigned application port. Locally, IA Gateway creates a self-signed development certificate and serves HTTPS on `8090` by default.

### Persistence and restart rules

| Data | Storage | Restart needed? |
|---|---|---|
| Model topology, aliases, fallbacks, and guardrail selection | `config.yaml` | Yes, LiteLLM must reload its routing table |
| Cloudera connections and secrets | `runtime/cloudera.sqlite3` | No |
| Renewed CDP tokens and model-specific tokens | `runtime/cloudera.sqlite3` | No; the LiteLLM callback reads the current value dynamically |
| Structured request logs | SQLite files under `runtime/` | No |
| Effective LiteLLM configuration | `runtime/active_config.yaml` | Generated automatically; do not edit |

SQLite is part of the Python standard library. There is no database server to install or operate.

## Cloudera AI installation

### 1. Download the project from GitHub

From a Cloudera project terminal:

```bash
git clone https://github.com/smerchanmole/AI-gateway.git
cd AI-gateway
```

If the project is already cloned and `config.yaml` contains local dashboard changes, preserve it before pulling:

```bash
git stash push -m "local IA Gateway config" -- config.yaml
git pull origin main
git stash pop
```

Alternatively, download a YAML backup from **Configuration → Export or restore config.yaml**, pull the repository, and import the backup afterward.

### 2. Create the Cloudera application

Create a Cloudera AI Workbench application with:

- **Launcher:** `app.py`
- **Platform authentication:** disabled, if clients must call `/v1/*` directly without a Cloudera browser session
- **Runtime:** Python 3.11 or newer
- **Port:** let Cloudera inject the assigned `CDSW_*_PORT`

`app.py` creates a private `.venv`, installs `requirements.txt`, validates it with `pip check`, and starts the application. The first launch therefore takes longer and requires access to the configured Python package index.

Disabling Cloudera platform authentication exposes the application URL to the network policy applied to that workspace. The dashboard still has its own administrator login. The `/v1/*` inference API currently does not require the dashboard password or a LiteLLM master key, so protect the URL with the appropriate Cloudera/network access policy before production use.

### 3. Sign in and change the initial password

Open the application URL and use:

```text
username: admin
password: admin
```

The dashboard forces a password change before allowing access. Use at least 12 characters with uppercase, lowercase, a number, and a symbol.

### 4. Register at least one Cloudera connection

Open **Configuration → Cloudera**. Two different URLs are involved:

1. **Base URL** is the origin that hosts Model Endpoints and the discovery API. You may paste a full endpoint URL; IA Gateway keeps only the scheme and domain.
2. **Renewal URL** is the IAM or Knox endpoint used to generate/renew the CDP workload token.

#### Cloudera Cloud example

| Field | Example |
|---|---|
| Service | `Cloudera AI Inference` |
| Deployment | `Cloud` |
| Full model endpoint URL | `https://ml-64288d82-5dd.go01-dem.ylcu-atmi.cloudera.site/namespaces/serving-default/endpoints/my-model/v1` |
| Base URL entered in the form | `https://ml-64288d82-5dd.go01-dem.ylcu-atmi.cloudera.site` |
| Discovery API used by IA Gateway | `https://ml-64288d82-5dd.go01-dem.ylcu-atmi.cloudera.site/api/v1alpha1/listEndpoints` |
| Renewal URL | `https://iamapi.us-west-1.altus.cloudera.com` |
| Workload name | for example `DE` |
| Credentials | `CDP_ACCESS_KEY_ID` and `CDP_PRIVATE_KEY` from a CDP machine user/API access key |

Do not use `https://console.cdp.cloudera.com` as the Base URL. The historic `altus.cloudera.com` IAM hostname is intentional; `iamapi.us-west-1.cdp.cloudera.com` does not resolve.

The Cloudera application runtime must have outbound DNS and HTTPS access to both the Model Endpoint domain and the IAM hostname. A DNS failure cannot be fixed by application code; the workspace egress policy must allow it.

#### Cloudera on-premises example

This form currently covers **Cloudera AI Inference only**. Workbench authentication is intentionally left for a later phase. Select both the Cloudera AI service pack and the exact CDP Base Runtime so the dashboard can enforce Cloudera's compatibility matrix:

| Cloudera AI | Supported Runtime selections |
|---|---|
| 1.5.5 SP2 | 7.1.9 SP1, 7.3.1 |
| 1.5.5 SP2 CHF1 | 7.1.9 SP1, 7.3.1, 7.3.2 |
| 1.5.5 SP3 | 7.1.9 SP2, 7.3.1, 7.3.2 |

| Field | Example |
|---|---|
| Service | `Cloudera AI Inference` |
| Deployment | `On-premise` |
| Full model endpoint URL | `https://ml.company.example/namespaces/serving-default/endpoints/my-model/openai/v1` |
| Base URL entered in the form | `https://ml.company.example` |
| Discovery API used by IA Gateway | `https://ml.company.example/api/v1alpha1/listEndpoints` for AI Inference |
| Automatic CDP token URL | Management Console/Control Plane origin, for example `https://console-cdp.apps.company.example` |
| Automatic credentials | `CDP_ACCESS_KEY_ID` and `CDP_PRIVATE_KEY` from a user or least-privilege machine user, plus workload name such as `DE` |

The on-premises panel offers two deliberately separate modes:

1. **Existing credential** accepts a UMS `CDP_TOKEN`, copied from Model Endpoint Details or produced by `cdp iam generate-workload-auth-token --workload-name DE`. A Knox API key is also accepted only for Cloudera AI 1.5.5 SP3 with Runtime 7.1.9 SP2 or 7.3.2, the combinations certified by Cloudera.
2. **Automatic UMS `CDP_TOKEN`** uses the private Management Console origin. CDP CLI builds the `/api/v1/iam/generateWorkloadAuthToken` operation, signs it with the access key/private key, generates the first token immediately, and replaces it early without restarting LiteLLM.

Private Control Planes often use a corporate or self-signed CA that is not present in the WebApp container. For those connections select **Private CA (PEM)** and paste the root CA plus any intermediate certificates; IA Gateway stores the bundle locally and invokes CDP CLI with `--ca-bundle`. Trusting the certificate in a workstation browser does not install it in the Cloudera WebApp. **Do not verify (insecure)** is available only for short diagnostic tests and should not be used for automatic renewal in production.

A general platform username/password is **not** a documented non-interactive credential for `iam generate-workload-auth-token`. A Basic-authenticated Knox token endpoint issues a different service JWT and must not be presented as a UMS `CDP_TOKEN`; consequently this CAI Inference form does not offer that misleading combination.

`cdp iam set-authentication-policy --workload-auth-token-expiration-sec ...` only changes the account-wide lifetime applied when UMS issues future tokens; it does not create or refresh one. IA Gateway therefore does not need to lengthen tokens to 90 days: it calls `generate-workload-auth-token` again inside the early-renewal window and atomically replaces the current value. Changing the global policy remains an explicit administrator decision because longer JWTs cannot be revoked individually.

Do not paste the interactive page ending in `/token-generation/index.html`, a Knox route, or a generic Knox Gateway JWT whose Target Base URL is `/gateway/cdp-proxy-token` into the IAM URL field. AI Inference expects a UMS CDP JWT or its specifically configured Knox API key support.

Knox API keys require an administrator to configure `cdp-preauth` in `conf/cdp-resources.xml` for the Data Lake Knox Gateway Default Group, refresh stale Knox configurations, and verify the topology in Knox Admin UI. If a UMS JWT works but a Knox API key returns HTTP 401, verify this prerequisite and that the complete generated key—not its identifier—was copied.

Credential lifecycle is always reported explicitly. JWT expiry is read from the `exp` claim. UMS tokens are regenerated in advance when all IAM generation fields are present. The default lead is up to seven days but capped at 25% of token lifetime, so a normal one-hour UMS token rotates roughly 15 minutes early; `CDP_RENEWAL_LEAD_SECONDS` can lower that maximum. The supervisor retries every minute and the LiteLLM callback reads the replacement atomically from SQLite on every request.

Opaque Knox API keys do not expose an expiry claim and cannot be regenerated by IA Gateway. Enter their administrative expiry date (for example, 90 days) so the dashboard calculates an advance rotation date. For uninterrupted operation, prefer the renewable UMS flow. If a model-specific JWT expires while the connection has a valid renewed CDP token, IA Gateway automatically falls back to the connection credential.

You can either paste an existing CAI credential or select automatic UMS generation and leave the token blank. IA Gateway then requests the initial token and subsequently replaces it in the background. Secret values are stored in SQLite with file mode `0600` and are never returned to the browser.

### 5. Discover and add models

1. Save the Cloudera connection.
2. Select **Discover models**.
3. Review deployment state, credential state, endpoint URL, and protocol compatibility.
4. Use **Test access** to validate the endpoint and credential.
5. Select **Prepare draft** on each required model.
6. Review the LiteLLM name, API base, and credential reference.
7. Select **Add model** and apply the pending LiteLLM restart.

A newly added model requires a LiteLLM restart because LiteLLM must rebuild its router. Token generation and renewal do not require a restart.

AI Gateway reports the HTTP protocol separately from the serving stack. An endpoint can therefore be OpenAI-compatible while running NVIDIA NIM, vLLM, or Triton underneath. For asymmetric NVIDIA NIM embedding models, discovery exposes two explicit drafts: `-query` for user questions and `-passage` for document ingestion. Configure both aliases when the RAG client can select them independently; using the query role to index documents can materially reduce retrieval quality. Triton/KServe v2 endpoints are checked through their readiness route until a tensor schema-specific adapter is configured.

### 6. Configure an optional guardrail

In **Configuration → Optional pre-request guardrail**:

1. Choose any configured chat model that can classify the conversation. It may be hosted by Ollama, Cloudera, OpenAI, or another OpenAI-compatible provider.
2. Enable **Apply before chat models**.
3. Choose a policy:
   - **Permissive:** record a warning and continue to the requested model.
   - **Restricted:** block the request when the guardrail marks it unsafe.
4. Under **Do not apply guardrail to**, select any chat aliases that must bypass classification.

Embedding requests are always excluded automatically: they carry vector inputs rather than a chat conversation and do not benefit from the current conversational classifier. Explicit exclusions are preserved when an alias is renamed and removed when that model is deleted. The guardrail model must return a classification that IA Gateway can interpret. Llama Guard models are a natural fit, but the transport is provider-independent.

## Local installation

Requirements:

- Python 3.11 or newer
- Internet or an internal Python package mirror on first launch
- Optional: Ollama, if local Ollama models are configured

```bash
git clone https://github.com/smerchanmole/AI-gateway.git
cd AI-gateway
python3 app.py
```

The bootstrap creates `.venv` and installs dependencies automatically. Then open:

```text
https://127.0.0.1:8090
```

The certificate is self-signed for local development, so the browser or `curl` needs an explicit exception. You may also prepare the environment manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Optional `.env` example:

```dotenv
OPENAI_API_KEY=sk-your-key
IA_GATEWAY_PORT=8090
IA_GATEWAY_DASHBOARD_PORT=18080
IA_GATEWAY_LITELLM_PORT=14000
CDP_RENEWAL_TIMEOUT_SECONDS=60
```

The three ports must be distinct. Only `IA_GATEWAY_PORT` is externally accessible.

## Calling the API

### List models

Local:

```bash
curl -k https://127.0.0.1:8090/v1/models
```

Cloudera:

```bash
curl https://your-app.your-workspace.cloudera.site/v1/models
```

### Chat completion

```bash
curl -k https://127.0.0.1:8090/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "your-model-alias",
    "messages": [{"role": "user", "content": "Hello, how are you?"}],
    "stream": false
  }'
```

For Cloudera, replace the local URL and remove `-k`:

```bash
curl https://your-app.your-workspace.cloudera.site/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"your-model-alias","messages":[{"role":"user","content":"Hello"}]}'
```

### Streaming

```bash
curl -k -N https://127.0.0.1:8090/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "your-model-alias",
    "messages": [{"role": "user", "content": "Explain SQLite briefly."}],
    "stream": true
  }'
```

### Multimodal requests

Multimodal payloads pass through the same `/v1/chat/completions` route. They work when LiteLLM supports the provider/model combination and the target model accepts images:

```bash
curl -k https://127.0.0.1:8090/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "your-vision-alias",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "Describe this image"},
        {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
      ]
    }]
  }'
```

The public proxy accepts streaming request bodies and responses and allows payloads up to 100 MiB. Base64 images increase request size significantly; hosted URLs are usually more efficient.

## Model configuration

`config.yaml` is the versionable source of truth. A minimal example is:

```yaml
model_list:
  - model_name: my-model
    litellm_params:
      model: openai/provider/model-id
      api_base: https://provider.example/v1
      api_key: os.environ/PROVIDER_API_KEY
      drop_params: true

dashboard_settings:
  guardrail:
    enabled: false
    model: ""
    policy: warn
    timeout: 8
    excluded_models: []

router_settings: {}
```

Never put a secret value directly in `config.yaml`. Use an environment reference such as `os.environ/OPENAI_API_KEY`, or use the Cloudera credential form so the value is stored in SQLite.

The advanced editor validates YAML structure, duplicate aliases, fallbacks, guardrail references, and environment references before saving. Export/import is available at the bottom of the Configuration page. Imports are validated before atomically replacing the configuration.

### Per-model capacity and generation profiles

The guided editor separates parameters by responsibility so a request option is not confused with a deployment limit:

| Group | Stored as | Purpose |
|---|---|---|
| Total context window and maximum output capacity | `model_info.max_input_tokens` / `max_output_tokens` plus dashboard metadata | Declares the capacity LiteLLM should use for routing. It does not increase a vLLM `--max-model-len`, NIM engine limit, or Triton model configuration. |
| Default output, temperature, Top P, penalties, seed and stop sequences | `litellm_params` | Default OpenAI-compatible generation behaviour for this deployment. |
| Top K, Min P and repetition penalty | `litellm_params.extra_body` | Provider extensions used only by vLLM, NIM, Ollama and the Workbench adapter. |
| Reasoning mode | standard `reasoning_effort`, plus backend-specific `extra_body` | Workbench receives `enable_thinking`; vLLM/NIM receive `chat_template_kwargs.enable_thinking`. Unsupported backends do not receive this extension. |
| Timeout, retries and maximum parallel requests | `litellm_params` | Operational protection and per-deployment router concurrency. The Workbench-oriented presets use zero retries to avoid overlapping requests leaving a replica busy. |

The `Cloudera AI 1.5.5 SP3` compatibility profile enforces the current Workbench adapter's safe default-output ceiling of 512 tokens. This ceiling is intentionally separate from the model's declared context capacity. The raw Triton OIP profile rejects OpenAI generation controls because tensor shapes, batching, scheduling, warmup and TensorRT-LLM engine limits belong to the Cloudera deployment or Triton `config.pbtxt`.

Example generated profile for a Qwen model served by vLLM:

```yaml
model_list:
  - model_name: qwen-reasoning
    litellm_params:
      model: openai/Qwen/Qwen3-32B
      api_base: https://inference.example/v1
      api_key: os.environ/CLOUDERA_TOKEN
      max_tokens: 1024
      temperature: 0.6
      top_p: 0.9
      max_parallel_requests: 4
      extra_body:
        top_k: 40
        min_p: 0.05
        repetition_penalty: 1.05
        chat_template_kwargs:
          enable_thinking: true
    model_info:
      max_input_tokens: 28672
      max_output_tokens: 4096
      supports_reasoning: true
      dashboard_context_window: 32768
      dashboard_backend_profile: vllm
      dashboard_compatibility_profile: cloudera_1_5_5_sp3
      dashboard_reasoning_mode: enabled
```

Provider behaviour is version-sensitive. vLLM documents non-OpenAI sampling values under `extra_body`; NVIDIA NIM exposes an OpenAI-compatible API but reasoning depends on the selected model and chat template; Triton deployment settings must be changed server-side. Keep the compatibility profile explicit when upgrading Cloudera AI, LiteLLM, NIM, or vLLM and re-run the endpoint probe after each change.

### Parameter precedence, extras, validation, and advisor

Each model has an explicit precedence policy:

- **Client wins:** configured values are defaults and a caller may replace them in an OpenAI-compatible request.
- **Model wins:** configured values are enforced immediately before the provider call, including nested `extra_body` values.

The guided editor accepts typed extras as `VARIABLE=VALUE`, one per line. JSON booleans, numbers, arrays, and objects keep their types; dotted names such as `extra_body.guided_json={"type":"object"}` build nested provider payloads. Routing, credentials, messages, prompts, and headers are reserved and cannot be replaced through extras.

Before a new or edited model can be saved, IA Gateway sends a minimal real inference using all configured request parameters. A successful result issues a short-lived validation proof bound to that exact configuration. Triton requires an explicit inference payload because its tensor schema cannot be inferred generically. The advanced YAML editor accepts unchanged legacy models, but rejects new or modified model definitions without a matching validation fingerprint.

An optional **configuration advisor** can be selected next to the guardrail. From each model row, **Talk to the advisor** collects the use case and asks that model for a structured recommendation. Recommendations remain untrusted suggestions: the user reviews and applies them to the form, and the resulting configuration must still pass the real provider test.

## Authentication and security boundaries

The dashboard uses an independent session cookie, CSRF protection for mutations, Argon2id password hashing, rate-limited login, and a forced first-password change.

The public edge deliberately separates credentials:

- Requests sent to the dashboard keep the browser session and dashboard authorization headers.
- Requests sent to LiteLLM do not receive the dashboard `Authorization` header.
- Cloudera secret values are never sent back to the browser.
- Credential and log SQLite files, generated TLS material, PIDs, and active configuration live under ignored `runtime/` paths.

The dashboard login is not currently an API key for `/v1/*`. If the Cloudera application is created without platform authentication, any client that can reach the application URL can call enabled models. Use workspace ingress controls now, and add gateway API-key/access-control enforcement before exposing it to an untrusted network.

## Logs and client IPs

Each model has structured daily logs containing request/response summaries, the effective sanitized provider parameters, status, origin IP, provider IP, TTFT, total duration, token counts, and guardrail outcome. This records the final OpenAI/vLLM/NIM/Workbench/Triton-compatible options after precedence rules have run, without logging API keys, authorization headers, messages as parameters, or routing secrets. The dashboard shows KPIs and an hourly histogram and can export the selected day to Excel.

The edge trusts Cloudera/Istio forwarding metadata at the application boundary, preferring `X-Envoy-External-Address`, then the first non-loopback value in `X-Forwarded-For`, then `X-Real-IP`. If Cloudera removes the external address before the application, the only observable address will be the platform sidecar (`127.0.0.x`); application code cannot reconstruct information the ingress did not forward.

## Operations

### Update an existing Cloudera checkout

If there are no local changes:

```bash
git pull origin main
```

If `config.yaml` has local changes:

```bash
git stash push -m "local IA Gateway config" -- config.yaml
git pull origin main
git stash pop
```

Resolve any reported conflict before restarting the application. Downloading a YAML backup first is recommended.

### Run tests

```bash
source .venv/bin/activate
pytest -q
```

### Useful runtime files

| Path | Purpose |
|---|---|
| `config.yaml` | Model source configuration |
| `.env` | Local environment secrets; ignored by Git |
| `runtime/cloudera.sqlite3` | Cloudera connections, credentials, and token state |
| `runtime/active_config.yaml` | Generated LiteLLM configuration |
| `runtime/edge/edge.log` | Python edge process log |
| `runtime/` request databases/logs | Structured and technical logs |

### Common failures

**`No connected db.`**

Do not configure LiteLLM features that require its PostgreSQL control-plane database. IA Gateway intentionally uses SQLite through its own callbacks and does not enable the LiteLLM virtual-key database.

**`Invalid model name ... Call /v1/models`**

The YAML changed but LiteLLM has not reloaded its router. Apply the pending changes or restart LiteLLM, then check `/v1/models`.

**`Authentication Error, No api key passed in`**

The provider credential is missing. Configure its environment variable or save a Cloudera/model token in the dashboard. A LiteLLM master key is not required by this deployment.

**CDP IAM hostname does not resolve**

For CDP Public Cloud in `us-west-1`, use `https://iamapi.us-west-1.altus.cloudera.com`. If that valid hostname also fails, request outbound DNS/HTTPS access from the Cloudera workspace administrator.

**Ports are reported as identical**

Set only the public Cloudera application port or `IA_GATEWAY_PORT`. Keep the internal defaults `18080` and `14000`, or assign three distinct values.

## Source map

| File | Responsibility |
|---|---|
| `app.py` | Dependency bootstrap, FastAPI endpoints, authentication middleware, and process lifespan |
| `gateway/edge_server.py` | Single-port streaming HTTP proxy and trusted client-IP normalization |
| `gateway/edge.py` | Edge process and public-port lifecycle |
| `gateway/core.py` | LiteLLM process, YAML operations, runtime configuration, and model state |
| `gateway/cloudera.py` | SQLite credentials, Cloudera discovery/probing, and token renewal |
| `gateway/litellm_callback.py` | Dynamic credentials, guardrail execution, and structured observability |
| `gateway/log_store.py` | Daily log persistence and KPI aggregation |
| `gateway/auth.py` | Administrator credentials and sessions |
| `static/index.html` | Dashboard structure |
| `static/app.js` | Dashboard behaviour and API integration |
| `static/i18n.js` | Spanish, English, and Italian interface localization |
| `static/style.css` | Responsive visual system |

## License and production note

Review all dependency and provider licences before redistribution. Before production use, add the required `/v1/*` access-control policy, use managed TLS at the ingress, restrict workspace egress and ingress, back up `config.yaml` and `runtime/cloudera.sqlite3`, and treat prompts/responses in logs as potentially sensitive data.
