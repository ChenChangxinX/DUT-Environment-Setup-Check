import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from redis.asyncio import Redis

app = FastAPI(title="Hub Aggregator (Redis Cache)")

# ----------------------------
# Config
# ----------------------------
BASE_DIR = Path(__file__).parent
NODES_PATH = Path(__file__).with_name("nodes.json")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))  # 24h
LOCK_TTL_SECONDS = int(os.getenv("LOCK_TTL_SECONDS", "900"))      # 15min

# Hub -> PC call timeout (long tasks)
PC_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=None)

# Redis client (decode_responses=True makes get/set return str)
redis: Redis = Redis.from_url(REDIS_URL, decode_responses=True)

# Redis key helpers
def k_latest(node_id: str) -> str:
    return f"hub:pc:{node_id}:latest"

def k_meta(node_id: str) -> str:
    return f"hub:pc:{node_id}:meta"

def k_lock(node_id: str) -> str:
    return f"hub:pc:{node_id}:lock"

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
            {"id": node_id, "name": info.get("name", node_id)}
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
# Redis cache primitives
# ----------------------------
async def redis_get_json(key: str) -> Optional[Dict[str, Any]]:
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

async def redis_set_json(key: str, obj: Dict[str, Any], ttl: int) -> None:
    await redis.set(key, json.dumps(obj), ex=ttl)

async def acquire_lock(node_id: str) -> bool:
    # NX: only set if not exists
    return bool(await redis.set(k_lock(node_id), "1", nx=True, ex=LOCK_TTL_SECONDS))

async def release_lock(node_id: str) -> None:
    await redis.delete(k_lock(node_id))

# ----------------------------
# API: Refresh -> write Redis (server-side shared cache)
# ----------------------------
@app.post("/api/node/{node_id}/refresh")
async def refresh_node(node_id: str, request: Request):
    # prevent concurrent refresh on same node
    locked = await acquire_lock(node_id)
    if not locked:
        raise HTTPException(status_code=409, detail="Refresh already in progress")

    start = time.time()
    try:
        data = await call_node(node_id, "/items/1", "GET", request)
        health = evaluate_health(data)
        duration = time.time() - start

        meta = {
            "node": node_id,
            "health": health,                 # {"level": "ok"/"warn", "reasons":[...]}
            "duration_sec": round(duration, 3),
            "updated_at": int(time.time()),
        }

        await redis_set_json(k_latest(node_id), data, CACHE_TTL_SECONDS)
        await redis_set_json(k_meta(node_id), meta, CACHE_TTL_SECONDS)

        return {"ok": True, "node": node_id, "meta": meta}
    finally:
        await release_lock(node_id)

# ----------------------------
# API: Read cached (server-side shared cache)
# ----------------------------
@app.get("/api/node/{node_id}/cached")
async def get_cached(node_id: str):
    data = await redis_get_json(k_latest(node_id))
    meta = await redis_get_json(k_meta(node_id))

    if not data:
        return JSONResponse(status_code=404, content={"ok": False, "detail": "No cached data"})

    return {"ok": True, "node": node_id, "meta": meta, "data": data}

# ----------------------------
# (Compatibility) Your existing endpoint: /api/node/{node_id}/items/1
# Now backed by Redis. Use query ?refresh=1 to force refresh.
# ----------------------------
@app.get("/api/node/{node_id}/items/1")
async def proxy_item_1(node_id: str, request: Request, refresh: int = 0):
    if refresh == 1:
        # Force refresh then return cached
        await refresh_node(node_id, request)
    cached = await get_cached(node_id)
    return cached

# ----------------------------
# API: Get all cached for UI initial sync
# ----------------------------
@app.get("/api/cache/latest")
async def get_all_cached():
    nodes = load_nodes()
    items: Dict[str, Any] = {}
    for node_id in nodes.keys():
        data = await redis_get_json(k_latest(node_id))
        meta = await redis_get_json(k_meta(node_id))
        if data:
            items[node_id] = {"meta": meta, "data": data}
    return {"ok": True, "items": items}

# ----------------------------
# API: Clear cached (admin/ops)
# ----------------------------
@app.delete("/api/node/{node_id}/cached")
async def clear_cached(node_id: str):
    await redis.delete(k_latest(node_id))
    await redis.delete(k_meta(node_id))
    await redis.delete(k_lock(node_id))
    return {"ok": True, "node": node_id}

@app.get("/api/health/redis")
async def health_redis():
    try:
        pong = await redis.ping()
        return {"ok": bool(pong), "redis_url": REDIS_URL}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Redis not ready: {e}")