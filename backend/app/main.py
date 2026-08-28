import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.config import settings
from app.core.errors import register_exception_handlers
from app.db_migrate import sync_schema
from engine.app.config import engine_settings
from engine.app.env_paths import ENV_FILE, candidate_env_files

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("app")

sync_schema()

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
)

# Only the origins configured for this deployment may call the API from a
# browser. The desktop client reaches the API through its own local proxy, so
# it is same-origin and needs no entry here. Credentials are only allowed when
# an explicit origin list exists - "*" with credentials is rejected by browsers
# anyway and would be unsafe if it were not.
if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

register_exception_handlers(app)
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.on_event("startup")
def log_startup_configuration() -> None:
    # Make the trading environment unmistakable in the logs. A deployment
    # pointed at production should never be a surprise.
    banner = "PRODUCTION - REAL ORDERS" if engine_settings.is_production else "UAT sandbox"
    # "Default", not "environment": each connection now picks its own server,
    # so this line describes what a user gets if they do not choose, not what
    # every session is using.
    logger.info("Choice environment (default): %s (%s)",
                engine_settings.CHOICE_ENV, banner)
    logger.info("Choice base URL (default): %s", engine_settings.choice_base_url)
    logger.info("Users may select UAT or PROD per connection.")
    logger.info("Order rate limit: %s/sec", engine_settings.ORDER_RATE_LIMIT_PER_SEC)
    # Say where settings came from, so "why is it still on UAT" is answerable
    # without guessing which .env the process picked up.
    if Path(ENV_FILE).is_file():
        logger.info("Configuration file: %s", Path(ENV_FILE).resolve())
    else:
        logger.info(
            "No .env found; using defaults. Searched: %s",
            ", ".join(str(p) for p in candidate_env_files()),
        )
    if engine_settings.is_production:
        logger.warning(
            "Live order routing is enabled. Orders must originate from the static "
            "IP declared with Choice, or the exchange will reject them."
        )

    # A paper run lives in memory. If the process died while one was active the
    # row still claims RUNNING, and the interface would show a strategy trading
    # when nothing is. Correct the record before anyone reads it.
    from app.services.paper_run_service import recover_orphaned_runs

    recovered = recover_orphaned_runs()
    if recovered:
        logger.warning(
            "%d paper run(s) were interrupted by a restart and have been marked "
            "as such. Positions they held were not carried over.", recovered
        )


@app.get("/")
def root():
    return {
        "message": f"Welcome to {settings.PROJECT_NAME}",
        "docs": f"{settings.API_V1_STR}/docs",
        "version": settings.VERSION,
        "choice_environment_default": engine_settings.CHOICE_ENV,
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": settings.VERSION}


if __name__ == "__main__":
    import uvicorn

    # Loopback by default; set HOST explicitly to expose the API.
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
