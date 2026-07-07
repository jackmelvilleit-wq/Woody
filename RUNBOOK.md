# Server Runbook — 192.168.50.74 (Hermes / Woody / n8n)

_Last updated: 2026-07-07. Maintained alongside the stack in `~/hermes`._

Ubuntu VM · 8 vCPU (Xeon Gold 6238R) · 31 GB RAM · VMware guest.
Owner: Jack Lucivero (jlucivero@sherwoodlumber.com).

## Access model

Inbound access to the web apps is **restricted to Jack's workstation
(192.168.40.115)** by iptables rules in the `DOCKER-USER` chain (Docker
published ports bypass ufw/INPUT). Rules persist via `netfilter-persistent`
(`/etc/iptables/rules.v4`). Restricted ports: **8088, 3000, 3001, 5080, 42110**.

Open to the whole LAN: **22** (ssh), **80** (nginx → n8n), **5678** (n8n).
n8n has its own login; consider adding it to the DOCKER-USER filter if
nobody else needs it.

Loopback-only: **11434** (Ollama, no auth), **5555** (Mission Control API),
**8088 on 127.0.0.1** (dashboard, for ssh tunnels).

```
# view / edit the filter
sudo iptables -L DOCKER-USER -n -v --line-numbers
sudo netfilter-persistent save        # persist after any change
```

## Services

### Hermes stack (`~/hermes`, compose project "hermes")

| Service | URL / port | What it does | Login |
|---|---|---|---|
| **Hermes Dashboard** | http://192.168.50.74:8088 | Mission control: launchpad links (server tools, Claude/ChatGPT/Groq/Kimi, OpenRouter/HF/consoles, Asana/M365), **Woody chat window** (streams from Ollama), **agent metrics** (runs from OpenObserve `agent_runs`), and status + start/stop/restart card per app via the locked-down socket-proxy. | `admin` / password in `~/hermes/.env.dashboard` |
| **Open WebUI** | http://192.168.50.74:3000 | Chat UI on local models (incl. `woody`). | Self-signup; **first account becomes admin** — after registering, set `ENABLE_SIGNUP: "false"` in `compose/ai-core.yml` |
| **Ollama** | 127.0.0.1:11434 (host) / `http://ollama:11434` (containers) | Local LLM runtime. Models: `woody` (qwen3:8b + persona, ~6 tok/s), `qwen3:8b`, `qwen3:1.7b` (~27 tok/s). **No auth — keep loopback/internal.** | — |
| **Khoj** | http://192.168.50.74:42110 | AI search/chat over uploaded docs; wired to Ollama (default qwen3:1.7b, advanced qwen3:8b). Admin panel: `/server/admin`. | `jlucivero@sherwoodlumber.com` / `KHOJ_ADMIN_PASSWORD` in `~/hermes/.env` |
| **OpenObserve** | http://192.168.50.74:5080 | Logs/metrics/traces. Agent audit trail: `default` org, `agent_runs` stream. Ingest: `POST /api/default/agent_runs/_json` (basic auth). | `jlucivero@sherwoodlumber.com` / `ZO_ROOT_USER_PASSWORD` in `~/hermes/.env` |
| **Flowise** | http://192.168.50.74:3001 | Visual LLM agent builder — the Hermes "agent layer". Woody's agent flow will live here. | **Not set up yet** — first visit creates the admin account |
| **PostgreSQL** (pgvector) | internal :5432 | Shared DB (khoj, flowise; dbs pre-created for the whole stack). | `POSTGRES_USER`/`POSTGRES_PASSWORD` in `~/hermes/.env` |
| **socket-proxy** | internal :2375 | Filtered Docker API for the dashboard (`CONTAINERS=1, POST=1`; exec/images/volumes/networks denied). Never publish. | — |

Not running (defined in `~/hermes/compose/`): redis, qdrant, minio, dify,
chatwoot, appflowy, twenty, plane, and **hermes-n8n + its compose sibling —
see Landmines**.

### Standalone n8n (`~/n8n`)

