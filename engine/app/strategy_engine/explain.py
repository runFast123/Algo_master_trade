"""Render a strategy DSL back as an English sentence.

Strategies are authored as JSON. Anyone not comfortable reading JSON cannot
check what a strategy will do, and anyone who is can still misread a nested
condition. Rendering the definition back as a sentence turns "is this right?"
into a question a person can answer by reading one line.

Deliberately the cheap half of the strategy-authoring work: if the sentence
makes the DSL legible enough to edit confidently, a visual builder may not be
needed at all. That decision wants evidence, and this is what produces it.

Anything unrecognised is described literally rather than guessed at — a
description that quietly omits a condition is worse than no description.
"""

from typing import Any, Dict, List

INDICATOR_PHRASES = {
    "RSI": "{length}-period RSI",
    "SMA": "{length}-period simple moving average",
    "EMA": "{length}-period exponential moving average",
    "MACD": "MACD",
    "ATR": "{length}-period ATR",
    "VWAP": "VWAP",
    "STOCH": "{length}-period stochastic",
    "BOLLINGER": "{length}-period Bollinger band",
}

OPERATOR_PHRASES = {
    "<": "falls below",
    "<=": "is at or below",
    ">": "rises above",
    ">=": "is at or above",
    "==": "equals",
    "!=": "is not",
    "crosses_above": "crosses above",
    "crosses_below": "crosses below",
}

PRICE_FIELDS = {
    "close": "the closing price",
    "open": "the opening price",
    "high": "the high",
    "low": "the low",
    "volume": "volume",
}


def _describe_indicator(name: str, cfg: Dict[str, Any]) -> str:
    kind = str(cfg.get("type", "")).upper()
    template = INDICATOR_PHRASES.get(kind)
    if template is None:
        return f"{name} ({kind or 'unknown indicator'})"
    return template.format(length=cfg.get("length", cfg.get("period", ""))).strip()


def _field_phrase(field: Any, indicators: Dict[str, Any]) -> str:
    """Turn a condition operand into words."""
    if not isinstance(field, str):
        return str(field)
    if field in indicators:
        return _describe_indicator(field, indicators[field])
    if field in PRICE_FIELDS:
        return PRICE_FIELDS[field]
    return field


def _describe_condition(cond: Dict[str, Any], indicators: Dict[str, Any]) -> str:
    """Describe a leaf condition, or a group of them.

    A group read as "and" when it means "or" would describe a different
    strategy from the one that runs — the single thing this module exists to
    prevent. Grouped clauses are bracketed so the combination survives being
    read aloud.
    """
    if not isinstance(cond, dict):
        return str(cond)

    for key, word in (("any", "or"), ("all", "and")):
        if key in cond:
            branches = [b for b in (cond[key] or []) if isinstance(b, (dict, str))]
            if not branches:
                return f"nothing (an empty {key!r} group never fires)"
            parts = [_describe_condition(b, indicators) for b in branches]
            if len(parts) == 1:
                return parts[0]
            return "(" + f" {word} ".join(parts) + ")"

    field = _field_phrase(cond.get("field"), indicators)
    operator = str(cond.get("operator", ""))
    phrase = OPERATOR_PHRASES.get(operator, operator or "compares to")
    value = cond.get("value")
    value_phrase = _field_phrase(value, indicators) if isinstance(value, str) else value
    return f"{field} {phrase} {value_phrase}"


def _join(parts: List[str]) -> str:
    """Join clauses without hiding how they combine.

    Top-level conditions are evaluated as AND, so the word is "and" — never a
    comma that could be read as "or". Anything combined differently arrives
    already bracketed from :func:`_describe_condition`.
    """
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return " and ".join(parts)


def explain(dsl: Dict[str, Any], symbol: str = "") -> str:
    """One sentence describing what the strategy does.

    Returns a plain statement of the limits when the definition is empty or
    unreadable, rather than an empty string that reads as "nothing to see".
    """
    if not isinstance(dsl, dict):
        return "This strategy definition is not readable."

    indicators = dsl.get("indicators") or {}
    entries = dsl.get("entry_conditions") or []
    exits = dsl.get("exit_conditions") or []
    actions = dsl.get("actions") or {}

    if not entries and not exits:
        return "This strategy has no entry or exit conditions, so it will never trade."

    qty = actions.get("buy_qty") or actions.get("quantity") or 1
    instrument = symbol or dsl.get("symbol") or "the instrument"
    unit = "share" if str(qty) == "1" else "shares"

    entry_text = _join([
        _describe_condition(c, indicators) for c in entries if isinstance(c, dict)
    ])
    exit_text = _join([
        _describe_condition(c, indicators) for c in exits if isinstance(c, dict)
    ])

    sentence = (
        f"Buy {qty} {unit} of {instrument} when {entry_text}"
        if entry_text else f"Buy {qty} {unit} of {instrument}"
    )
    if exit_text:
        sentence += f"; sell when {exit_text}"

    # Risk clauses are part of what the strategy does, so they belong in the
    # sentence rather than only in the JSON.
    risk = []
    if actions.get("stop_loss_pct"):
        risk.append(f"stop out at {actions['stop_loss_pct']}% loss")
    if actions.get("target_pct"):
        risk.append(f"take profit at {actions['target_pct']}% gain")
    if risk:
        sentence += f". Also {_join(risk)}"

    return sentence + "."
