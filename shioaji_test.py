"""First-contact test script for 永豐金 Shioaji API - just logs in and dumps
raw responses so we can see what fields actually come back before deciding
how (or whether) to wire this into the twstock.db pipeline.

Setup:
    pip install shioaji
    copy .env.example to .env and fill in SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY
    (never commit .env - it's already in .gitignore)

Usage:
    python shioaji_test.py                  # simulation mode (safe default)
    python shioaji_test.py --live           # real account/data
    python shioaji_test.py --live --codes 2330 2317
"""
import argparse
import sys

import utils

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codes", nargs="*", default=["2330"], help="stock codes to test with")
    parser.add_argument("--live", action="store_true", help="use real account instead of simulation mode")
    args = parser.parse_args()

    utils.load_env()

    import os
    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("Missing credentials. Copy .env.example to .env and fill in:")
        print("  SHIOAJI_API_KEY=...")
        print("  SHIOAJI_SECRET_KEY=...")
        sys.exit(1)

    import shioaji as sj

    api = sj.Shioaji(simulation=not args.live)
    print(f"Logging in ({'LIVE' if args.live else 'simulation'} mode)...")
    accounts = api.login(api_key=api_key, secret_key=secret_key)
    print(f"\nLogged in. Accounts:\n{accounts}\n")

    print(f"Usage/quota: {api.usage()}\n")

    for code in args.codes:
        contract = api.Contracts.Stocks.get(code)
        if contract is None:
            print(f"[not found] no contract for code {code}")
            continue
        print(f"=== {code} contract ===")
        print(contract)

        snapshot = api.snapshots([contract])
        print(f"\n=== {code} snapshot ===")
        print(snapshot)
        print()

    api.logout()
    print("Logged out.")


if __name__ == "__main__":
    main()
