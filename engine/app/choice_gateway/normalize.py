"""Field extraction helpers for Choice OpenAPI payloads.

Two problems this solves.

**Naming.** Choice returns the same logical field under several different names
depending on the endpoint and version. The helpers pick the first key that is
actually *present*, which is not the same as the first key that is truthy: a
real balance of 0, a flat position of 0 quantity and a P&L of exactly 0 are all
legitimate values that must survive normalisation intact.

**Nesting.** The payload envelope varies too. Records may sit directly under
``Response``, or one level deeper under a named collection
(``{"Response": {"Holdings": [...]}}``). Looking only at the top level finds
nothing and reports an empty account, which is indistinguishable from a real
empty account — so the search descends until it finds records.
"""

from typing import Any, Dict, List, Optional

# Envelope keys Choice wraps payloads in, in the order they should be tried.
ENVELOPE_KEYS = ("Response", "response", "Data", "data", "Result", "result")

# Values in a Status field that mean the call did not succeed.
FAILURE_STATUSES = {"failure", "fail", "error", "false"}

MAX_SEARCH_DEPTH = 5


def pick(data: Dict[str, Any], *keys: str) -> Optional[Any]:
    """Return the first key present with a non-None value, else None.

    Matching is case-insensitive on a second pass, because Choice is not
    consistent about casing between endpoints.
    """
    if not isinstance(data, dict):
        return None

    for key in keys:
        if key in data and data[key] is not None:
            return data[key]

    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(str(key).lower())
        if value is not None:
            return value
    return None


def pick_float(data: Dict[str, Any], *keys: str, default: Optional[float] = None):
    value = pick(data, *keys)
    if value is None:
        return default
    try:
        # Choice sometimes sends numbers as strings, occasionally with commas.
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if not value:
                return default
        return float(value)
    except (TypeError, ValueError):
        return default


def pick_int(data: Dict[str, Any], *keys: str, default: Optional[int] = None):
    value = pick_float(data, *keys)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pick_str(data: Dict[str, Any], *keys: str, default: str = "") -> str:
    value = pick(data, *keys)
    return default if value is None else str(value).strip()


# Choice sends prices as scaled integers with the scale alongside them: a
# PriceDivisor of 100 means the price is in paise. Every price read from a
# Choice record goes through scaled_price — reading one directly is how a
# portfolio came to be displayed a hundredfold too large.
DIVISOR_KEYS = ("PriceDivisor", "priceDivisor", "Divisor")


def scaled_price(data: Dict[str, Any], *keys: str) -> Optional[float]:
    """Read a price and apply the record's own scaling factor.

    A record with no divisor is left alone, so this is safe on payloads that
    already report rupees.
    """
    raw = pick_float(data, *keys)
    if raw is None:
        return None

    divisor = pick_float(data, *DIVISOR_KEYS, default=1.0)
    if not divisor or divisor <= 0:
        divisor = 1.0
    return raw / divisor


def is_failure(response: Any) -> bool:
    """True when the payload carries an explicit failure status."""
    if not isinstance(response, dict):
        return False
    status = pick(response, "Status", "status")
    return status is not None and str(status).strip().lower() in FAILURE_STATUSES


def failure_reason(response: Any) -> str:
    """The most specific explanation the payload carries.

    Choice puts the reason in ``Reason`` on some endpoints and in ``Response``
    on others - MultipleTouchline returns a plain string there - so both are
    consulted and combined.
    """
    if not isinstance(response, dict):
        return ""

    parts = []
    reason = pick_str(response, "Reason", "reason", "Message", "message",
                      "Error", "error")
    if reason:
        parts.append(reason)

    # A string Response on a failed call is the error text, not a payload.
    body = response.get("Response")
    if isinstance(body, str) and body.strip() and body.strip() not in parts:
        parts.append(body.strip())

    return " | ".join(parts) or "Choice reported a failure without a reason"


