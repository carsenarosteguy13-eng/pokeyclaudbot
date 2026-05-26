import base64
import hashlib
import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFICATION_TOKEN = os.environ["EBAY_VERIFICATION_TOKEN"]
ENDPOINT_URL = os.environ["EBAY_ENDPOINT_URL"]
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
EBAY_RUNAME = os.environ.get("EBAY_RUNAME", "")


@app.route("/ebay/account-deletion", methods=["GET", "POST"])
def account_deletion():
    challenge_code = request.args.get("challenge_code")
    if challenge_code:
        raw = challenge_code + VERIFICATION_TOKEN + ENDPOINT_URL
        response_hash = hashlib.sha256(raw.encode()).hexdigest()
        return jsonify({"challengeResponse": response_hash})
    return "", 200


@app.route("/ebay/callback")
def oauth_callback():
    code = request.args.get("code", "")
    error = request.args.get("error", "")

    if error:
        return f"<h2>Error</h2><p>{error}: {request.args.get('error_description', '')}</p>", 400

    if not code:
        return "<h2>No code received.</h2>", 400

    # Exchange code for tokens immediately before it expires
    creds = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": EBAY_RUNAME,
        },
        timeout=15,
    )
    data = resp.json()

    if "refresh_token" in data:
        refresh_token = data["refresh_token"]
        expires_days = data.get("refresh_token_expires_in", 0) // 86400
        return f"""
        <h2>Success!</h2>
        <p>Copy your <strong>EBAY_REFRESH_TOKEN</strong> below and paste it into your .env file:</p>
        <textarea rows="6" cols="80" onclick="this.select()" style="font-family:monospace">{refresh_token}</textarea>
        <p>This token expires in ~{expires_days} days.</p>
        """, 200

    return f"<h2>Token exchange failed</h2><pre>{data}</pre>", 400


@app.route("/")
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
