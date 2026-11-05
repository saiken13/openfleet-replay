from fastapi import APIRouter

from app.api.v1 import health, robots, runs

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(robots.router)
api_router.include_router(runs.router)
