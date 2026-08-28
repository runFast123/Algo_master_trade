"""Indian retail transaction costs.

This lived inside ``backtest_runner`` and was reachable only from a backtest,
which left the order ticket quieter about fees than the backtester was. It is
shared now: a backtest and a pre-trade preview quote the same schedule, so what
a strategy is tested against is what a manual order is charged.

Rates follow the common retail delivery schedule and are overridable, because
brokerage differs per account and a wrong default quoted confidently is worse
than one the user can correct.
"""

from typing import Any, Dict


def for_product(product_type: str = "CNC") -> "CostModel":
    """A cost model matching the product the order is actually placed under.

    `preview_order` took no product and always used the delivery schedule, so
    an intraday preview overstated the charges and quoted a break-even price
    that could never be reached.
    """
    if str(product_type or "").strip().upper() in {"MIS", "CO", "BO"}:
        return CostModel(**CostModel.INTRADAY)
    return CostModel()


class CostModel:
    """Indian equity delivery cost model.

    Defaults follow the common retail schedule; override per run when the
    account's actual brokerage differs.
    """

    # Intraday is a different schedule, not a discount on the same one: STT is
    # charged on the sell leg only and at a quarter the rate, and stamp duty is
    # a fifth. Quoting the delivery schedule for an MIS order overstated STT by
    # the whole buy leg — on a 500,000 turnover, 500 rupees of tax that is not
    # charged — and the break-even price was wrong by the same amount.
    INTRADAY = {"stt_pct": 0.00025, "stt_on_sell_only": True,
                "stamp_duty_pct": 0.00003}

    def __init__(
        self,
        brokerage_pct: float = 0.0003,      # 0.03%, capped per order
        brokerage_cap: float = 20.0,
        stt_pct: float = 0.001,             # 0.1% on both legs (delivery)
        exchange_pct: float = 0.0000345,
        sebi_pct: float = 0.000001,
        stamp_duty_pct: float = 0.00015,    # buy side only
        gst_pct: float = 0.18,              # on brokerage + exchange charges
        slippage_pct: float = 0.0005,       # 5 bps against the fill
        stt_on_sell_only: bool = False,
    ):
        self.stt_on_sell_only = stt_on_sell_only
        self.brokerage_pct = brokerage_pct
        self.brokerage_cap = brokerage_cap
        self.stt_pct = stt_pct
        self.exchange_pct = exchange_pct
        self.sebi_pct = sebi_pct
        self.stamp_duty_pct = stamp_duty_pct
        self.gst_pct = gst_pct
        self.slippage_pct = slippage_pct

    def fill_price(self, price: float, side: str) -> float:
        """Apply slippage against the trader."""
        drift = price * self.slippage_pct
        return price + drift if side == "BUY" else price - drift

    def _components(self, turnover: float, side: str) -> Dict[str, float]:
        brokerage = min(turnover * self.brokerage_pct, self.brokerage_cap)
        exchange = turnover * self.exchange_pct
        return {
            "brokerage": brokerage,
            "stt": (0.0 if (self.stt_on_sell_only and side == "BUY")
                    else turnover * self.stt_pct),
            "exchange": exchange,
            "sebi": turnover * self.sebi_pct,
            "stamp_duty": turnover * self.stamp_duty_pct if side == "BUY" else 0.0,
            "gst": (brokerage + exchange) * self.gst_pct,
        }

    def charges(self, turnover: float, side: str) -> float:
        """Total charges, unrounded — backtests compound this over many trades,
        so rounding here would drift the equity curve."""
        return sum(self._components(turnover, side).values())

    def breakdown(self, turnover: float, side: str) -> Dict[str, float]:
        """The same charges itemised and rounded, for display in a preview."""
        return {k: round(v, 2) for k, v in self._components(turnover, side).items()}

    def describe(self) -> Dict[str, float]:
        return {
            "brokerage_pct": self.brokerage_pct,
            "brokerage_cap": self.brokerage_cap,
            "stt_pct": self.stt_pct,
            "stamp_duty_pct": self.stamp_duty_pct,
            "gst_pct": self.gst_pct,
            "slippage_pct": self.slippage_pct,
        }


def preview_order(
    side: str,
    quantity: int,
    price: float,
    cost_model: CostModel = None,
    product_type: str = "CNC",
) -> Dict[str, Any]:
    """What an order will actually cost, before it is sent.

    ``breakeven_price`` is the price the instrument has to reach for a buyer to
    exit at no loss *after paying the exit charges too* — the number people get
    wrong when they estimate fees on one leg only.
    """
    model = cost_model or for_product(product_type)
    side = (side or "BUY").upper()
    quantity = int(quantity or 0)
    price = float(price or 0.0)

    turnover = price * quantity
    breakdown = model.breakdown(turnover, side)
    charges = round(model.charges(turnover, side), 2)

    if side == "BUY":
        net = turnover + charges              # cash out of the account
        effective = net / quantity if quantity else 0.0
        exit_charges = sum(model.breakdown(turnover, "SELL").values())
        breakeven = (net + exit_charges) / quantity if quantity else 0.0
    else:
        net = turnover - charges              # cash into the account
        effective = net / quantity if quantity else 0.0
        breakeven = effective

    return {
        "side": side,
        "quantity": quantity,
        "price": round(price, 2),
        "turnover": round(turnover, 2),
        "charges": charges,
        "charge_breakdown": breakdown,
        "net_amount": round(net, 2),
        "effective_price": round(effective, 4),
        "breakeven_price": round(breakeven, 4),
        "charges_pct": round((charges / turnover * 100), 4) if turnover else 0.0,
    }
