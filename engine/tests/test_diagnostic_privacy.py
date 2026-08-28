"""The diagnostic must be safe to hand to someone else.

Its whole purpose is to be shared — to get a field mapping corrected or an
entitlement question answered. It says so in its own hint. So the bar is not
"hides balances", it is "discloses nothing about this account".

It failed that bar twice. The `normalized` block returned real balances and
holdings whatever `include_values` said. And `describe_shape` reported dict
keys verbatim — but Choice keys its holdings payload by ISIN, so the "shape"
and the `flat_fields` list were the account's holdings under another name.
Both were live in a report that had already been shared.
"""

from engine.app.choice_gateway.normalize import (
    describe_shape,
    is_keyed_collection,
    unwrap_dict,
)

# The real shape Choice returns, taken from a live diagnostic. Two of the 33
# ISINs are enough to establish the repeated schema.
HOLDINGS = {
    "Status": "Success",
    "Response": {
        "lDictStockViewHoldingData": {
            "INE009A01021": {"SegmentId": 1, "Token": 1594, "Symbol": "INFY-EQ",
                             "LTP": 111850, "AvgBuyPrice": 1334.77, "Qty": 326},
            "INE002A01018": {"SegmentId": 1, "Token": 2885, "Symbol": "RELIANCE-EQ",
                             "LTP": 132410, "AvgBuyPrice": 1288.66, "Qty": 413},
        }
    },
    "Reason": "",
}

# A funds record: keys here ARE field names and are the point of the report.
FUNDS = {
    "Status": "Success",
    "Response": {"FundsViewNew": {"LedgerBalance": 2955.20, "FundsAvailable": 2955.20,
                                  "MarginUtilized": 0.0, "RealizedPnL": 0.0}},
    "Reason": "",
}


def _flat(payload) -> str:
    """Everything the described shape would print, as one string."""
    return repr(describe_shape(payload))


def test_an_isin_keyed_holdings_map_is_recognised_as_a_collection():
    assert is_keyed_collection(HOLDINGS["Response"]["lDictStockViewHoldingData"])


def test_a_funds_record_is_not_mistaken_for_a_collection():
    """Collapsing this would throw away the field names the report exists to
    show. The distinction is that a record's values are scalars."""
    assert not is_keyed_collection(HOLDINGS)
    assert not is_keyed_collection(FUNDS["Response"]["FundsViewNew"])


def test_the_described_shape_does_not_disclose_a_single_isin():
    described = _flat(HOLDINGS)

    assert "INE009A01021" not in described
    assert "INE002A01018" not in described
    assert "2 entries" in described


def test_the_described_shape_still_names_the_record_fields():
    """Redaction must not cost the report its usefulness: the field names are
    what a mapping fix needs."""
    described = _flat(HOLDINGS)

    for field in ("SegmentId", "Token", "Symbol", "LTP", "AvgBuyPrice"):
        assert field in described, field


def test_a_funds_record_keeps_its_field_names():
    described = _flat(FUNDS)

    for field in ("LedgerBalance", "FundsAvailable", "MarginUtilized"):
        assert field in described, field


def test_no_value_survives_into_the_described_shape():
    """Types, never values — for both payload shapes."""
    for payload, values in ((HOLDINGS, ["1334.77", "326", "132410", "INFY-EQ"]),
                            (FUNDS, ["2955.2"])):
        described = _flat(payload)
        for value in values:
            assert value not in described, f"{value} leaked"


def test_the_flat_field_list_would_have_leaked_the_same_isins():
    """`unwrap_dict` merges the holdings map, so its keys are ISINs. The probe
    published `sorted(merged.keys())` directly."""
    merged = unwrap_dict(HOLDINGS)

    # This is what the probe used to publish, and why it must now be gated.
    assert "INE009A01021" in merged
    assert is_keyed_collection(merged)
