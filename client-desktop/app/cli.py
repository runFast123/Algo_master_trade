"""Desktop entry point.

Two modes:

  ChoiceFinxTrader.exe                    launch the UI and the backend
  ChoiceFinxTrader.exe --run-backend N    run only the backend on port N

The second mode is how the launcher spawns the backend. It runs in a separate
process on purpose: this package and the backend both provide a top-level
``app`` package, so importing the backend here would resolve ``app.config`` to
the desktop client's module instead.
"""

import os
import sys


def _bundle_root() -> str:
    """Directory holding the bundled backend/ and engine/ trees."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def run_backend(port: int) -> None:
    base_dir = _bundle_root()

    # Drop any 'app' module already imported from the desktop client so the
    # backend's own package resolves cleanly.
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]

    # Backend first: it must win the 'app' package name.
    for path in (base_dir, os.path.join(base_dir, "engine"),
                 os.path.join(base_dir, "backend")):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)

    # Optional: a local checkout of the Choice SDK that is not pip-installed.
    sdk_path = os.environ.get("CHOICE_API_PATH")
    if sdk_path and os.path.isdir(sdk_path) and sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)

    import uvicorn
    from app.main import app as backend_app

    uvicorn.run(backend_app, host="127.0.0.1", port=port, log_level="warning")


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-backend":
        try:
            port = int(sys.argv[2])
        except ValueError:
            print(f"Invalid port: {sys.argv[2]!r}", file=sys.stderr)
            return 2
        run_backend(port)
        return 0

    package_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if package_dir not in sys.path:
        sys.path.insert(0, package_dir)

    from app.launcher import start_desktop_app

    start_desktop_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
