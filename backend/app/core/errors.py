"""Maps gateway failures onto HTTP responses.

A broker failure must never arrive at the client inside a 200. Each gateway
error carries the status code it should surface as, so the UI can branch on the
HTTP status alone.

The message is also made actionable where we can tell what went wrong. "Choice
rejected the session" is true but useless on its own; if the reason is that
production credentials were sent to the UAT sandbox, the response says so and
names the setting to change.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from engine.app.choice_gateway.errors import (
    ChoiceAuthError,
    ChoiceCredentialExpired,
    ChoiceGatewayError,
)
from engine.app.config import engine_settings
from engine.app.strategy_engine.dsl import DSLError

logger = logging.getLogger("api")

# Substrings Choice returns when it does not recognise the credentials at all,
# as opposed to a session that merely expired.
UNKNOWN_CREDENTIAL_MARKERS = (
    "vendorid invalid",
    "vendor id invalid",
    "doesn't exists",
    "does not exist",
    "invalid vendor",
)


# Choice returns this when the vendor is recognised but the API key it was
# given has lapsed. It is a different fix from a wrong credential: the key has
# to be reissued, and no amount of retrying or re-entering the old one helps.
EXPIRED_TOKEN_MARKERS = ("token expired", "token has expired", "expired token")


def _environment_hint(exc: ChoiceGatewayError) -> str:
    """Explain a credential rejection in terms of the action that fixes it."""
    haystack = f"{exc.message} {exc.details}".lower()

    # The gateway classifies expiry centrally, so the type is authoritative.
    # The string check remains for errors raised outside that path.
    if isinstance(exc, ChoiceCredentialExpired) or any(
        marker in haystack for marker in EXPIRED_TOKEN_MARKERS
    ):
        return (
            " Your Choice API key has expired — the Client ID is recognised, the "
            "key is not. Generate a fresh API key in the Choice Open API portal "
            "and paste it in; re-entering the old key will keep failing."
        )

    if not any(marker in haystack for marker in UNKNOWN_CREDENTIAL_MARKERS):
        return ""

    # The session's own server where the failure carries it. A user may have
    # picked the other one when connecting, and naming the deployment default
    # would send them to check the wrong credentials.
    environment = (getattr(exc, "environment", None)
                   or engine_settings.CHOICE_ENV or "UAT").upper()
    base_url = getattr(exc, "base_url", None) or engine_settings.choice_base_url

    if environment.startswith("PROD"):
        return (
            f" This session is connected to production ({base_url}). "
            "Check the Client ID and API key, and that the key has not expired."
        )
    return (
        f" This session is connected to the UAT sandbox ({base_url}), which does "
        "not recognise production credentials. Either use UAT credentials from "
        "the Choice Open API team, or disconnect and reconnect with the server "
        "set to Production."
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ChoiceGatewayError)
    async def choice_gateway_error_handler(_: Request, exc: ChoiceGatewayError):
        logger.warning("%s: %s | %s", type(exc).__name__, exc.message, exc.details)

        detail = exc.message
        # Choice's own wording is the most specific thing we have; keep it.
        if exc.details and exc.details.strip() not in detail:
            detail = f"{detail} Choice said: {exc.details.strip()}"
        if isinstance(exc, ChoiceAuthError):
            detail += _environment_hint(exc)

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": detail,
                "error": type(exc).__name__,
                "reason": exc.message,
                "upstream": exc.details or None,
                # The server this call actually failed against.
                "choice_environment": (getattr(exc, "environment", None)
                                       or engine_settings.CHOICE_ENV),
            },
        )

    @app.exception_handler(DSLError)
    async def dsl_error_handler(_: Request, exc: DSLError):
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "error": "DSLError"},
        )
