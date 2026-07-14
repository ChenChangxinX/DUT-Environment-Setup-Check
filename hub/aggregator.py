# aggregator.py
from typing import Optional
import httpx
from fastapi import HTTPException, Request

PC_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=300.0,
    write=60.0,
    pool=None,
)

async def call_node(
    base_url: str,
    path: str,
    method: str,
    request: Request,
):
    target_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    params = dict(request.query_params)
    body: Optional[bytes] = None

    if method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    try:
        async with httpx.AsyncClient(
            timeout=PC_TIMEOUT,
            trust_env=False
        ) as client:
            resp = await client.request(
                method=method,
                url=target_url,
                params=params,
                content=body,
            )
        resp.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(504, "PC response timeout")
    except httpx.RequestError as e:
        raise HTTPException(502, f"PC network error: {e}")

    if "application/json" in resp.headers.get("content-type",""):
        return resp.json()
    return {"text": resp.text}