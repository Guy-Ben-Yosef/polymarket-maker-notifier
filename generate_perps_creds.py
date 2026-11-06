"""
Create Polymarket Perps proxy credentials.

Perps authenticates with a *proxy signer*: a fresh keypair whose address is
registered against your Polymarket account with one EIP-712 signature from
your account signer (the wallet behind PRIVATE_KEY). The API returns a
secret; the proxy address + secret are then used for REST and WebSocket.

Usage:
    pip install -r requirements-creds.txt
    python generate_perps_creds.py [--days 7] [--label maker-notifier]

Docs: https://docs.polymarket.com/perps/authenticated-sessions
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time

import httpx
from dotenv import load_dotenv

try:
    from eth_account import Account
except ImportError:  # pragma: no cover
    print("Missing dependency. Run: pip install -r requirements-creds.txt")
    sys.exit(1)

API = "https://api.perpetuals.polymarket.com"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Perps proxy credentials")
    parser.add_argument("--days", type=int, default=7, help="credential lifetime in days")
    parser.add_argument("--label", default="maker-notifier", help="label shown in the account")
    args = parser.parse_args()

    load_dotenv()
    pk = os.getenv("PRIVATE_KEY", "").strip()
    if not pk:
        print("Error: PRIVATE_KEY not found in .env")
        sys.exit(1)

    owner = Account.from_key(pk)
    print(f"Account signer: {owner.address}")

    # Is this address registered for Perps?
    try:
        r = httpx.get(f"{API}/v1/info/registered", params={"address": owner.address}, timeout=15)
        r.raise_for_status()
        if not r.json().get("registered", False):
            print(
                "This address is not registered for Perps yet. "
                "Open Polymarket Perps in the browser with this account once, then retry."
            )
            sys.exit(1)
    except httpx.HTTPError as e:
        print(f"Warning: could not check registration: {e}")

    proxy = Account.create()
    now_ms = int(time.time() * 1000)
    expiry = now_ms + args.days * 24 * 3600 * 1000
    salt = secrets.randbits(53)

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "CreateProxy": [
                {"name": "addr", "type": "address"},
                {"name": "exp", "type": "uint64"},
                {"name": "salt", "type": "uint64"},
                {"name": "ts", "type": "uint64"},
            ],
        },
        "primaryType": "CreateProxy",
        "domain": {"name": "Polymarket", "version": "1", "chainId": 137},
        "message": {"addr": proxy.address, "exp": expiry, "salt": salt, "ts": now_ms},
    }
    signed = Account.sign_typed_data(pk, full_message=typed)
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig

    body = {
        "op": {
            "type": "createProxy",
            "args": {"owner": owner.address, "proxy": proxy.address, "expiry": expiry},
        },
        "sig": sig,
        "salt": salt,
        "ts": now_ms,
        "label": args.label,
    }

    print("Registering proxy signer…")
    r = httpx.post(f"{API}/v1/account/proxy", json=body, timeout=20)
    if r.status_code >= 400:
        print(f"Failed ({r.status_code}): {r.text}")
        sys.exit(1)
    secret = r.json().get("secret")
    if not secret:
        print(f"Unexpected response: {r.text}")
        sys.exit(1)

    exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry / 1000))
    print("\nSuccess! Add these lines to your .env:")
    print("=" * 50)
    print(f"PERPS_PROXY_ADDRESS={proxy.address}")
    print(f"PERPS_PROXY_SECRET={secret}")
    print("=" * 50)
    print(f"Credentials expire: {exp_str}  (re-run this script before then)")
    print(
        "\nThe proxy private key is NOT needed by the notifier (read-only use). "
        "Keep it only if you plan to trade with this proxy:\n"
        f"PERPS_PROXY_PRIVATE_KEY={proxy.key.hex()}"
    )


if __name__ == "__main__":
    main()
