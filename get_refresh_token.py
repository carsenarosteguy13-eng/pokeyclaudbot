#!/usr/bin/env python3
"""
Run this once to get your eBay OAuth refresh token.
You only need to do this one time — the token lasts ~18 months.
"""

import base64
import sys
import urllib.parse
import webbrowser
import requests


def main():
    print("eBay OAuth Refresh Token Helper")
    print("=" * 45)
    print()
    print("STEP 1 — Register a redirect URI in eBay:")
    print("  1. Go back to the 'User Tokens' page on developer.ebay.com")
    print("  2. Scroll down to 'Get a Token from eBay via Your Application'")
    print("  3. Click 'Add eBay Redirect URL'")
    print("  4. Enter  https://localhost  as the URL, any name is fine")
    print("  5. eBay will assign a RuName like 'YourName-AppName-PRD-xxxx'")
    print("  6. Come back here and continue")
    print()

    client_id     = input("EBAY_CLIENT_ID:     ").strip()
    client_secret = input("EBAY_CLIENT_SECRET: ").strip()
    runame        = input("RuName:             ").strip()

    scopes = " ".join([
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
        "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    ])

    auth_url = (
        "https://auth.ebay.com/oauth2/authorize?"
        + urllib.parse.urlencode({
            "client_id":     client_id,
            "response_type": "code",
            "redirect_uri":  runame,
            "scope":         scopes,
        })
    )

    print("\nOpening eBay login in your browser...")
    webbrowser.open(auth_url)
    print()
    print("After you log in, your browser will try to go to https://localhost")
    print("It will show a 'can't connect' error — that's expected.")
    print("Copy the FULL URL from the address bar (it contains '?code=...')")
    print()
    redirect_url = input("Paste the full URL here: ").strip()

    parsed = urllib.parse.urlparse(redirect_url)
    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        print(f"\nCouldn't find a 'code' in that URL. Got:\n{redirect_url}")
        sys.exit(1)

    print("\nExchanging code for tokens...")
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization":  f"Basic {creds}",
            "Content-Type":   "application/x-www-form-urlencoded",
        },
        data={
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": runame,
        },
        timeout=15,
    )
    data = resp.json()

    if "refresh_token" in data:
        print()
        print("=" * 45)
        print("SUCCESS! Copy this into your .env as EBAY_REFRESH_TOKEN:")
        print()
        print(data["refresh_token"])
        print()
        expires_days = data.get("refresh_token_expires_in", 0) // 86400
        print(f"Expires in ~{expires_days} days")
    else:
        print(f"\nError: {data}")
        sys.exit(1)


if __name__ == "__main__":
    main()
