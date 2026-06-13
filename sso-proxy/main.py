import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

import store
from config import REDIS_URL
from routes import auth_router, proxy_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.redis = Redis.from_url(REDIS_URL, decode_responses=True)
    yield
    await store.redis.aclose()


app = FastAPI(lifespan=lifespan)
app.include_router(auth_router)


# Register /healthz BEFORE the proxy router — the proxy has a /{path:path} catch-all
# that would intercept it if included first.
@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Liveness/readiness probe — returns 200 when Redis is reachable."""
    try:
        await store.redis.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Redis unavailable") from exc
    return JSONResponse({"status": "ok"})


app.include_router(proxy_router)
