"""Activation against the licence service, and what happens when it is silent.

Three states, and the difference between the last two is the whole design:

* **active** — checked recently, or checked long enough ago to still be inside
  the grace period.
* **revoked** — the service was reached and said no. Stop.
* **unreachable** — the service could not be reached. Keep working until the
  grace period runs out, then stop.

Conflating the last two would mean a flaky connection stops someone who is
managing a live position. That is a worse outcome than a revoked licence
running for a few more days, so the grace period is generous and the failure is
explicit rather than silent.

**Optional by construction.** With no licence server configured the app runs
exactly as before. Licensing is something the operator turns on, not a
dependency the app cannot start without — a build handed to a client before the
service exists must not become a brick when it does.
"""

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("licence")

# How long a heartbeat is trusted for. Matched by the server, which reports its
# own value; this is the fallback when nothing has been heard yet.
DEFAULT_GRACE_DAYS = 7
HEARTBEAT_SECONDS = 6 * 60 * 60
HTTP_TIMEOUT = 10


class LicenceState:
    ACTIVE = "active"
    REVOKED = "revoked"
    UNREACHABLE = "unreachable"
    UNLICENSED = "unlicensed"       # no key entered yet
    DISABLED = "disabled"           # no server configured; licensing is off


def _store_path() -> Path:
    from engine.app.env_paths import user_config_dir
    return user_config_dir() / "licence.json"


def _read() -> Dict[str, Any]:
    try:
        return json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(data: Dict[str, Any]) -> None:
    try:
        _store_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        # Losing the record means re-entering the key next launch, which is
        # annoying rather than dangerous — never a reason to refuse to start.
        logger.warning("Could not store the licence record: %s", exc)


def install_id() -> str:
    """A random id for this installation, generated once and kept.

    Random rather than derived from the machine: a hostname or a MAC address
    would identify the person, and nothing here needs to. It only has to be
    stable enough to count seats.
    """
    data = _read()
    existing = data.get("install_id")
    if existing:
        return existing
    generated = "install-" + uuid.uuid4().hex
    data["install_id"] = generated
    _write(data)
    return generated


def _post(base_url: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def activate(base_url: str, key: str, app_version: str = "",
             environment: str = "", label: str = "") -> Dict[str, Any]:
    """Register this installation. Returns the service's answer, or raises."""
    payload = {"key": key.strip(), "install_id": install_id(),
               "app_version": app_version, "environment": environment,
               "label": label}
    try:
        answer = _post(base_url, "/api/activate", payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"Activation refused: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"Could not reach the licence service: {exc}") from exc

    data = _read()
    data.update(key=key.strip(), activated_at=time.time(),
                last_verified=time.time(),
                grace_days=answer.get("grace_days", DEFAULT_GRACE_DAYS),
                client_name=answer.get("client_name", ""))
    _write(data)
    return answer


def check(base_url: str, app_version: str = "", environment: str = "") -> str:
    """Heartbeat, then decide whether this installation may run.

    Called at startup and periodically. Never raises: an unreachable service is
    an expected condition, not an error, and the caller needs a verdict rather
    than an exception to handle.
    """
    if not base_url:
        return LicenceState.DISABLED

    data = _read()
    key = data.get("key")
    if not key:
        return LicenceState.UNLICENSED

    try:
        answer = _post(base_url, "/api/heartbeat", {
            "key": key, "install_id": install_id(),
            "app_version": app_version, "environment": environment})
    except Exception as exc:                      # noqa: BLE001 - see docstring
        logger.info("Licence service unreachable (%s); using the grace period.", exc)
        return _grace_verdict(data)

    status = answer.get("status")
    if status == "active":
        data["last_verified"] = time.time()
        data["grace_days"] = answer.get("grace_days", DEFAULT_GRACE_DAYS)
        _write(data)
        return LicenceState.ACTIVE

    # "unknown" means the service has no record of this key — a deleted licence
    # is as much a refusal as a revoked one.
    return LicenceState.REVOKED


def _grace_verdict(data: Dict[str, Any]) -> str:
    last = data.get("last_verified")
    if not last:
        # Never successfully checked in. Nothing has been granted to extend.
        return LicenceState.UNLICENSED

    grace = float(data.get("grace_days", DEFAULT_GRACE_DAYS)) * 86400
    if (time.time() - float(last)) <= grace:
        return LicenceState.ACTIVE
    return LicenceState.UNREACHABLE


def describe() -> Dict[str, Any]:
    """What the interface shows: who this is licensed to, and how fresh."""
    data = _read()
    last = data.get("last_verified")
    return {
        "licensed_to": data.get("client_name", ""),
        "key": data.get("key", ""),
        "install_id": data.get("install_id", ""),
        "last_verified": last,
        "grace_days": data.get("grace_days", DEFAULT_GRACE_DAYS),
        "days_since_check": round((time.time() - float(last)) / 86400, 1) if last else None,
    }


def message_for(state: str, grace_days: int = DEFAULT_GRACE_DAYS) -> Optional[str]:
    """What to tell the user. None when there is nothing to say."""
    return {
        LicenceState.REVOKED:
            "This licence has been withdrawn. Contact the platform operator. "
            "Any open positions are unaffected — they are held at your broker, "
            "not here.",
        LicenceState.UNREACHABLE:
            f"The licence service has not been reachable for over {grace_days} "
            "days, so this copy has stopped. Reconnect to the internet and "
            "restart, or contact the platform operator.",
        LicenceState.UNLICENSED:
            "This copy has not been activated. Enter the licence key you were "
            "given.",
    }.get(state)
