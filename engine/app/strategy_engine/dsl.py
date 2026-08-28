"""JSON DSL interpreter: indicators and condition evaluation.

Two rules run through this module:

* An unrecognised indicator type or a condition on a field that does not exist
  is an error, not a silent no-op. Previously an unknown indicator was assigned
  the close price and a misspelt field evaluated ``False`` forever, so a broken
  strategy looked like a strategy that simply never traded.
* Everything returned is a plain Python type, never a NumPy scalar.
"""

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

SUPPORTED_INDICATORS = {
    "SMA", "EMA", "RSI", "BOLLINGER", "MACD", "ATR", "VWAP", "STOCH",
}

COMPARISONS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

CROSS_OPERATORS = {"crosses_above", "crosses_below"}


class DSLError(ValueError):
    """The strategy definition is invalid."""


class DSLEngine:
    """Interprets JSON DSL strategy definitions."""

    # -- indicators --------------------------------------------------------

    @staticmethod
    def _rsi(close: pd.Series, length: int) -> pd.Series:
        """RSI using Wilder's smoothing, as used by every charting platform.

        A simple rolling mean (the previous implementation) produces materially
        different values and would disagree with the chart a trader is reading.
        """
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
        avg_loss = loss.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        # A run with no losses is RSI 100; no gains is RSI 0.
        rsi = rsi.where(avg_loss != 0.0, 100.0)
        rsi = rsi.where(avg_gain != 0.0, 0.0)
        # Neither gains nor losses is a flat series, which is neutral — not
        # maximally oversold. The two lines above are ordered so the second
        # overwrote the first in that case, and a flat series read 0, exactly
        # like a collapse. An `rsi < 30` entry — the most common strategy in
        # this product — then fired on every flat bar, and a stale repeated
        # price produces precisely such a series. Applied last so the two
        # genuine extremes above are left intact, and the warm-up NaNs with
        # them.
        rsi = rsi.where((avg_gain != 0.0) | (avg_loss != 0.0), 50.0)
        return rsi

    @classmethod
    def calculate_indicators(
        cls, df: pd.DataFrame, indicators_config: Dict[str, Any]
    ) -> pd.DataFrame:
        df = df.copy()
        close = df["close"]

        for name, cfg in (indicators_config or {}).items():
            if not isinstance(cfg, dict):
                raise DSLError(f"Indicator {name!r} must be an object, got {type(cfg).__name__}")
            ind_type = str(cfg.get("type", "")).upper()
            length = int(cfg.get("length", 14))
            if length < 1:
                raise DSLError(f"Indicator {name!r} needs a length of at least 1")

            if ind_type == "SMA":
                df[name] = close.rolling(window=length).mean()
            elif ind_type == "EMA":
                df[name] = close.ewm(span=length, adjust=False).mean()
            elif ind_type == "RSI":
                df[name] = cls._rsi(close, length)
            elif ind_type == "BOLLINGER":
                sma = close.rolling(window=length).mean()
                std = close.rolling(window=length).std()
                mult = float(cfg.get("std_dev", 2.0))
                df[f"{name}_upper"] = sma + std * mult
                df[f"{name}_middle"] = sma
                df[f"{name}_lower"] = sma - std * mult
                # Also expose the bare name so a condition on `name` resolves
                # to the mid-band rather than silently missing.
                df[name] = sma
            elif ind_type == "MACD":
                fast = close.ewm(span=int(cfg.get("fast", 12)), adjust=False).mean()
                slow = close.ewm(span=int(cfg.get("slow", 26)), adjust=False).mean()
                macd_line = fast - slow
                signal = macd_line.ewm(
                    span=int(cfg.get("signal", 9)), adjust=False
                ).mean()
                df[name] = macd_line
                df[f"{name}_signal"] = signal
                df[f"{name}_hist"] = macd_line - signal
            elif ind_type == "ATR":
                high, low = df["high"], df["low"]
                prev_close = close.shift(1)
                true_range = pd.concat(
                    [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
                    axis=1,
                ).max(axis=1)
                df[name] = true_range.ewm(
                    alpha=1.0 / length, adjust=False, min_periods=length
                ).mean()
            elif ind_type == "VWAP":
                typical = (df["high"] + df["low"] + close) / 3.0
                cum_vol = df["volume"].cumsum()
                df[name] = (typical * df["volume"]).cumsum() / cum_vol.replace(0, np.nan)
            elif ind_type == "STOCH":
                low_n = df["low"].rolling(window=length).min()
                high_n = df["high"].rolling(window=length).max()
                span = (high_n - low_n).replace(0, np.nan)
                df[name] = 100.0 * (close - low_n) / span
            else:
                raise DSLError(
                    f"Unknown indicator type {ind_type!r} for {name!r}. "
                    f"Supported: {', '.join(sorted(SUPPORTED_INDICATORS))}"
                )

        return df

    # -- conditions --------------------------------------------------------

    @staticmethod
    def _resolve(row: pd.Series, ref: Any) -> Any:
        """Resolve a condition operand: a column reference or a literal."""
        if isinstance(ref, str):
            if ref not in row:
                raise DSLError(
                    f"Condition refers to unknown field {ref!r}. "
                    f"Available: {', '.join(map(str, row.index))}"
                )
            return row[ref]
        return ref

    @classmethod
    def evaluate_condition(
        cls,
        row: pd.Series,
        condition: Dict[str, Any],
        prev_row: Optional[pd.Series] = None,
    ) -> bool:
        field = condition.get("field")
        operator = condition.get("operator")
        value = condition.get("value")

        if not field:
            raise DSLError("Condition is missing 'field'")
        if operator not in COMPARISONS and operator not in CROSS_OPERATORS:
            raise DSLError(
                f"Unknown operator {operator!r}. Supported: "
                f"{', '.join(sorted(set(COMPARISONS) | CROSS_OPERATORS))}"
            )
        if field not in row:
            raise DSLError(
                f"Condition refers to unknown field {field!r}. "
                f"Available: {', '.join(map(str, row.index))}"
            )

        current = row[field]
        target = cls._resolve(row, value)

        # Indicators are NaN during their warm-up window; no signal yet.
        if pd.isna(current) or (target is not None and pd.isna(target)):
            return False

        if operator in CROSS_OPERATORS:
            if prev_row is None:
                return False
            prev_current = prev_row[field]
            prev_target = cls._resolve(prev_row, value)
            if pd.isna(prev_current) or pd.isna(prev_target):
                return False
            if operator == "crosses_above":
                return bool(prev_current <= prev_target and current > target)
            return bool(prev_current >= prev_target and current < target)

        return bool(COMPARISONS[operator](current, target))

    @classmethod
    def evaluate_node(
        cls,
        row: pd.Series,
        node: Dict[str, Any],
        prev_row: Optional[pd.Series] = None,
    ) -> bool:
        """Evaluate one condition, or a group of them.

        A node is either a leaf (``field``/``operator``/``value``) or a group:
        ``{"any": [...]}`` for OR, ``{"all": [...]}`` for AND. Groups nest, so
        "A and (B or C)" is expressible without inventing precedence rules.

        An empty group never fires, matching the rule for an empty condition
        list: a strategy that specifies nothing should not trade on everything.
        """
        if not isinstance(node, dict):
            raise DSLError(f"Condition must be an object, got {type(node).__name__}")

        for key, combine in (("any", any), ("all", all)):
            if key in node:
                branches = node[key]
                if not isinstance(branches, list):
                    raise DSLError(f"{key!r} must be an array of conditions")
                if not branches:
                    return False
                return combine(
                    cls.evaluate_node(row, branch, prev_row) for branch in branches
                )

        return cls.evaluate_condition(row, node, prev_row)

    @classmethod
    def evaluate_conditions_all(
        cls,
        row: pd.Series,
        conditions: List[Dict[str, Any]],
        prev_row: Optional[pd.Series] = None,
    ) -> bool:
        """All top-level entries must hold. An empty list never fires.

        The list itself is still AND — an existing strategy is a flat list of
        leaves and keeps behaving exactly as before. OR arrives by making any
        entry a group.
        """
        if not conditions:
            return False
        return all(
            cls.evaluate_node(row, cond, prev_row) for cond in conditions
        )

    # -- validation --------------------------------------------------------

    @classmethod
    def validate(cls, dsl: Dict[str, Any]) -> Dict[str, Any]:
        """Check a strategy definition up front and report every problem.

        Called when a strategy is created or updated so mistakes surface in the
        editor rather than as a strategy that quietly never trades.
        """
        if not isinstance(dsl, dict):
            raise DSLError("Strategy definition must be a JSON object")

        indicators = dsl.get("indicators", {})
        if not isinstance(indicators, dict):
            raise DSLError("'indicators' must be an object")

        known_fields = {"open", "high", "low", "close", "volume", "timestamp"}
        for name, cfg in indicators.items():
            if not isinstance(cfg, dict):
                raise DSLError(f"Indicator {name!r} must be an object")
            ind_type = str(cfg.get("type", "")).upper()
            if ind_type not in SUPPORTED_INDICATORS:
                raise DSLError(
                    f"Unknown indicator type {ind_type!r} for {name!r}. "
                    f"Supported: {', '.join(sorted(SUPPORTED_INDICATORS))}"
                )
            known_fields.add(name)
            if ind_type == "BOLLINGER":
                known_fields.update({f"{name}_upper", f"{name}_middle", f"{name}_lower"})
            elif ind_type == "MACD":
                known_fields.update({f"{name}_signal", f"{name}_hist"})

        entry = dsl.get("entry_conditions") or []
        exit_ = dsl.get("exit_conditions") or []
        if not isinstance(entry, list) or not isinstance(exit_, list):
            raise DSLError("'entry_conditions' and 'exit_conditions' must be arrays")
        if not entry:
            raise DSLError("A strategy needs at least one entry condition")
        if not exit_:
            raise DSLError(
                "A strategy needs at least one exit condition, otherwise positions "
                "are only closed when the run ends"
            )

        def check_node(node: Any, where: str) -> None:
            """Validate a leaf or a group, recursing into groups.

            Recursion matters: a misspelt field nested inside an ``any`` group
            would otherwise pass validation and fail at run time, which is the
            exact failure this validator exists to prevent.
            """
            if not isinstance(node, dict):
                raise DSLError(f"{where} must be an object")

            groups = [key for key in ("any", "all") if key in node]
            if groups:
                if len(groups) > 1:
                    raise DSLError(
                        f"{where} has both 'any' and 'all'; use one, nesting the other inside it"
                    )
                key = groups[0]
                branches = node[key]
                if not isinstance(branches, list):
                    raise DSLError(f"{where}: {key!r} must be an array of conditions")
                if not branches:
                    raise DSLError(
                        f"{where}: {key!r} is empty, so it can never be satisfied"
                    )
                for j, branch in enumerate(branches):
                    check_node(branch, f"{where} > {key}[{j}]")
                return

            field = node.get("field")
            operator = node.get("operator")
            if field not in known_fields:
                raise DSLError(
                    f"{where} refers to unknown field {field!r}. "
                    f"Available: {', '.join(sorted(known_fields))}"
                )
            if operator not in COMPARISONS and operator not in CROSS_OPERATORS:
                raise DSLError(f"{where} has unknown operator {operator!r}")
            value = node.get("value")
            if isinstance(value, str) and value not in known_fields:
                raise DSLError(f"{where} compares against unknown field {value!r}")

        for label, conditions in (("entry", entry), ("exit", exit_)):
            for i, cond in enumerate(conditions):
                check_node(cond, f"{label} condition {i}")

        actions = dsl.get("actions", {})
        if not isinstance(actions, dict):
            raise DSLError("'actions' must be an object")
        qty = actions.get("buy_qty", 1)
        if not isinstance(qty, int) or qty <= 0:
            raise DSLError(f"actions.buy_qty must be a positive integer, got {qty!r}")

        return dsl


dsl_engine = DSLEngine()
