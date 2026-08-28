"""Desktop launcher: start the backend, serve the UI, open the browser."""

import logging
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from .config import desktop_config

logger = logging.getLogger("launcher")


class _QuietPeerDisconnect(logging.Filter):
    """Stop a browser hanging up being reported as an application error.

    Windows' asyncio proactor raises ConnectionResetError from the callback
    that tears a connection down, and asyncio logs any unhandled callback
    exception at ERROR with a traceback. A browser closing a tab, refreshing
    mid-request, or dropping a speculative connection all land here — nothing
    is lost, the next request succeeds, and yet the console shows a stack trace
    labelled ERROR in an application where a real fault costs money.

    Scoped to the two exception types that mean "the peer went away" and only
    on the asyncio logger, so an actual failure still gets through. The exact
    trigger was not reproducible outside a real browser: three candidate causes
    were tested against a bare server and none of them logged this, so this
    suppresses a symptom whose cause is not established. It is deliberately
    narrow for that reason.
    """

    BENIGN = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, self.BENIGN) and "_call_connection_lost" in record.getMessage():
            logger.debug("Client disconnected before the response completed: %s", exc)
            return False
        return True


def quieten_peer_disconnects() -> None:
    """Install the filter. Safe to call more than once."""
    log = logging.getLogger("asyncio")
    if not any(isinstance(f, _QuietPeerDisconnect) for f in log.filters):
        log.addFilter(_QuietPeerDisconnect())


def find_available_port(start_port: int, max_attempts: int = 40) -> int:
    """First free loopback port at or after ``start_port``."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"No free port between {start_port} and {start_port + max_attempts}"
    )


def start_backend(port: int) -> subprocess.Popen:
    """Spawn the backend in its own process.

    Isolation matters: the desktop client and the backend both define a
    top-level ``app`` package, so importing the backend into this process
    would resolve ``app.config`` to the wrong module.
    """
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--run-backend", str(port)]
    else:
        command = [sys.executable, sys.argv[0], "--run-backend", str(port)]

    print(f"Starting backend service on http://127.0.0.1:{port} ...", flush=True)
    return subprocess.Popen(command)


def wait_for_backend(port: int, timeout: float) -> bool:
    """Poll the backend health endpoint until it answers or time runs out."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def open_browser_when_ready(ui_port: int, backend_port: int) -> None:
    """Open the browser only once the backend can actually serve the UI's calls."""
    if not wait_for_backend(backend_port, desktop_config.BACKEND_STARTUP_TIMEOUT):
        print(
            "Warning: the backend did not start in time. The window will open, "
            "but data will not load until it is available.",
            flush=True,
        )
    url = f"http://127.0.0.1:{ui_port}"
    print(f"Opening {url} ...", flush=True)
    webbrowser.open(url)


def check_licence_or_exit() -> None:
    """Verify activation before anything starts.

    Off unless a licence server is configured, so an unlicensed build keeps
    working exactly as it did. An unreachable service inside the grace period
    is treated as fine: the alternative is stopping someone who may be
    managing a live position because their wifi dropped.
    """
    from . import licence

    base = desktop_config.LICENCE_SERVER_URL
    state = licence.check(base, app_version=desktop_config.APP_VERSION)
    if state in (licence.LicenceState.ACTIVE, licence.LicenceState.DISABLED):
        return

    if state == licence.LicenceState.UNLICENSED:
        print(flush=True)
        print("This copy has not been activated.", flush=True)
        key = input(
            "Enter the licence key you were given (or press Enter to quit): "
        ).strip()
        if not key:
            raise SystemExit(1)
        try:
            answer = licence.activate(
                base, key, app_version=desktop_config.APP_VERSION)
        except RuntimeError as exc:
            print(flush=True)
            print(str(exc), flush=True)
            raise SystemExit(1)
        print(f"Activated for {answer.get('client_name', 'this licence')}.",
              flush=True)
        return

    print(flush=True)
    print(licence.message_for(state) or "This copy cannot run.", flush=True)
    raise SystemExit(1)


def start_licence_heartbeat() -> None:
    """Re-check periodically, in the background.

    Only reports; it never stops a running app. Pulling the floor out from
    under someone mid-session is worse than letting a withdrawn licence run
    until the next launch, which is at most a working day away.
    """
    if not desktop_config.LICENCE_SERVER_URL:
        return

    from . import licence

    def beat():
        while True:
            time.sleep(licence.HEARTBEAT_SECONDS)
            state = licence.check(desktop_config.LICENCE_SERVER_URL,
                                  app_version=desktop_config.APP_VERSION)
            if state not in (licence.LicenceState.ACTIVE, licence.LicenceState.DISABLED):
                logger.warning("Licence check: %s. %s", state,
                               licence.message_for(state) or "")

    threading.Thread(target=beat, name="licence-heartbeat", daemon=True).start()


def start_desktop_app() -> None:
    from .local_server import app

    check_licence_or_exit()
    start_licence_heartbeat()

    backend_port = find_available_port(desktop_config.BACKEND_PORT)
    desktop_config.BACKEND_URL = f"http://127.0.0.1:{backend_port}"

    ui_port = find_available_port(desktop_config.LOCAL_PORT)
    desktop_config.LOCAL_PORT = ui_port

    backend_process = start_backend(backend_port)
    threading.Thread(
        target=open_browser_when_ready, args=(ui_port, backend_port), daemon=True
    ).start()

    print(f"{desktop_config.APP_NAME} running on http://127.0.0.1:{ui_port}", flush=True)
    quieten_peer_disconnects()
    try:
        # Loopback only: the desktop app must not be reachable from the network.
        uvicorn.run(app, host="127.0.0.1", port=ui_port, log_level="info")
    finally:
        if backend_process.poll() is None:
            backend_process.terminate()
            try:
                backend_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                backend_process.kill()
        print("Shut down.", flush=True)
