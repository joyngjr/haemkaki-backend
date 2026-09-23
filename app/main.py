import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings

from app.config import get_settings
from app.db import create_db_and_tables
from app.mcp_server import mcp
from app.routers import events, imports, plans, schedules, status, supplies, translate, users

logging.basicConfig(level=logging.INFO)
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_db_and_tables()
    # The MCP transport is a route, not a mounted app, so its own lifespan never
    # runs; its session manager is started here. Without this the first /mcp
    # request fails with "Task group is not initialized".
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="HaemKaki API",
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
app.include_router(imports.router)
app.include_router(translate.router)

# Build the Streamable HTTP transport once — this is what makes `session_manager`
# exist — and register its ASGI endpoint as an exact route. `app.mount("/mcp", …)`
# would be a Mount, which only matches "/mcp/…" and answers a bare POST /mcp with
# a 307 to /mcp/ that not every MCP client follows. Stateless with JSON responses:
# Railway may run more than one instance, and no tool needs the server-to-client
# stream. DNS-rebinding protection is switched off on purpose: left unset, the SDK
# enables it with a localhost-only allow-list, which passes in Docker and rejects
# Railway's Host header with 421.
mcp.streamable_http_app(
    json_response=True,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
app.add_route("/mcp", StreamableHTTPASGIApp(mcp.session_manager))


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
