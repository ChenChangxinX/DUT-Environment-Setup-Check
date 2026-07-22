import asyncio
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

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
JIRA_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=None)
JIRA_CASES_CACHE_KEY = "jira_approved_cases"
JIRA_EXCLUDED_FOLDERS = {
    "/dev_nvl_hx_nit",
    "/fpga_nit",
    "/nvl_cit_nit",
    "/pit-lite",
}

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
# Jira case sync helpers
# ----------------------------
def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def _normalize_field_key(value: Any) -> str:
    return _normalize_text(value)


def _pick_status_name(case_obj: Dict[str, Any]) -> str:
    candidates = (
        case_obj.get("statusName"),
        case_obj.get("workflowStatusName"),
        case_obj.get("status"),
        case_obj.get("workflowStatus"),
        (case_obj.get("status") or {}).get("name") if isinstance(case_obj.get("status"), dict) else None,
        (case_obj.get("workflowStatus") or {}).get("name") if isinstance(case_obj.get("workflowStatus"), dict) else None,
    )
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


def _get_custom_fields(case_obj: Dict[str, Any]) -> Dict[str, Any]:
    cf = case_obj.get("customFields")
    return cf if isinstance(cf, dict) else {}


def _pick_custom_field(case_obj: Dict[str, Any], field_name: str) -> str:
    cf = _get_custom_fields(case_obj)
    if not cf:
        return ""
    target = _normalize_field_key(field_name)
    for k, v in cf.items():
        if _normalize_field_key(k) == target:
            return "" if v is None else str(v).strip()
    return ""


def _pick_folder_value(case_obj: Dict[str, Any]) -> str:
    candidates = (
        _pick_custom_field(case_obj, "Folder"),
        case_obj.get("folder"),
        case_obj.get("folderName"),
        case_obj.get("folderPath"),
        (case_obj.get("folder") or {}).get("name") if isinstance(case_obj.get("folder"), dict) else None,
        (case_obj.get("folder") or {}).get("path") if isinstance(case_obj.get("folder"), dict) else None,
    )
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


def _extract_case_filter_fields(case_obj: Dict[str, Any]) -> Dict[str, str]:
    specific_dut = _pick_custom_field(case_obj, "Specific DUT")
    if not specific_dut:
        specific_dut = _pick_custom_field(case_obj, "DUT")
    return {
        "specificDut": specific_dut,
        "lightEquipment": _pick_custom_field(case_obj, "Light Equipment"),
        "testChart": _pick_custom_field(case_obj, "Test Chart"),
        "testScene": _pick_custom_field(case_obj, "Test Scene"),
    }


def _is_case_target(case_obj: Dict[str, Any]) -> bool:
    status_ok = _normalize_text(_pick_status_name(case_obj)) == "approved"
    execution_type_ok = _normalize_text(_pick_custom_field(case_obj, "Execution Type")) == "auto"
    folder_norm = _normalize_text(_pick_folder_value(case_obj))
    folder_is_blank = folder_norm == ""
    folder_allowed = (folder_norm not in JIRA_EXCLUDED_FOLDERS) and (not folder_is_blank)
    return status_ok and execution_type_ok and folder_allowed


