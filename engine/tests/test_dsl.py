import numpy as np
import pandas as pd
import pytest

from engine.app.strategy_engine.dsl import DSLError, dsl_engine


def frame(closes):
    n = len(closes)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": closes,
        "high": [c + 5 for c in closes],
        "low": [c - 5 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })


CLOSES = [100, 102, 101, 105, 107, 106, 110, 108, 112, 115,
          114, 118, 120, 119, 122, 125, 124, 128, 130, 132]


def test_sma_and_rsi_are_calculated():
    result = dsl_engine.calculate_indicators(
        frame(CLOSES),
        {"sma_5": {"type": "SMA", "length": 5}, "rsi_14": {"type": "RSI", "length": 14}},
    )
    assert not pd.isna(result["sma_5"].iloc[-1])
    assert not pd.isna(result["rsi_14"].iloc[-1])
    assert 0 <= result["rsi_14"].iloc[-1] <= 100


def test_rsi_uses_wilder_smoothing():
    """A steadily rising series has no losses, so Wilder's RSI is 100."""
    rising = [100 + i for i in range(30)]
    result = dsl_engine.calculate_indicators(
        frame(rising), {"rsi": {"type": "RSI", "length": 14}}
    )
    assert result["rsi"].iloc[-1] == pytest.approx(100.0)


def test_bollinger_exposes_bands_and_a_base_series():
    result = dsl_engine.calculate_indicators(
        frame(CLOSES), {"bb": {"type": "BOLLINGER", "length": 5}}
    )
    for column in ("bb", "bb_upper", "bb_middle", "bb_lower"):
        assert column in result.columns
    assert result["bb_upper"].iloc[-1] > result["bb_lower"].iloc[-1]


def test_unknown_indicator_type_raises():
    """An unrecognised indicator is an error, not silently the close price."""
    with pytest.raises(DSLError, match="Unknown indicator type"):
        dsl_engine.calculate_indicators(frame(CLOSES), {"x": {"type": "MADE_UP"}})


def test_condition_returns_a_python_bool():
    row = pd.Series({"close": 150.0, "rsi_14": 25.0, "sma_50": 140.0})
    truthy = dsl_engine.evaluate_condition(row, {"field": "rsi_14", "operator": "<", "value": 30})
    falsy = dsl_engine.evaluate_condition(row, {"field": "rsi_14", "operator": ">", "value": 70})

    assert truthy is True
    assert falsy is False
    assert not isinstance(truthy, np.bool_)


def test_condition_on_an_unknown_field_raises():
    """A misspelt field must not evaluate False forever."""
    row = pd.Series({"close": 150.0, "rsi_14": 25.0})
    with pytest.raises(DSLError, match="unknown field"):
        dsl_engine.evaluate_condition(row, {"field": "rsi_41", "operator": "<", "value": 30})


def test_condition_during_indicator_warmup_is_false():
    row = pd.Series({"close": 150.0, "rsi_14": float("nan")})
    assert dsl_engine.evaluate_condition(
        row, {"field": "rsi_14", "operator": "<", "value": 30}) is False


def test_field_to_field_comparison():
    row = pd.Series({"close": 150.0, "sma_20": 140.0})
    assert dsl_engine.evaluate_condition(
        row, {"field": "close", "operator": ">", "value": "sma_20"}) is True


def test_crossover_operators():
    prev = pd.Series({"close": 138.0, "sma_20": 140.0})
    now = pd.Series({"close": 145.0, "sma_20": 140.0})
    assert dsl_engine.evaluate_condition(
        now, {"field": "close", "operator": "crosses_above", "value": "sma_20"}, prev) is True
    assert dsl_engine.evaluate_condition(
        now, {"field": "close", "operator": "crosses_below", "value": "sma_20"}, prev) is False


def test_empty_condition_list_never_fires():
    row = pd.Series({"close": 150.0})
    assert dsl_engine.evaluate_conditions_all(row, []) is False


def test_validate_accepts_a_well_formed_strategy():
    dsl = {
        "indicators": {"rsi": {"type": "RSI", "length": 14}},
        "entry_conditions": [{"field": "rsi", "operator": "<", "value": 30}],
        "exit_conditions": [{"field": "rsi", "operator": ">", "value": 70}],
        "actions": {"buy_qty": 10},
    }
    assert dsl_engine.validate(dsl) is dsl