def _as_record_collection(node: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Recognise a mapping that is really a collection of records.

    Choice returns holdings as an object keyed by ISIN rather than as a list::

        {"lDictStockViewHoldingData": {"INE002A01018": {...}, "INE009A01021": {...}}}

    Every value is a record of the same shape, so this is a collection wearing a
    dictionary's clothes. The key is preserved on each record as ``_record_key``
    because it carries meaning — the ISIN, in that example.
    """
    values = list(node.values())
    if len(values) < 1 or not all(isinstance(v, dict) for v in values):
        return None
    # Records have substance and a shared shape; an envelope does not.
    if not all(len(v) >= 3 for v in values):
        return None
    first_keys = set(values[0].keys())
    if not all(set(v.keys()) == first_keys for v in values):
        return None

    return [dict(record, _record_key=key) for key, record in node.items()]


def _find_records(node: Any, depth: int = 0) -> List[Dict[str, Any]]:
    """Depth-first search for the first collection of dict records."""
    if depth > MAX_SEARCH_DEPTH:
        return []

    if isinstance(node, list):
        records = [item for item in node if isinstance(item, dict)]
        return records if records else []

    if isinstance(node, dict):
        # Prefer the conventional envelope keys before scanning everything.
        for key in ENVELOPE_KEYS:
            if key in node:
                found = _find_records(node[key], depth + 1)
                if found:
                    return found

        for key, value in node.items():
            if key in ENVELOPE_KEYS:
                continue
            if isinstance(value, dict):
                collection = _as_record_collection(value)
                if collection:
                    return collection
            if isinstance(value, (list, dict)):
                found = _find_records(value, depth + 1)
                if found:
                    return found

        # The node may itself be the keyed collection.
        collection = _as_record_collection(node)
        if collection:
            return collection
    return []


def unwrap_list(response: Any) -> List[Dict[str, Any]]:
    """Pull the record list out of a Choice envelope, however deeply nested."""
    return _find_records(response)


def unwrap_dict(response: Any) -> Dict[str, Any]:
    """Pull the record object out of a Choice envelope.

    When the payload turns out to be a list of records — segment-wise funds,
    for instance — they are merged into one mapping, first value winning, so a
    caller looking for a single figure still finds it.
    """
    if not isinstance(response, dict):
        if isinstance(response, list):
            return _merge(response)
        return {}

    for key in ENVELOPE_KEYS:
        if key not in response:
            continue
        payload = response[key]
        if isinstance(payload, dict):
            # Descend through a single-key wrapper such as {"Data": {...}}.
            inner = _single_dict_child(payload)
            return inner if inner is not None else payload
        if isinstance(payload, list):
            merged = _merge(payload)
            if merged:
                return merged

    # No recognised envelope: treat the response itself as the record, unless
    # it only carries status metadata.
    stripped = {
        k: v for k, v in response.items()
        if str(k).lower() not in {"status", "reason", "message"}
    }
    return stripped or response


def _single_dict_child(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Unwrap {"Holdings": {...}} to the inner mapping, when unambiguous."""
    children = [v for v in payload.values() if isinstance(v, dict)]
    if len(payload) == 1 and len(children) == 1:
        return children[0]
    return None


def _merge(records: List[Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        for key, value in record.items():
            if key not in merged and value is not None:
                merged[key] = value
    return merged


def is_keyed_collection(payload: Any) -> bool:
    """True when a dict is a collection keyed by data rather than a record.

    The distinction decides whether the keys are safe to report. Choice returns
    holdings as ``{"INE009A01021": {...}, "INE018E01016": {...}}`` — the keys
    are ISINs, which name the securities the account holds. A funds record, by
    contrast, is ``{"LedgerBalance": 0.0, "TodaysBalance": 0.0, ...}``, where
    the keys are field names and are exactly what a diagnostic exists to show.

    The reliable signal is the values: a collection's values are dicts that all
    share one schema, a record's values are scalars. Two entries is enough to
    establish a repeated schema.
    """
    # One entry counts. Requiring two left an account holding a single
    # security disclosing that security's ISIN — the narrow case, but exactly
    # the one the redaction exists for.
    if not isinstance(payload, dict) or not payload:
        return False
    values = list(payload.values())
    if not all(isinstance(v, dict) for v in values):
        return False
    first = set(values[0].keys())
    if not first or not all(set(v.keys()) == first for v in values[1:]):
        return False
    # Field names are written like words; identifiers carry digits. `ISIN
    # INE002A01018`, a token `2885` and a segment `1` are all data and must be
    # withheld; `FundsViewNew`, `LedgerBalance` and `lstMktStatus` name a field
    # and are the whole point of the report, so they stay.
    return not all(str(k).replace("_", "").isalpha() for k in payload)


def describe_shape(payload: Any, depth: int = 0) -> Any:
    """A structural summary of a payload, for diagnostics.

    Returns field names and value types rather than values, so a response can
    be inspected — and shared with someone else to get a mapping corrected —
    without exposing balances or holdings.

    Keys are only safe when they are field names. Where a dict turns out to be
    a collection keyed by data, the keys are replaced by a count: reporting
    them disclosed every ISIN on the account, which is the holdings list under
    another name, and it did so even with values switched off.
    """
    if depth > 4:
        return "..."
    if isinstance(payload, dict):
        if is_keyed_collection(payload):
            sample = describe_shape(next(iter(payload.values())), depth + 1)
            return {f"<{len(payload)} entries, keys withheld>": sample}
        return {k: describe_shape(v, depth + 1) for k, v in list(payload.items())[:40]}
    if isinstance(payload, list):
        if not payload:
            return "[] (empty list)"
        return [describe_shape(payload[0], depth + 1), f"... {len(payload)} items"]
    return type(payload).__name__
