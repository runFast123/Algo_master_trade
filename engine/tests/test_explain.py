"""Plain-English rendering of a strategy DSL.

The point is that a trader can check what a strategy does without reading
JSON, so the tests assert on the sentence a person would read — not on
internal structure.
"""

import pytest

from engine.app.strategy_engine.explain import explain

RSI_DIP = {
    "indicators": {"r": {"type": "RSI", "length": 14}},
    "entry_conditions": [{"field": "r", "operator": "<", "value": 30}],
    "exit_conditions": [{"field": "r", "operator": ">", "value": 70}],
    "actions": {"buy_qty": 1},
}


def test_the_canonical_example_reads_as_english():
    assert explain(RSI_DIP, "RELIANCE") == (
        "Buy 1 share of RELIANCE when 14-period RSI falls below 30; "
        "sell when 14-period RSI rises above 70."
    )


def test_plural_shares():
    assert "Buy 50 shares" in explain({**RSI_DIP, "actions": {"buy_qty": 50}}, "INFY")


def test_a_crossover_between_two_indicators_names_both():
    dsl = {
        "indicators": {"fast": {"type": "EMA", "length": 9},
                       "slow": {"type": "EMA", "length": 21}},
        "entry_conditions": [{"field": "fast", "operator": "crosses_above",
                              "value": "slow"}],
        "exit_conditions": [],
        "actions": {"buy_qty": 10},
    }
    sentence = explain(dsl, "TCS")

    assert "9-period exponential moving average crosses above" in sentence
    assert "21-period exponential moving average" in sentence


def test_risk_clauses_are_part_of_the_sentence():
    """Stops and targets are what the strategy does, so hiding them in JSON
    while describing only the entry would be a misleading description."""
    dsl = {**RSI_DIP, "actions": {"buy_qty": 1, "stop_loss_pct": 2, "target_pct": 5}}
    sentence = explain(dsl, "RELIANCE")

    assert "stop out at 2% loss" in sentence
    assert "take profit at 5% gain" in sentence


def test_multiple_conditions_join_with_and_not_a_comma():
    """Conditions are ANDed. A comma could be read as "or", which would
    describe a different strategy."""
    dsl = {
        "indicators": {"r": {"type": "RSI", "length": 14},
                       "m": {"type": "SMA", "length": 50}},
        "entry_conditions": [{"field": "r", "operator": "<", "value": 30},
                             {"field": "close", "operator": ">", "value": "m"}],
        "exit_conditions": [],
        "actions": {},
    }
    assert " and " in explain(dsl, "X")


def test_price_fields_are_named_in_words():
    dsl = {"indicators": {}, "actions": {},
           "entry_conditions": [{"field": "close", "operator": ">", "value": 100}],
           "exit_conditions": []}
    assert "the closing price rises above 100" in explain(dsl, "X")


def test_a_strategy_that_cannot_trade_says_so():
    """Silence would read as "nothing to report" rather than "this will never
    place an order"."""
    result = explain({"indicators": {}, "entry_conditions": [],
                      "exit_conditions": [], "actions": {}})
    assert "never trade" in result


def test_an_unknown_indicator_is_described_literally_not_guessed():
    """A description that quietly drops a condition is worse than an awkward
    one that includes it."""
    dsl = {"indicators": {"x": {"type": "WEIRD"}},
           "entry_conditions": [{"field": "x", "operator": "??", "value": 1}],
           "exit_conditions": [], "actions": {}}
    sentence = explain(dsl, "TCS")

    assert "WEIRD" in sentence
    assert "??" in sentence


@pytest.mark.parametrize("junk", ["nonsense", None, 42, []])
def test_unreadable_definitions_do_not_raise(junk):
    assert "not readable" in explain(junk)


def test_missing_symbol_falls_back_to_a_neutral_phrase():
    assert "the instrument" in explain(RSI_DIP)


# -- groups read as what they are ------------------------------------------

def test_an_or_group_is_described_with_or():
    """A group read as "and" describes a different strategy from the one that
    runs — the exact failure this module exists to prevent."""
    dsl = {
        "indicators": {"rsi_14": {"type": "RSI", "length": 14}},
        "entry_conditions": [{"any": [
            {"field": "rsi_14", "operator": "<", "value": 30},
            {"field": "close", "operator": ">", "value": 200},
        ]}],
        "exit_conditions": [{"field": "rsi_14", "operator": ">", "value": 70}],
        "actions": {"buy_qty": 1},
    }
    sentence = explain(dsl, symbol="ACME")

    assert " or " in sentence
    assert "(14-period RSI falls below 30 or the closing price rises above 200)" in sentence


def test_a_group_is_bracketed_so_it_survives_being_read_aloud():
    """"A and B or C" is ambiguous out loud; the brackets carry the grouping."""
    dsl = {
        "indicators": {"rsi_14": {"type": "RSI", "length": 14}},
        "entry_conditions": [
            {"field": "rsi_14", "operator": "<", "value": 40},
            {"any": [
                {"field": "close", "operator": ">", "value": 200},
                {"field": "volume", "operator": ">", "value": 1000},
            ]},
        ],
        "exit_conditions": [{"field": "rsi_14", "operator": ">", "value": 70}],
        "actions": {"buy_qty": 1},
    }
    sentence = explain(dsl)

    assert "and (" in sentence
    assert sentence.count("(") == sentence.count(")")


def test_a_single_member_group_is_not_bracketed_for_nothing():
    dsl = {
        "indicators": {"rsi_14": {"type": "RSI", "length": 14}},
        "entry_conditions": [{"any": [{"field": "rsi_14", "operator": "<", "value": 30}]}],
        "exit_conditions": [{"field": "rsi_14", "operator": ">", "value": 70}],
        "actions": {"buy_qty": 1},
    }
    assert "(" not in explain(dsl)
