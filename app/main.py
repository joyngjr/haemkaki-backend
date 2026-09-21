import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import create_db_and_tables
from app.routers import events, plans, schedules, status, supplies, users

logging.basicConfig(level=logging.INFO)
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(
    title="HackitRx API",
    version="0.1.0",
    description="Medication supply tracking for people with haemophilia.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # Vercel preview deployments get a fresh subdomain per branch.
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers are registered here as features get built.
app.include_router(users.router)
app.include_router(events.router)
app.include_router(status.router)
app.include_router(supplies.router)
app.include_router(schedules.router)
app.include_router(plans.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
