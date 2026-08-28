"""Portfolio analytics derived from holdings the account already returns.

Nothing here needs an extra broker call. The inputs — last price, previous
close, average cost, quantity — arrive on every holdings refresh; before this
module the close was fetched, scaled and then discarded, so the app could not
answer "what happened today" despite holding the number.

Day change and total return answer different questions and are deliberately
kept apart: a book up 18% since purchase can still be down 3% this morning, and
one figure hides the other.
"""

from typing import Any, Dict, List


def _value(row: Dict[str, Any]) -> float:
    return (row.get("current_price") or 0.0) * (row.get("quantity") or 0.0)


def _cost(row: Dict[str, Any]) -> float:
    return (row.get("average_price") or 0.0) * (row.get("quantity") or 0.0)


def day_change(row: Dict[str, Any]) -> Dict[str, Any]:
    """Today's move for one holding, or nulls when the close is unavailable.

    A missing close means "not known", never zero — a holding silently counted
    as flat would drag the whole day's figure toward zero and look plausible.
    """
    last = row.get("current_price")
    close = row.get("close_price")
    quantity = row.get("quantity") or 0.0

    # `priced_from_close` means the last price was absent and the close was put
    # in its place to keep the holding in the portfolio value. Comparing that
    # against itself yields exactly 0.00, which reads as "traded flat today"
    # rather than "never priced" — the same false flat this function refuses to
    # report when the close is what is missing.
    if last is None or not close or row.get("priced_from_close"):
        return {"day_change": None, "day_change_pct": None, "day_pnl": None}

    move = last - close
    return {
        "day_change": round(move, 2),
        "day_change_pct": round((move / close) * 100, 2),
        "day_pnl": round(move * quantity, 2),
    }


def enrich(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add per-holding value, cost, return and day change."""
    enriched = []
    for row in rows:
        value, cost = _value(row), _cost(row)
        item = dict(row)
        item.update(day_change(row))
        item["value"] = round(value, 2)
        item["cost"] = round(cost, 2)
        item["return_pct"] = round(((value - cost) / cost) * 100, 2) if cost else None
        enriched.append(item)
    return enriched


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Portfolio-level figures, including concentration.

    Concentration is reported because a book where one name is 60% of the value
    carries a completely different risk from an evenly spread one, and no other
    figure on the dashboard distinguishes them.
    """
    if not rows:
        return {
            "holdings": 0, "value": 0.0, "cost": 0.0, "pnl": 0.0, "return_pct": None,
            "day_pnl": None, "day_pnl_pct": None, "day_priced": 0,
            "winners": 0, "losers": 0, "flat": 0,
            "largest_position": None, "largest_position_pct": None,
            "top3_pct": None, "concentration_hhi": None,
        }

    value = sum(r.get("value") or 0.0 for r in rows)
    cost = sum(r.get("cost") or 0.0 for r in rows)
    pnl = sum(r.get("pnl") or 0.0 for r in rows)

    # Only holdings with a usable close contribute to the day figure, and the
    # count is reported so a partial number is never read as a complete one.
    priced = [r for r in rows if r.get("day_pnl") is not None]
    day_pnl = sum(r["day_pnl"] for r in priced) if priced else None
    prior = sum(
        (r.get("close_price") or 0.0) * (r.get("quantity") or 0.0) for r in priced
    )

    ranked = sorted((r.get("value") or 0.0 for r in rows), reverse=True)
    largest = max(rows, key=lambda r: r.get("value") or 0.0)

    return {
        "holdings": len(rows),
        "value": round(value, 2),
        "cost": round(cost, 2),
        "pnl": round(pnl, 2),
        "return_pct": round((pnl / cost) * 100, 2) if cost else None,
        "day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
        "day_pnl_pct": round((day_pnl / prior) * 100, 2) if day_pnl is not None and prior else None,
        "day_priced": len(priced),
        "winners": sum(1 for r in rows if (r.get("pnl") or 0) > 0),
        "losers": sum(1 for r in rows if (r.get("pnl") or 0) < 0),
        "flat": sum(1 for r in rows if (r.get("pnl") or 0) == 0),
        "day_up": sum(1 for r in priced if r["day_pnl"] > 0),
        "day_down": sum(1 for r in priced if r["day_pnl"] < 0),
        "largest_position": largest.get("symbol"),
        "largest_position_pct": round((ranked[0] / value) * 100, 2) if value else None,
        "top3_pct": round((sum(ranked[:3]) / value) * 100, 2) if value else None,
        # Herfindahl index over position weights: 1.0 is a single holding,
        # 1/n is perfectly even. A blunt but honest concentration measure.
        "concentration_hhi": round(
            sum((v / value) ** 2 for v in ranked), 4
        ) if value else None,
    }
