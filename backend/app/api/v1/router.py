from fastapi import APIRouter

from app.api.v1 import auth, health, incidents, robots, runs, triage, ws

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(robots.router)
api_router.include_router(runs.router)
api_router.include_router(incidents.router)
api_router.include_router(triage.router)
api_router.include_router(ws.router)
