import base64
import hashlib
import secrets
import urllib.parse
import webbrowser

import requests


REDIRECT_URI = "https://tichu-guru.github.io/Rob-etsy-store/src/docs/oauth/callback/"
AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

SCOPES = [
    "listings_r",
    "shops_r",
]


def make_pkce():
    verifier = base64.urlsafe_b64encode(
        secrets.token_bytes(32)
    ).decode("ascii").rstrip("=")

    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")

    return verifier, challenge


def main():
    print()
    print("Etsy OAuth setup")
    print("----------------")
    print()

    client_id = input("Paste your Etsy Keystring: ").strip()

    if not client_id:
        raise SystemExit("No Keystring supplied.")

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(32)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    print()
    print("Open this URL in your browser:")
    print()
    print(url)
    print()

    webbrowser.open(url)

    print("After approving Etsy, copy the authorization code")
    print("shown on your callback page and paste it below.")
    print()

    code = input("Authorization code: ").strip()

    if not code:
        raise SystemExit("No authorization code supplied.")

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": verifier,
        },
        timeout=30,
    )

    print()
    print("Etsy response:", response.status_code)

    if not response.ok:
        print(response.text)
        raise SystemExit("Etsy token exchange failed.")

    token_data = response.json()

    print()
    print("SUCCESS: Etsy OAuth token exchange completed.")
    print()
    print("Access token received:", bool(token_data.get("access_token")))
    print("Refresh token received:", bool(token_data.get("refresh_token")))
    print()
    print("DO NOT paste either token into ChatGPT.")
    print()

    with open(".etsy_tokens.json", "w", encoding="utf-8") as f:
        import json
        json.dump(token_data, f, indent=2)

    print("Tokens saved locally to .etsy_tokens.json")
    print("Do NOT commit that file to GitHub.")


if __name__ == "__main__":
    main()