"""Hermes dashboard — single pane of glass for the stack.

Groups containers by their `hermes.app` label, shows status/health/memory,
and exposes start/stop/restart per app group. Talks to the host Docker
daemon through the mounted socket, so every endpoint (including static
pages) sits behind HTTP Basic auth.
"""

import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

import docker
import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

SERVER_HOST = os.environ.get("SERVER_HOST", "localhost")
AUTH_USER = os.environ.get("DASHBOARD_USERNAME", "admin")
AUTH_PASS = os.environ.get("DASHBOARD_PASSWORD", "")

# Woody / agent integration (reachable over hermes_net)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OO_URL = os.environ.get("OPENOBSERVE_URL", "http://openobserve:5080")
OO_USER = os.environ.get("OO_USERNAME", "")
OO_PASS = os.environ.get("OO_PASSWORD", "")
CHAT_MODELS = {"woody", "woody:latest", "qwen3:8b", "qwen3:1.7b"}
WOODY_DIR = os.environ.get("WOODY_DIR", "/opt/woody")
WOODY_BASE_MODEL = "qwen3:8b"
HOST_UID = 1000  # jluciveroa on the host — keep edited files owned by him

# in-memory token counters for the chat proxy (reset on container restart)
TOKENS = {"in": 0, "out": 0, "chats": 0, "last_tps": None}

APP_LABEL = "hermes.app"
ALLOWED_ACTIONS = {"start", "stop", "restart"}
# infra is deliberately excluded from stop/restart: killing postgres/redis
# takes every other app down with it. It can still be seen, just not toggled.
PROTECTED_APPS = {"infra", "dashboard"}

app = FastAPI(title="Hermes Dashboard", docs_url=None, redoc_url=None)
security = HTTPBasic()
client = docker.from_env()


def check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not AUTH_PASS:
        raise HTTPException(503, "DASHBOARD_PASSWORD is not set; refusing to serve")
    user_ok = secrets.compare_digest(credentials.username, AUTH_USER)
    pass_ok = secrets.compare_digest(credentials.password, AUTH_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Bad credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def _mem_usage_mb(container) -> float | None:
    try:
        stats = container.stats(stream=False)
        usage = stats["memory_stats"].get("usage")
        # cgroup v2 reports file cache under inactive_file; subtract it the
        # way `docker stats` does so numbers match what admins expect
        cache = stats["memory_stats"].get("stats", {}).get("inactive_file", 0)
        return round((usage - cache) / (1024 * 1024), 1) if usage else None
    except Exception:
        return None


def _container_info(c, with_stats: bool) -> dict:
    labels = c.labels or {}
    health = c.attrs.get("State", {}).get("Health", {}).get("Status")
    return {
        "id": c.short_id,
        "container": c.name,
        "name": labels.get("hermes.name", c.name),
        "status": c.status,
        "health": health,
        # read the image name off container attrs: c.image would call
        # GET /images/{id}, which the socket-proxy denies (IMAGES=0)
        "image": c.attrs.get("Config", {}).get("Image", "unknown"),
        "memory_mb": _mem_usage_mb(c) if (with_stats and c.status == "running") else None,
    }


@app.get("/api/apps")
def list_apps(stats: bool = False, _: str = Depends(check_auth)):
    containers = client.containers.list(all=True, filters={"label": APP_LABEL})
    apps: dict[str, dict] = {}

    if stats:
        # container.stats() blocks ~1s each; fan out so the page stays snappy
        with ThreadPoolExecutor(max_workers=16) as pool:
            infos = list(pool.map(lambda c: (c, _container_info(c, True)), containers))
    else:
        infos = [(c, _container_info(c, False)) for c in containers]

    for c, info in infos:
        labels = c.labels or {}
        key = labels[APP_LABEL]
        entry = apps.setdefault(
            key,
            {
                "app": key,
                "role": None,
                "url": None,
                "description": None,
                "protected": key in PROTECTED_APPS,
                "containers": [],
            },
        )
        entry["containers"].append(info)
        # role/url/description live on the app's primary container only
        if labels.get("hermes.role"):
            entry["role"] = labels["hermes.role"]
        if labels.get("hermes.url"):
            entry["url"] = labels["hermes.url"].replace("{host}", SERVER_HOST)
        if labels.get("hermes.description"):
            entry["description"] = labels["hermes.description"]

    for entry in apps.values():
        states = {c["status"] for c in entry["containers"]}
        if states <= {"running"}:
            entry["state"] = "running"
        elif "running" in states:
            entry["state"] = "partial"
        elif states <= {"exited", "created"} and any(
            c["container"].endswith(("-init", "-migrator")) for c in entry["containers"]
        ) and any(c["status"] == "running" for c in entry["containers"]):
            entry["state"] = "running"
        else:
            entry["state"] = "stopped"

    return {"host": SERVER_HOST, "apps": sorted(apps.values(), key=lambda a: a["app"])}


@app.get("/api/host")
def host_info(_: str = Depends(check_auth)):
    # /proc/meminfo inside a container reflects the host
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            mem[key.strip()] = int(rest.strip().split()[0])  # kB
    return {
        "host": SERVER_HOST,
        "mem_total_gb": round(mem["MemTotal"] / 1048576, 1),
        "mem_available_gb": round(mem["MemAvailable"] / 1048576, 1),
        "docker_version": client.version().get("Version"),
    }


@app.post("/api/apps/{app_name}/{action}")
def app_action(app_name: str, action: str, _: str = Depends(check_auth)):
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(400, f"action must be one of {sorted(ALLOWED_ACTIONS)}")
    if app_name in PROTECTED_APPS and action != "start":
        raise HTTPException(403, f"'{app_name}' is protected; manage it from the CLI")

    containers = client.containers.list(all=True, filters={"label": f"{APP_LABEL}={app_name}"})
    if not containers:
        raise HTTPException(404, f"no containers labeled {APP_LABEL}={app_name}")

    results = []
    for c in containers:
        # leave one-shot init/migrator containers alone
        if c.name.endswith(("-init", "-migrator")):
            continue
        try:
            getattr(c, action)()
            results.append({"container": c.name, "ok": True})
        except docker.errors.APIError as e:
            results.append({"container": c.name, "ok": False, "error": str(e)})

    return JSONResponse({"app": app_name, "action": action, "results": results})


@app.post("/api/chat")
async def woody_chat(request: Request, _: str = Depends(check_auth)):
    body = await request.json()
    model = body.get("model", "woody")
    if model not in CHAT_MODELS:
        raise HTTPException(400, f"model must be one of {sorted(CHAT_MODELS)}")
    # cap history so a long-lived tab can't grow requests unboundedly
    messages = body.get("messages", [])[-20:]
    if not messages:
        raise HTTPException(400, "messages required")

    def stream():
        try:
            with requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": model, "messages": messages, "stream": True, "think": False},
                stream=True,
                timeout=300,
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    # the final chunk carries token counts — tally them
                    try:
                        j = json.loads(line)
                        if j.get("done"):
                            TOKENS["in"] += j.get("prompt_eval_count", 0)
                            TOKENS["out"] += j.get("eval_count", 0)
                            TOKENS["chats"] += 1
                            if j.get("eval_count") and j.get("eval_duration"):
                                TOKENS["last_tps"] = round(
                                    j["eval_count"] / (j["eval_duration"] / 1e9), 1
                                )
                    except (ValueError, KeyError):
                        pass
                    yield line + b"\n"
        except requests.RequestException as e:
            yield ('{"error": ' + repr(str(e)).replace("'", '"') + "}\n").encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/api/agent")
def agent_metrics(_: str = Depends(check_auth)):
    out = {"models": [], "loaded": [], "runs": None, "tokens": dict(TOKENS), "host": None}
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
        cpus = os.cpu_count() or 1
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                mem[key.strip()] = int(rest.strip().split()[0])
        out["host"] = {
            "load1": load1,
            "cpus": cpus,
            "cpu_pct": round(min(load1 / cpus, 1.0) * 100),
            "mem_total_gb": round(mem["MemTotal"] / 1048576, 1),
            "mem_used_gb": round((mem["MemTotal"] - mem["MemAvailable"]) / 1048576, 1),
        }
    except Exception:
        pass
    try:
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()
        out["models"] = [m["name"] for m in tags.get("models", [])]
        ps = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5).json()
        out["loaded"] = [m["name"] for m in ps.get("models", [])]
    except Exception:
        pass
    if OO_USER and OO_PASS:
        try:
            now_us = int(time.time() * 1_000_000)
            week_ago_us = now_us - 7 * 86_400 * 1_000_000
            q = {
                "query": {
                    # SELECT * — naming columns breaks until every field has
                    # appeared in the stream at least once (schema-on-write)
                    "sql": 'SELECT * FROM "agent_runs" ORDER BY _timestamp DESC',
                    "from": 0,
                    "size": 50,
                    "start_time": week_ago_us,
                    "end_time": now_us,
                }
            }
            r = requests.post(
                f"{OO_URL}/api/default/_search", json=q, auth=(OO_USER, OO_PASS), timeout=8
            )
            hits = r.json().get("hits", [])
            out["runs"] = {"count_7d": len(hits), "recent": hits[:8]}
        except Exception:
            pass
    return out


