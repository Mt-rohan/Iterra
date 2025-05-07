# app.py

import os
import zipfile
import shutil
import json
import traceback
import stripe

from uuid import uuid4
from flask import (
    Flask, request, jsonify, send_file,
    abort, redirect, render_template
)
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import openai  # pinned to v0.28

# ─── Config & Initialization ────────────────────────────────────────────────
load_dotenv()
OPENAI_KEY       = os.getenv("OPENAI_API_KEY")
STRIPE_SECRET    = os.getenv("STRIPE_SECRET_KEY")
WEBHOOK_SECRET   = os.getenv("STRIPE_WEBHOOK_SECRET")
PRICE_ID         = os.getenv("STRIPE_PRICE_ID")

if not all((OPENAI_KEY, STRIPE_SECRET, WEBHOOK_SECRET, PRICE_ID)):
    raise RuntimeError(
        "Please set OPENAI_API_KEY, STRIPE_SECRET_KEY, "
        "STRIPE_WEBHOOK_SECRET & STRIPE_PRICE_ID in your .env"
    )

openai.api_key = OPENAI_KEY
stripe.api_key = STRIPE_SECRET

app = Flask(__name__, template_folder="templates")
UPLOAD_DIR  = "uploads"
RESULTS_DIR = "processed"
DATA_FILE   = "usage.json"
FREE_LIMIT  = 3  # free uploads per user

# ensure directories exist
os.makedirs(UPLOAD_DIR,  exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)

# ─── Data persistence helpers ────────────────────────────────────────────────
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user_record(user_id: str) -> dict:
    data = load_data()
    return data.setdefault(user_id, {
        "used": 0,
        "subscribed": False,
        # we'll stash stripe_customer_id here once created
    })

def increment_usage(user_id: str, amount: int):
    rec = get_user_record(user_id)
    rec["used"] += amount
    save_data(load_data())

def mark_subscribed(user_id: str):
    rec = get_user_record(user_id)
    rec["subscribed"] = True
    save_data(load_data())

# ─── Stripe Checkout helpers ────────────────────────────────────────────────
def _create_stripe_session(user_id: str) -> stripe.checkout.Session:
    """
    Create or find a Stripe Customer, then build
    a Checkout Subscription Session for PRICE_ID.
    """
    data = load_data()
    rec = data.setdefault(user_id, {})

    if "stripe_customer_id" not in rec:
        cust = stripe.Customer.create(email=user_id)
        rec["stripe_customer_id"] = cust.id
        save_data(data)

    return stripe.checkout.Session.create(
        customer=rec["stripe_customer_id"],
        mode="subscription",
        line_items=[{"price": PRICE_ID, "quantity": 1}],
        success_url="http://127.0.0.1:5050/success",
        cancel_url="http://127.0.0.1:5050/cancel"
    )

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    body = request.get_json() or {}
    user_id = body.get("user_id")
    if not user_id:
        return jsonify(error="user_id required"), 400

    session = _create_stripe_session(user_id)
    return jsonify(checkout_url=session.url)


@app.route("/checkout")
def checkout_redirect():
    """
    Browser hits /checkout?user_id=foo@example.com
    We redirect to the Stripe-hosted checkout page.
    """
    user_id = request.args.get("user_id")
    if not user_id:
        abort(400, "user_id query parameter required")
    session = _create_stripe_session(user_id)
    return redirect(session.url, code=303)


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload    = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, WEBHOOK_SECRET
        )
    except Exception:
        abort(400)

    # when checkout completes, mark their record subscribed
    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        cust = sess["customer"]
        data = load_data()
        for uid, rec in data.items():
            if rec.get("stripe_customer_id") == cust:
                rec["subscribed"] = True
                break
        save_data(data)

    return "", 200


@app.route("/upload", methods=["POST"])
def upload():
    user_id = request.headers.get("User-ID")
    if not user_id:
        return jsonify(error="Missing User-ID header"), 400

    rec = get_user_record(user_id)
    # Enforce free limit
    if rec["used"] >= FREE_LIMIT and not rec["subscribed"]:
        session = _create_stripe_session(user_id)
        return jsonify(
            error="Free limit reached. Subscribe to continue.",
            checkout_url=session.url
        ), 402

    # validate file
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".zip"):
        return jsonify(error="Upload a valid .zip"), 400

    # set up job dirs
    job  = uuid4().hex
    src  = os.path.join(UPLOAD_DIR, job)
    outd = os.path.join(RESULTS_DIR, job)
    os.makedirs(src,  exist_ok=True)
    os.makedirs(outd, exist_ok=True)
    path = os.path.join(src, secure_filename(f.filename))
    f.save(path)

    try:
        # unzip
        with zipfile.ZipFile(path, "r") as z:
            z.extractall(src)

        # find and refactor .js/.jsx
        count = 0
        for root, _, files in os.walk(src):
            for fn in files:
                if fn.endswith((".js", ".jsx")):
                    full = os.path.join(root, fn)
                    code = open(full, encoding="utf-8").read()
                    new  = _refactor(code)
                    if not new:
                        continue
                    rel  = os.path.relpath(full, src)
                    dest = os.path.join(outd, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    open(dest, "w", encoding="utf-8").write(new)
                    count += 1

        if count == 0:
            return jsonify(error="No refactor output"), 500

        # record usage
        increment_usage(user_id, count)

        # zip up results
        archive = os.path.join(RESULTS_DIR, job + "_res")
        shutil.make_archive(archive, "zip", outd)
        return send_file(archive + ".zip", as_attachment=True)

    finally:
        shutil.rmtree(src,  ignore_errors=True)
        shutil.rmtree(outd, ignore_errors=True)


@app.route("/success")
def success():
    return "<h1>🎉 Subscription active! You can now upload more.</h1>"


@app.route("/cancel")
def cancel():
    return "<h1>Subscription canceled. You remain on free plan.</h1>"


# ─── Refactor helper ──────────────────────────────────────────────────────────
def _refactor(code: str) -> str | None:
    prompt = (
        "Refactor this React code to use modern best practices:\n"
        "- Convert class components to functional components with hooks\n"
        "- Simplify props/state\n"
        "- Split large components where sensible\n\n"
        + code
    )
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an expert React developer."},
                {"role": "user",   "content": prompt}
            ]
        )
        return resp.choices[0].message.content.strip()  # type: ignore
    except Exception:
        traceback.print_exc()
        return None


# ─── Run the App ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)
