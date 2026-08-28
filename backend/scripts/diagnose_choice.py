"""Show what Choice actually returns for an account.

Run this when the platform connects but shows no funds, holdings or quotes. It
signs in with the `kkunal` SDK, calls each endpoint, and prints the *shape* of
every reply — field names and value types, never values — so the output is safe
to share when a mapping needs correcting.

    python backend/scripts/diagnose_choice.py --vendor M09984 --mobile 9428880191

The API key is prompted for rather than passed on the command line, so it does
not end up in shell history. Nothing here places an order.
"""

import argparse
import getpass
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from engine.app.choice_gateway.client_manager import ChoiceSession  # noqa: E402
from engine.app.choice_gateway.errors import ChoiceGatewayError  # noqa: E402
from engine.app.choice_gateway.normalize import (  # noqa: E402
    describe_shape,
    unwrap_dict,
    unwrap_list,
)
from engine.app.config import engine_settings  # noqa: E402


def probe(label: str, call, show_values: bool) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    try:
        raw = call()
    except Exception as exc:
        print(f"  FAILED: {str(exc)[:400]}")
        return

    records = unwrap_list(raw)
    merged = unwrap_dict(raw)

    if isinstance(raw, dict):
        print(f"  envelope keys : {sorted(raw.keys())}")
        status = raw.get("Status") or raw.get("status")
        if status:
            print(f"  status        : {status}")
    print(f"  records found : {len(records)}")
    if records:
        print(f"  record fields : {sorted(records[0].keys())}")
    elif merged:
        print(f"  flat fields   : {sorted(merged.keys())}")

    print("  shape:")
    print("    " + json.dumps(describe_shape(raw), indent=2).replace("\n", "\n    "))

    if show_values:
        print("  RAW VALUES:")
        print("    " + json.dumps(raw, indent=2, default=str)[:4000].replace("\n", "\n    "))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vendor", required=True, help="Choice Client ID, e.g. M09984")
    parser.add_argument("--mobile", required=True, help="Registered mobile number")
    parser.add_argument("--tokens", default="1_2885,1_1594",
                        help="segment_token pairs to quote")
    parser.add_argument("--show-values", action="store_true",
                        help="Include actual values. Your own account data — only "
                             "use when you intend to share the output knowingly.")
    args = parser.parse_args()

    api_key = getpass.getpass("Choice API key (input hidden): ").strip()
    if not api_key:
        print("No API key supplied.")
        return 2

    print(f"\nEnvironment : {engine_settings.CHOICE_ENV}")
    print(f"Base URL    : {engine_settings.choice_base_url}")
    print(f"Client ID   : {args.vendor}")

    session = ChoiceSession("diagnostics")
    try:
        # paper=True: this session can never place an order.
        session.login_totp(args.vendor, api_key, args.mobile, paper=True)
    except ChoiceGatewayError as exc:
        print(f"\nLOGIN FAILED: {exc.message}")
        print(f"Choice said : {exc.details}")
        return 1

    print("\nLogin OK.")
    print(f"  session id   : {'yes' if session.session_id else 'no'}")
    print(f"  access token : {'yes' if session.access_token else 'no'}")
    client = session.client
    print(f"  feed handler : {getattr(client, 'bcast_ip', None)}:"
          f"{getattr(client, 'bcast_port', None)}")

    probe("FundsViewNew", client.funds.get_funds_view_new, args.show_values)
    probe("FundsView", client.funds.get_funds_view, args.show_values)
    probe("Holdings", client.portfolio.get_holdings, args.show_values)
    probe("NetPosition", client.portfolio.get_net_position, args.show_values)
    probe("MarketStatus", client.market.get_market_status, args.show_values)
    probe("MultipleTouchline",
          lambda: client.market.get_multiple_touchline(args.tokens), args.show_values)
    probe("OrderBook", client.orders.get_order_book_v2, args.show_values)
    probe("UserProfile", client.market.get_user_profile, args.show_values)

    print(f"\n{'=' * 70}")
    print("What the platform makes of it:")
    from engine.app.choice_gateway import funds, market, portfolio

    for label, call in (
        ("funds", lambda: funds.get_funds(session)),
        ("holdings", lambda: portfolio.get_holdings(session)),
        ("positions", lambda: portfolio.get_positions(session)),
        ("quotes", lambda: market.get_multiple_touchline(session, args.tokens)),
    ):
        try:
            result = call()
            count = len(result.get("data", [])) if isinstance(result.get("data"), list) else "-"
            print(f"  {label:10} OK    ({count} rows)"
                  if count != "-" else f"  {label:10} OK    {result.get('data')}")
        except Exception as exc:
            print(f"  {label:10} ERROR {str(exc)[:200]}")

    print("\nShare the section above (without --show-values) to have any "
          "unmapped fields wired up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
