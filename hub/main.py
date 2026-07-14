import asyncio
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title="Hub Aggregator")

# ----------------------------
# Config
# ----------------------------
BASE_DIR = Path(__file__).parent
NODES_PATH = Path(__file__).with_name("nodes.json")

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))  # 24h
LOCK_TTL_SECONDS = int(os.getenv("LOCK_TTL_SECONDS", "900"))      # 15min
CACHE_FILE = BASE_DIR / "cache.json"
CORS_ALLOW_ORIGINS = [
    origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Hub -> PC call timeout (long tasks)
PC_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=None)

# ----------------------------
# In-memory cache (replaces Redis)
# ----------------------------
# Structure: { node_id: {"data": {...}, "meta": {...}, "expires_at": float} }
_cache: Dict[str, Any] = {}
# Per-node asyncio locks (replaces Redis distributed lock)
_locks: Dict[str, asyncio.Lock] = {}

def _get_lock(node_id: str) -> asyncio.Lock:
    if node_id not in _locks:
        _locks[node_id] = asyncio.Lock()
    return _locks[node_id]

def _cache_load() -> None:
    """Load persisted cache from disk on startup."""
    if CACHE_FILE.exists():
        try:
            _cache.update(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass

def _cache_save() -> None:
    """Persist cache to disk so it survives restarts."""
    try:
        CACHE_FILE.write_text(json.dumps(_cache), encoding="utf-8")
    except Exception:
        pass

@app.on_event("startup")
async def startup_event():
    _cache_load()

def cache_get(node_id: str) -> Optional[Dict[str, Any]]:
    entry = _cache.get(node_id)
    if not entry:
        return None
    if time.time() > entry.get("expires_at", 0):
        _cache.pop(node_id, None)
        return None
    return entry

def cache_set(node_id: str, data: Dict[str, Any], meta: Dict[str, Any]) -> None:
    _cache[node_id] = {
        "data": data,
        "meta": meta,
        "expires_at": time.time() + CACHE_TTL_SECONDS,
    }
    _cache_save()

# ----------------------------
# Nodes
# ----------------------------
def load_nodes() -> Dict[str, Dict[str, str]]:
    if not NODES_PATH.exists():
        # 企业级：明确错误
        raise RuntimeError(f"nodes.json not found at {NODES_PATH}")
    try:
        return json.loads(NODES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid nodes.json format: {e}") from e

@app.get("/api/nodes")
def list_nodes():
    try:
        nodes = load_nodes()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "nodes": [
            {
                "id": node_id,
                "name": info.get("name", node_id),
                "device_name": info.get("device_name", ""),
                **info  # Include all config fields from nodes.json
            }
            for node_id, info in nodes.items()
        ]
    }

# ----------------------------
# Health evaluation (same logic as frontend)
# ----------------------------
def evaluate_health(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    返回:
      { "level": "ok"|"warn", "reasons": [..] }
    规则按你们 checkpoint：
      - services: Running 异常，Stopped 正常 [2](https://intel.sharepoint.com/sites/vtg_ss_val_win/_layouts/15/Doc.aspx?sourcedoc=%7B64B006C4-CFE8-4D84-8E37-6F8A6E5E08BA%7D&file=RVP%20Check%20Point.xlsx&action=default&mobileredirect=true&DefaultItemOpen=1)
      - bios: aligned 正常，否则异常 [2](https://intel.sharepoint.com/sites/vtg_ss_val_win/_layouts/15/Doc.aspx?sourcedoc=%7B64B006C4-CFE8-4D84-8E37-6F8A6E5E08BA%7D&file=RVP%20Check%20Point.xlsx&action=default&mobileredirect=true&DefaultItemOpen=1)
      - power: screen_sleep_ac/hibernate_ac 期望 Never（可按需收紧）
      - sync_time: 缺失视为 warn
    """
    reasons = []

    services = data.get("services") or []
    if any(str(s.get("status", "")).lower() == "running" for s in services):
        reasons.append("Service Running")

    bios = data.get("bios") or {}
    if any(str(v).lower() != "aligned" for v in bios.values()):
        reasons.append("BIOS Not Aligned")

    power = data.get("power") or {}
    if power.get("screen_sleep_ac") and str(power["screen_sleep_ac"]).lower() != "never":
        reasons.append("Screen Sleep Not Never")
    if power.get("hibernate_ac") and str(power["hibernate_ac"]).lower() != "never":
        reasons.append("Hibernate Not Never")

    sync = (data.get("sync_time") or {}).get("time_zone_information")
    if not sync:
        reasons.append("Sync Time Missing")

    return {"level": "warn" if reasons else "ok", "reasons": reasons}

# ----------------------------
# Call node (your existing call_node upgraded)
# ----------------------------
async def call_node(node_id: str, path: str, method: str, request: Request) -> Dict[str, Any]:
    nodes = load_nodes()
    if node_id not in nodes:
        raise HTTPException(status_code=404, detail=f"Unknown node: {node_id}")

    base_url = nodes[node_id]["base_url"].rstrip("/")
    target_url = f"{base_url}/{path.lstrip('/')}"

    params = dict(request.query_params)

    body: Optional[bytes] = None
    if method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    headers = {"X-Hub-NodeId": node_id}

    try:
        # ✅ 关键：trust_env=False，避免系统代理把内网请求走 DMZ 导致失败 [1](https://www.geeksforgeeks.org/javascript/how-to-create-responsive-admin-dashboard-using-html-css-javascript/)
        async with httpx.AsyncClient(timeout=PC_TIMEOUT, trust_env=False) as client:
            resp = await client.request(
                method=method,
                url=target_url,
                params=params,
                content=body,
                headers=headers,
            )
        resp.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Timeout calling node {node_id}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error calling node {node_id}: {e!s}")

    if "application/json" in (resp.headers.get("content-type", "") or ""):
        return resp.json()
    return {"text": resp.text}

# ----------------------------
# API: Refresh -> write cache (server-side shared cache)
# ----------------------------
@app.post("/api/node/{node_id}/refresh")
async def refresh_node(node_id: str, request: Request):
    lock = _get_lock(node_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Refresh already in progress")

    async with lock:
        start = time.time()
        data = await call_node(node_id, "/items/1", "GET", request)
        health = evaluate_health(data)
        duration = time.time() - start

        meta = {
            "node": node_id,
            "health": health,
            "duration_sec": round(duration, 3),
            "updated_at": int(time.time()),
        }

        cache_set(node_id, data, meta)
        return {"ok": True, "node": node_id, "meta": meta}

# ----------------------------
# API: Read cached (server-side shared cache)
# ----------------------------
@app.get("/api/node/{node_id}/cached")
async def get_cached(node_id: str):
    try:
        nodes = load_nodes()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    if node_id not in nodes:
        raise HTTPException(status_code=404, detail=f"Unknown node: {node_id}")
    
    entry = cache_get(node_id)
    if not entry:
        return JSONResponse(status_code=404, content={"ok": False, "detail": "No cached data"})
    
    # Include full node config from nodes.json
    return {
        "ok": True,
        "node": node_id,
        "config": nodes[node_id],  # Full config from nodes.json
        "meta": entry["meta"],
        "data": entry["data"]
    }

# ----------------------------
# (Compatibility) Your existing endpoint: /api/node/{node_id}/items/1
# ----------------------------
@app.get("/api/node/{node_id}/items/1")
async def proxy_item_1(node_id: str, request: Request, refresh: int = 0):
    if refresh == 1:
        await refresh_node(node_id, request)
    return await get_cached(node_id)

# ----------------------------
# API: Get all cached for UI initial sync
# ----------------------------
@app.get("/api/cache/latest")
async def get_all_cached():
    nodes = load_nodes()
    items: Dict[str, Any] = {}
    for node_id in nodes.keys():
        entry = cache_get(node_id)
        if entry:
            items[node_id] = {"meta": entry["meta"], "data": entry["data"]}
    return {"ok": True, "items": items}

# ----------------------------
# API: Clear cached (admin/ops)
# ----------------------------
@app.delete("/api/node/{node_id}/cached")
async def clear_cached(node_id: str):
    _cache.pop(node_id, None)
    _cache_save()
    return {"ok": True, "node": node_id}

@app.get("/api/health")
async def health():
    return {"ok": True, "cache_entries": len(_cache)}


# ----------------------------
# SSH Setup Jobs (reuse windows_env_setup_web logic)
# ----------------------------
sys.path.insert(0, str(BASE_DIR))
try:
    from windows_env_setup_web import JOB_STORE as _SSH_JOB_STORE, run_remote_setup as _run_remote_setup
    _SSH_AVAILABLE = True
except ImportError:
    _SSH_AVAILABLE = False


@app.post("/api/node/{node_id}/setup")
async def run_node_setup(node_id: str, request: Request):
    """SSH into node and run ValidationExecutionConfig or AutoCaseEnvInstall"""
    if not _SSH_AVAILABLE:
        raise HTTPException(status_code=503, detail="SSH setup unavailable: install paramiko")

    try:
        nodes = load_nodes()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if node_id not in nodes:
        raise HTTPException(status_code=404, detail=f"Unknown node: {node_id}")

    body = await request.json()
    setup_type = body.get("type", "both")  # "validation", "auto", or "both"
    username = (body.get("username") or "").strip()
    password = body.get("password", "")

    if not username:
        raise HTTPException(status_code=400, detail="SSH username is required")

    node_info = nodes[node_id]
    host = node_info["name"]  # name field is the IP address

    board_view = {
        "id": node_id,
        "name": node_info.get("device_name", node_id),
        "host": host,
    }
    job_id = _SSH_JOB_STORE.create([board_view], {"type": setup_type})

    board_config = {
        "id": node_id,
        "name": board_view["name"],
        "host": host,
        "port": int(body.get("port", 22)),
        "username": username,
        "password": password,
        "workspace_dir": body.get("workspace_dir") or r"C:\AutoPackageSetup",
        "validation_share": body.get("validation_share") or r"C:\workspace\ValidationExecutionConfig.zip",
        "auto_share": body.get("auto_share") or r"C:\workspace\AutoCaseEnvInstall.bat",
        "share_username": body.get("share_username", ""),
        "share_password": body.get("share_password", ""),
        "run_validation": setup_type in ("validation", "both"),
        "run_auto": setup_type in ("auto", "both"),
    }

    def _run():
        _run_remote_setup(board_config, job_id)
        _SSH_JOB_STORE.finish(job_id)

    threading.Thread(target=_run, daemon=True).start()

    return {"ok": True, "job_id": job_id}


@app.get("/api/setup/job/{job_id}")
async def get_setup_job(job_id: str):
    """Poll setup job status and logs"""
    if not _SSH_AVAILABLE:
        raise HTTPException(status_code=503, detail="SSH setup unavailable")
    job = _SSH_JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    board = list(job["boards"].values())[0] if job["boards"] else {}
    return {
        "ok": True,
        "status": job["status"],
        "board_status": board.get("status", "unknown"),
        "message": board.get("message", ""),
        "logs": board.get("logs", []),
    }