@app.get("/runbook")
def runbook(_: str = Depends(check_auth)):
    return FileResponse("RUNBOOK.md", media_type="text/plain; charset=utf-8")


@app.get("/api/woody/config")
def woody_config(_: str = Depends(check_auth)):
    brain_path = os.path.join(WOODY_DIR, "brain", "system.txt")
    with open(brain_path) as f:
        return {"system": f.read(), "base_model": WOODY_BASE_MODEL}


@app.post("/api/woody/config")
async def woody_config_save(request: Request, _: str = Depends(check_auth)):
    body = await request.json()
    system = (body.get("system") or "").strip()
    if len(system) < 20:
        raise HTTPException(400, "system prompt too short — refusing to lobotomize Woody")
    brain_path = os.path.join(WOODY_DIR, "brain", "system.txt")
    with open(brain_path, "w") as f:
        f.write(system + "\n")
    try:
        os.chown(brain_path, HOST_UID, HOST_UID)
    except OSError:
        pass

    # rebuild the model so the new brain takes effect; new-style API first,
    # legacy modelfile payload as fallback for older ollama builds
    r = requests.post(
        f"{OLLAMA_URL}/api/create",
        json={"model": "woody", "from": WOODY_BASE_MODEL, "system": system, "stream": False},
        timeout=120,
    )
    if r.status_code >= 400:
        modelfile = f'FROM {WOODY_BASE_MODEL}\nSYSTEM """\n{system}\n"""\nPARAMETER temperature 0.7\n'
        r = requests.post(
            f"{OLLAMA_URL}/api/create",
            json={"name": "woody", "modelfile": modelfile, "stream": False},
            timeout=120,
        )
    if r.status_code >= 400:
        raise HTTPException(502, f"brain saved but model rebuild failed: {r.text[:200]}")
    return {"ok": True, "rebuilt": "woody", "detail": r.json()}


@app.get("/")
def index(_: str = Depends(check_auth)):
    # no-store: the UI is a single baked-in file; a stale cached copy keeps
    # rendering removed sections, so force browsers to refetch every load
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store"})
