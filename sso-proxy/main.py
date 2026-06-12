import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

from contextlib import asynccontextmanager

from fastapi import FastAPI
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
app.include_router(proxy_router)