@pytest.mark.parametrize("dsl", [
    {"indicators": {}, "entry_conditions": [], "exit_conditions": [], "actions": {}},
    {"indicators": {"r": {"type": "RSI"}},
     "entry_conditions": [{"field": "nope", "operator": "<", "value": 1}],
     "exit_conditions": [{"field": "r", "operator": ">", "value": 9}],
     "actions": {"buy_qty": 1}},
    {"indicators": {"r": {"type": "RSI"}},
     "entry_conditions": [{"field": "r", "operator": "<", "value": 1}],
     "exit_conditions": [{"field": "r", "operator": ">", "value": 9}],
     "actions": {"buy_qty": 0}},
])
def test_validate_rejects_bad_strategies(dsl):
    with pytest.raises(DSLError):
        dsl_engine.validate(dsl)


# -- payload unwrapping ----------------------------------------------------
#
# Choice nests records differently per endpoint. Reading only the top level
# returned an empty account, which is indistinguishable from a real one.

import pytest as _pytest  # noqa: E402

from engine.app.choice_gateway.normalize import (  # noqa: E402
    failure_reason,
    is_failure,
    pick_float,
    unwrap_dict,
    unwrap_list,
)


@_pytest.mark.parametrize("payload", [
    {"Status": "Success", "Response": [{"Symbol": "RELIANCE", "NetQty": 10}]},
    {"Status": "Success", "Response": {"Holdings": [{"Symbol": "RELIANCE", "NetQty": 10}]}},
    {"Status": "Success", "Data": {"Result": [{"Symbol": "RELIANCE", "NetQty": 10}]}},
    {"Response": {"data": {"rows": [{"Symbol": "RELIANCE", "NetQty": 10}]}}},
    [{"Symbol": "RELIANCE", "NetQty": 10}],
])
def test_records_are_found_however_they_are_nested(payload):
    rows = unwrap_list(payload)
    assert len(rows) == 1
    assert rows[0]["Symbol"] == "RELIANCE"


def test_no_records_returns_empty_not_an_error():
    assert unwrap_list({"Status": "Success", "Response": []}) == []
    assert unwrap_list({"Status": "Success"}) == []


def test_funds_object_is_found_when_wrapped():
    for payload in (
        {"Status": "Success", "Response": {"AvailableMargin": 1500.0}},
        {"Status": "Success", "Response": {"Limits": {"AvailableMargin": 1500.0}}},
        {"Status": "Success", "Response": [{"AvailableMargin": 1500.0}]},
    ):
        assert pick_float(unwrap_dict(payload), "AvailableMargin") == 1500.0


def test_segment_wise_funds_are_merged():
    payload = {"Response": [
        {"SegmentId": 1, "AvailableMargin": 1500.0},
        {"SegmentId": 2, "UsedMargin": 200.0},
    ]}
    merged = unwrap_dict(payload)
    assert pick_float(merged, "AvailableMargin") == 1500.0
    assert pick_float(merged, "UsedMargin") == 200.0


def test_field_lookup_is_case_insensitive():
    assert pick_float({"availablemargin": 42.0}, "AvailableMargin") == 42.0


def test_numeric_strings_with_commas_are_parsed():
    assert pick_float({"AvailableMargin": "1,50,000.50"}, "AvailableMargin") == 150000.50


def test_zero_is_preserved_not_treated_as_missing():
    assert pick_float({"AvailableMargin": 0}, "AvailableMargin", default=999) == 0.0


def test_failure_status_is_detected():
    assert is_failure({"Status": "Failure", "Reason": "Session expired"}) is True
    assert is_failure({"Status": "Success"}) is False
    assert failure_reason({"Status": "Failure", "Reason": "Session expired"}) == "Session expired"


# -- OR groups -------------------------------------------------------------
#
# The top level stays AND, so every strategy written before this keeps its
# behaviour exactly. OR arrives by making any entry a group.

def _row(**values):
    return pd.Series(values)


