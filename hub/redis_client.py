# redis_client.py
import os
from redis.asyncio import Redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

def get_redis() -> Redis:
    # decode_responses=True -> 直接返回 str，便于 JSON
    return Redis.from_url(REDIS_URL, decode_responses=True)