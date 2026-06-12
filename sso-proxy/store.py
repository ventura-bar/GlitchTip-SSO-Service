from redis.asyncio import Redis

# Initialised by main.lifespan before the first request is served.
redis: Redis