ANY_GROUP = [{"any": [
    {"field": "rsi", "operator": "<", "value": 30},
    {"field": "close", "operator": ">", "value": 200},
]}]


def test_an_any_group_fires_when_either_leg_holds():
    assert dsl_engine.evaluate_conditions_all(_row(rsi=25.0, close=100.0), ANY_GROUP)
    assert dsl_engine.evaluate_conditions_all(_row(rsi=55.0, close=250.0), ANY_GROUP)


def test_an_any_group_stays_quiet_when_neither_holds():
    assert not dsl_engine.evaluate_conditions_all(_row(rsi=55.0, close=100.0), ANY_GROUP)


def test_an_all_group_needs_every_leg():
    group = [{"all": [
        {"field": "rsi", "operator": "<", "value": 30},
        {"field": "close", "operator": ">", "value": 200},
    ]}]
    assert dsl_engine.evaluate_conditions_all(_row(rsi=25.0, close=250.0), group)
    assert not dsl_engine.evaluate_conditions_all(_row(rsi=25.0, close=100.0), group)


def test_groups_nest_so_precedence_is_written_not_inferred():
    """"A and (B or C)" — the shape a joiner between flat rows cannot express
    without inventing precedence."""
    conditions = [
        {"field": "rsi", "operator": "<", "value": 40},
        {"any": [
            {"field": "close", "operator": ">", "value": 200},
            {"field": "volume", "operator": ">", "value": 1000},
        ]},
    ]
    assert dsl_engine.evaluate_conditions_all(_row(rsi=35.0, close=250.0, volume=10.0), conditions)
    assert dsl_engine.evaluate_conditions_all(_row(rsi=35.0, close=10.0, volume=5000.0), conditions)
    # The outer AND still binds: fail it and the group cannot rescue the trade.
    assert not dsl_engine.evaluate_conditions_all(_row(rsi=90.0, close=250.0, volume=5000.0), conditions)


def test_an_empty_group_never_fires():
    """Same rule as an empty condition list: specifying nothing must not mean
    trading on everything."""
    assert not dsl_engine.evaluate_conditions_all(_row(rsi=1.0), [{"any": []}])


def test_a_flat_list_still_means_and():
    """Every strategy saved before groups existed is a flat list."""
    conditions = [
        {"field": "rsi", "operator": "<", "value": 30},
        {"field": "close", "operator": ">", "value": 200},
    ]
    assert dsl_engine.evaluate_conditions_all(_row(rsi=25.0, close=250.0), conditions)
    assert not dsl_engine.evaluate_conditions_all(_row(rsi=25.0, close=100.0), conditions)


# -- validation reaches inside groups --------------------------------------

BASE = {
    "indicators": {"rsi": {"type": "RSI", "length": 14}},
    "exit_conditions": [{"field": "rsi", "operator": ">", "value": 70}],
    "actions": {"buy_qty": 1},
}


def test_a_misspelt_field_inside_a_group_is_caught_at_save_time():
    """Unrecursed, this passed validation and failed at run time — which is the
    failure the validator exists to prevent."""
    dsl = {**BASE, "entry_conditions": [{"any": [
        {"field": "rsi", "operator": "<", "value": 30},
        {"field": "typo", "operator": "<", "value": 30},
    ]}]}
    with pytest.raises(DSLError, match="typo"):
        dsl_engine.validate(dsl)


def test_an_empty_group_is_rejected_at_save_time():
    with pytest.raises(DSLError, match="empty"):
        dsl_engine.validate({**BASE, "entry_conditions": [{"any": []}]})


def test_a_group_cannot_be_both_any_and_all():
    dsl = {**BASE, "entry_conditions": [{
        "any": [{"field": "rsi", "operator": "<", "value": 30}],
        "all": [{"field": "rsi", "operator": ">", "value": 10}],
    }]}
    with pytest.raises(DSLError, match="both"):
        dsl_engine.validate(dsl)


def test_a_valid_group_saves():
    dsl_engine.validate({**BASE, "entry_conditions": [{"any": [
        {"field": "rsi", "operator": "<", "value": 30},
        {"field": "close", "operator": ">", "value": 200},
    ]}]})
