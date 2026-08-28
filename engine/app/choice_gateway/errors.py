"""Typed errors for the Choice gateway.

Every failure path in the gateway raises one of these instead of returning a
success-shaped payload. Callers decide how to surface them; nothing is
silently swallowed.
"""


class ChoiceGatewayError(Exception):
    """Base class for every Choice gateway failure."""

    status_code = 502

    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.message = message
        self.details = details


class ChoiceNotConnected(ChoiceGatewayError):
    """No usable Choice session for this user."""

    status_code = 409


class ChoiceAuthError(ChoiceGatewayError):
    """Login or session validation failed / the session expired."""

    status_code = 401


class ChoiceCredentialExpired(ChoiceAuthError):
    """Choice accepted the Client ID and refused the API key as expired.

    A distinct type rather than a message to match on: the remedy differs from
    every other auth failure — the key has to be reissued, and retrying or
    re-entering the same key cannot succeed. Callers branch on the type, so the
    classification lives in exactly one place instead of a string comparison
    repeated at each call site.
    """

    status_code = 401


class ChoiceUpstreamError(ChoiceGatewayError):
    """Choice OpenAPI returned an error or was unreachable."""

    status_code = 502


class ChoiceRateLimited(ChoiceGatewayError):
    """Local throttle tripped before the request left the process."""

    status_code = 429


class OrderRejected(ChoiceGatewayError):
    """The order was refused, either by our risk checks or by Choice."""

    status_code = 400
