import hashlib
import os
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFICATION_TOKEN = os.environ["EBAY_VERIFICATION_TOKEN"]
ENDPOINT_URL = os.environ["EBAY_ENDPOINT_URL"]


@app.route("/ebay/account-deletion", methods=["GET", "POST"])
def account_deletion():
    # eBay sends a GET with challenge_code to verify the endpoint
    challenge_code = request.args.get("challenge_code")
    if challenge_code:
        raw = challenge_code + VERIFICATION_TOKEN + ENDPOINT_URL
        response_hash = hashlib.sha256(raw.encode()).hexdigest()
        return jsonify({"challengeResponse": response_hash})

    # POST = actual deletion notification; just acknowledge it
    return "", 200


@app.route("/")
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
