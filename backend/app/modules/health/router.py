from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.db.session import Database

router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    status: str


@router.get("/healthz", response_model=HealthStatus)
async def liveness() -> HealthStatus:
    return HealthStatus(status="ok")


@router.get("/readyz", response_model=HealthStatus, responses={503: {"model": HealthStatus}})
async def readiness(request: Request) -> HealthStatus | JSONResponse:
    db: Database = request.app.state.db
    try:
        async with db.app_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return HealthStatus(status="ok")