| | |
|---|---|
| URL | http://192.168.50.74:5678 (also http://192.168.50.74/ via nginx) |
| What | Workflow automation hub. Joined to `hermes_net`, so nodes can call `http://ollama:11434`, `http://openobserve:5080`, `http://khoj:42110`, `http://flowise:3000`. |
| Login | n8n owner account (created in UI; n8n manages its own users — the `N8N_BASIC_AUTH_*` vars in the compose file are deprecated no-ops) |
| Credentials vault | Asana PAT, Telegram bot, OpenObserve Root (encrypted with `N8N_ENCRYPTION_KEY` from `~/n8n/docker-compose.yml`) |
| Active workflows | **Asana Remediation Weekly Rollup** (Mon 07:00 ET): Asana → stats → Woody analysis → Telegram (chat 8131524177) → OpenObserve `agent_runs` |

### Host services (not Docker)

| Service | Port | What it does |
|---|---|---|
| nginx | 80 | Reverse proxy → n8n (default server and `n8n.local`) |
| Mission Control API | 127.0.0.1:5555 | Flask helper (`~/dashboard/api/proxy.py`, systemd unit `mission-control-api.service`) serving docker/system/n8n stats |
| sshd | 22 | Shell access |

## Woody (the Hermes agent)

**W.O.O.D.Y. — Workflow Orchestration & Operations Deputy, at Your service.**

- **Identity**: `/opt/woody/brain/system.txt` → baked into Ollama model `woody`.
  Editable from the dashboard ("Woody's brain" panel), which saves the file and
  rebuilds the model in one click. The runbook itself is served at
  http://192.168.50.74:8088/runbook (live view of this file).
- **Deployment profile & jobs**: `/opt/woody/memory/profile.md`.
- **Workflow archive**: `/opt/woody/workflows/active/`.
- **Rebuild after editing the brain**: copy `system.txt` + Modelfile into
  `hermes-ollama` and run `ollama create woody -f <Modelfile>` (exact steps
  in the profile doc).
- **Next evolution**: agent flow in Flowise (`http://flowise:3000/api/v1/prediction/<id>`),
  so the model behind him (Ollama ↔ Anthropic) can be swapped without touching n8n.

## Routine operations

```
# status of everything            docker ps
# hermes stack (from ~/hermes)    docker compose up -d <svc> · logs -f <svc> · restart <svc>
# standalone n8n (from ~/n8n)     docker compose up -d · docker logs n8n
# dashboard rebuild               cd ~/hermes && docker compose up -d --build dashboard
# NEVER start every service       do not run bare `docker compose up -d` in ~/hermes
```

Data lives in named Docker volumes (`docker volume ls | grep -E 'hermes|n8n'`):
postgres_data, open_webui_data, khoj_config/khoj_models, openobserve_data,
flowise_data, n8n_n8n_data, plus Ollama models in `/usr/share/ollama/.ollama`
(host dir). Back these up if the VM isn't snapshotted.

## Secrets index (no values here — locations only)

| File | Holds |
|---|---|
| `~/hermes/.env` (600) | Postgres, Redis, MinIO, Qdrant, Khoj admin, OpenObserve root, app secret keys |
| `~/hermes/.env.dashboard` (600) | Dashboard basic-auth user/password + OpenObserve creds for the agent-metrics panel |
| `~/n8n/docker-compose.yml` | `N8N_ENCRYPTION_KEY` (protects the n8n credential vault — losing it orphans all stored credentials) |
| n8n credential vault (UI) | Asana PAT, Telegram bot token, OpenObserve basic auth |

## Landmines

1. **Never start `hermes-n8n`** (`~/hermes/compose/automation.yml`) — it binds
   5678 and collides with the standalone n8n. Either migrate to it deliberately
   or delete the service block.
2. **Ollama has no auth** — it must stay loopback/internal. Don't publish 11434.
3. **Open WebUI signup is still open** (to .40.115 only) until
   `ENABLE_SIGNUP: "false"` is set after Jack registers.
4. **`docker compose up -d` bare in `~/hermes`** would start the entire
   12-app stack (and landmine #1). Always name services.
5. The dashboard's socket-proxy allows exec *creation* but not exec *start* —
   harmless, but don't be surprised seeing it in an audit.
6. OpenObserve requires a complexity-compliant root password — a plain hex
   rotation will crash-loop it (panic at startup).
7. Editing n8n workflows via CLI import while the editor is open in a browser
   can silently revert changes — refresh the editor after out-of-band edits.
