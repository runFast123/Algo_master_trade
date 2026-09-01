try:
    from pydantic_settings import BaseSettings
except ImportError:  # pragma: no cover - pydantic v1 fallback
    from pydantic import BaseSettings


def _baked_licence_server() -> str:
    """The licence server this build was compiled for, if any.

    Absent on a normal build, which is what makes licensing opt-in. The module
    is regenerated on every build — never left over — so a build meant to be
    unlicensed cannot silently inherit yesterday's URL.
    """
    try:
        from ._build import LICENCE_SERVER_URL
        return LICENCE_SERVER_URL
    except Exception:
        return ""


class DesktopConfig(BaseSettings):
    APP_NAME: str = "Choice FINX Algo Trading Platform"
    APP_VERSION: str = "1.2.9"
    GITHUB_REPO: str = "runFast123/Algo_master_trade"

    # Preferred ports; the launcher moves to the next free one if taken.
    LOCAL_PORT: int = 9000
    BACKEND_PORT: int = 8080

    # Set by the launcher once the backend port is known. 127.0.0.1 rather than
    # "localhost" so httpx cannot resolve it to an IPv6 address the backend is
    # not listening on.
    BACKEND_URL: str = "http://127.0.0.1:8080"

    # A backtest over a long range can take a while; allow for it.
    PROXY_TIMEOUT: float = 120.0

    # How long to wait for the backend subprocess to become reachable.
    BACKEND_STARTUP_TIMEOUT: float = 60.0

    # Where activation is checked. Empty means licensing is off entirely, which
    # is the default: a build handed to someone before the service exists must
    # keep working when it does.
    #
    # Written by `build_exe.py --licence-server URL` into a generated module,
    # because the value has to travel inside the executable — it cannot be read
    # from the environment of a machine the app has not reached yet.
    LICENCE_SERVER_URL: str = _baked_licence_server()


desktop_config = DesktopConfig()