def _parse_list_payload(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("values", "results", "items", "testCases"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _parse_total(data: Any) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    for key in ("total", "totalCount", "count"):
        v = data.get(key)
        if isinstance(v, int):
            return v
    return None


def _build_jira_query_candidates(project_id: str, project_key: str) -> List[str]:
    candidates: List[str] = []
    if project_key:
        candidates.extend(
            [
                f"projectKey = {project_key}",
                f"projectKey = \"{project_key}\"",
                f"project = {project_key}",
                f"project = \"{project_key}\"",
            ]
        )
    candidates.extend(
        [
            f"projectId = {project_id}",
            f"project = {project_id}",
            f"project = \"{project_id}\"",
        ]
    )
    return candidates


def _build_jira_fetch_attempts(project_id: str, project_key: str, start_at: int, page_size: int) -> List[Tuple[str, Dict[str, Any]]]:
    query_candidates = _build_jira_query_candidates(project_id, project_key)
    attempts: List[Tuple[str, Dict[str, Any]]] = []
    for q in query_candidates:
        attempts.append(("/rest/atm/1.0/testcase/search", {"query": q, "startAt": start_at, "maxResults": page_size}))
    for q in query_candidates:
        attempts.append(
            (
                "/rest/atm/1.0/testcase/search",
                {"projectId": project_id, "query": q, "startAt": start_at, "maxResults": page_size},
            )
        )
    attempts.append(("/rest/atm/1.0/testcase", {"projectId": project_id, "startAt": start_at, "maxResults": page_size}))
    return attempts


def _is_jira_query_error(status_code: int, body_text: str) -> bool:
    if status_code != 400:
        return False
    low = (body_text or "").lower()
    return (
        "a query must be provided" in low
        or "query statement is not valid" in low
        or "unrecognized field" in low
    )


async def _jira_get_json(
    client: httpx.AsyncClient,
    base: str,
    endpoint: str,
    headers: Dict[str, str],
    auth: Optional[Tuple[str, str]],
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str, Any]:
    url = f"{base}{endpoint}"
    resp = await client.get(url, params=params, headers=headers, auth=auth)
    text = resp.text or ""
    try:
        data = resp.json() if text.strip() else None
    except Exception:
        data = None
    return resp.status_code, text, data


async def _resolve_project_key(
    client: httpx.AsyncClient,
    base: str,
    project_id: str,
    headers: Dict[str, str],
    auth: Optional[Tuple[str, str]],
) -> str:
    code, _, data = await _jira_get_json(client, base, f"/rest/api/2/project/{project_id}", headers=headers, auth=auth)
    if code >= 400:
        return ""
    if isinstance(data, dict):
        return str(data.get("key") or "").strip()
    return ""


async def _fetch_project_cases(
    base: str,
    project_id: str,
    headers: Dict[str, str],
    auth: Optional[Tuple[str, str]],
    insecure: bool,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], str]:
    all_items: List[Dict[str, Any]] = []
    seen = set()
    start_at = 0
    selected_strategy: Optional[Tuple[str, Dict[str, Any]]] = None

    async with httpx.AsyncClient(timeout=JIRA_TIMEOUT, trust_env=False, verify=(not insecure)) as client:
        project_key = await _resolve_project_key(client, base, project_id, headers, auth)

        while True:
            if selected_strategy is None:
                attempts = _build_jira_fetch_attempts(project_id, project_key, start_at, page_size)
            else:
                endpoint_template, params_template = selected_strategy
                params = dict(params_template)
                params["startAt"] = start_at
                params["maxResults"] = page_size
                attempts = [(endpoint_template, params)]

            code = 0
            text = ""
            data = None
            errors: List[str] = []

            for endpoint, params in attempts:
                code, text, data = await _jira_get_json(client, base, endpoint, headers=headers, auth=auth, params=params)
                if code < 400:
                    if selected_strategy is None:
                        selected_strategy = (endpoint, {k: v for k, v in params.items() if k not in ("startAt", "maxResults")})
                    break
                if _is_jira_query_error(code, text):
                    errors.append(f"endpoint={endpoint}, params={params}, http={code}, body={text[:300]}")
                    continue
                errors.append(f"endpoint={endpoint}, params={params}, http={code}, body={text[:300]}")

            if code >= 400:
                err_block = "\n".join(errors)
                raise RuntimeError(
                    "[Jira testcase fetch] all candidate requests failed.\n"
                    f"projectId={project_id}, startAt={start_at}, maxResults={page_size}\n"
                    f"attempts:\n{err_block}"
                )

            page_items = _parse_list_payload(data)
            if not page_items:
                break

            for item in page_items:
                uniq = item.get("id") or item.get("key") or json.dumps(item, sort_keys=True, ensure_ascii=False)
                if uniq in seen:
                    continue
                seen.add(uniq)
                all_items.append(item)

            total = _parse_total(data)
            if len(page_items) < page_size:
                break
            if total is not None and len(all_items) >= total:
                break

            start_at += page_size

    return all_items, project_key


def _build_filter_options(cases: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    option_sets = {
        "specificDut": set(),
        "lightEquipment": set(),
        "testChart": set(),
        "testScene": set(),
    }
    for case_obj in cases:
        f = _extract_case_filter_fields(case_obj)
        for key, value in f.items():
            clean = str(value or "").strip()
            if clean:
                option_sets[key].add(clean)
    return {k: sorted(v) for k, v in option_sets.items()}


def _is_filter_match(case_value: str, filter_value: str) -> bool:
    fv = str(filter_value or "").strip()
    cv = str(case_value or "").strip()
    if not fv or fv == "__ANY__":
        return True
    if fv == "__EMPTY__":
        return cv == ""
    return _normalize_text(cv) == _normalize_text(fv)


def _match_cases(cases: List[Dict[str, Any]], filters: Dict[str, str]) -> List[Dict[str, Any]]:
    matched: List[Dict[str, Any]] = []
    for case_obj in cases:
        f = _extract_case_filter_fields(case_obj)
        if not _is_filter_match(f.get("specificDut", ""), filters.get("specificDut", "")):
            continue
        if not _is_filter_match(f.get("lightEquipment", ""), filters.get("lightEquipment", "")):
            continue
        if not _is_filter_match(f.get("testChart", ""), filters.get("testChart", "")):
            continue
        if not _is_filter_match(f.get("testScene", ""), filters.get("testScene", "")):
            continue
        matched.append(case_obj)
    return matched

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
# Jira case sync + match APIs
# ----------------------------
@app.post("/api/jira/sync-cases")
async def jira_sync_cases(request: Request):
    body = await request.json()
    base = str(body.get("base") or "").strip().rstrip("/")
    project_id = str(body.get("projectId") or "").strip()
    token = str(body.get("token") or "").strip()
    username = str(body.get("user") or "").strip()
    password = str(body.get("password") or "").strip()
    insecure = bool(body.get("insecure", False))
    page_size = int(body.get("pageSize") or 200)

    if not base:
        raise HTTPException(status_code=400, detail="base is required")
    if not project_id:
        raise HTTPException(status_code=400, detail="projectId is required")
    if page_size <= 0:
        raise HTTPException(status_code=400, detail="pageSize must be > 0")
    if not token and not (username and password):
        raise HTTPException(status_code=400, detail="Auth required: token or user/password")

    lock = _get_lock("jira_sync_cases")
    if lock.locked():
        raise HTTPException(status_code=409, detail="Jira sync already in progress")

    async with lock:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth: Optional[Tuple[str, str]] = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            auth = (username, password)

        start = time.time()
        try:
            all_cases, project_key = await _fetch_project_cases(
                base=base,
                project_id=project_id,
                headers=headers,
                auth=auth,
                insecure=insecure,
                page_size=page_size,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Jira sync failed: {e}")

        filtered_cases = [x for x in all_cases if _is_case_target(x)]
        options = _build_filter_options(filtered_cases)
        duration_sec = round(time.time() - start, 3)

        cache_set(
            JIRA_CASES_CACHE_KEY,
            data={
                "cases": filtered_cases,
                "options": options,
            },
            meta={
                "projectId": project_id,
                "projectKey": project_key,
                "totalFetched": len(all_cases),
                "targetMatched": len(filtered_cases),
                "syncedAt": int(time.time()),
                "durationSec": duration_sec,
                "filters": {
                    "status": "Approved",
                    "executionType": "Auto",
                    "folderNotIn": [
                        "/DEV_NVL_HX_NIT",
                        "/FPGA_NIT",
                        "/NVL_CIT_NIT",
                        "/PIT-Lite",
                        "Blank (empty value)",
                    ],
                },
            },
        )

        return {
            "ok": True,
            "meta": _cache[JIRA_CASES_CACHE_KEY]["meta"],
            "options": options,
        }


@app.get("/api/jira/sync-cases")
async def jira_get_synced_cases_meta():
    entry = cache_get(JIRA_CASES_CACHE_KEY)
    if not entry:
        return {
            "ok": True,
            "synced": False,
            "meta": {},
            "options": {
                "specificDut": [],
                "lightEquipment": [],
                "testChart": [],
                "testScene": [],
            },
            "count": 0,
        }

    data = entry.get("data") or {}
    cases = data.get("cases") or []
    options = data.get("options") or _build_filter_options(cases)
    return {
        "ok": True,
        "synced": True,
        "meta": entry.get("meta") or {},
        "options": options,
        "count": len(cases),
    }


@app.post("/api/jira/match-cases")
async def jira_match_cases(request: Request):
    body = await request.json()
    filters = {
        "specificDut": str(body.get("specificDut") or ""),
        "lightEquipment": str(body.get("lightEquipment") or ""),
        "testChart": str(body.get("testChart") or ""),
        "testScene": str(body.get("testScene") or ""),
    }
    node_id = str(body.get("nodeId") or "").strip()

    entry = cache_get(JIRA_CASES_CACHE_KEY)
    if not entry:
        raise HTTPException(status_code=404, detail="No synced Jira cases found. Please sync first.")

    data = entry.get("data") or {}
    cases = data.get("cases") or []
    matched = _match_cases(cases, filters)

    return {
        "ok": True,
        "nodeId": node_id,
        "filters": filters,
        "total": len(matched),
        "cases": matched,
        "meta": entry.get("meta") or {},
    }


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


# ----------------------------
# Serve static files (frontend)
# ----------------------------
FRONTEND_DIR = BASE_DIR / "frontend"
INDEX_HTML = FRONTEND_DIR / "app.html"

@app.get("/")
async def serve_frontend():
    """Serve the frontend index.html"""
    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML, media_type="text/html")
    return {"detail": "Frontend not found"}

# Mount remaining static files
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")