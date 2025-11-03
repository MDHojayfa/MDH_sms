#!/usr/bin/env python3
"""
Upgraded SMS form sender:
- uses requests.Session to GET the form page, parse hidden inputs, then POST
- logs requests & responses to file
- phone number normalization (phonenumbers)
- simple in-memory rate limiter per IP
- optional proxy via env var SMS_HTTP_PROXY
"""

from flask import Flask, request, render_template_string, abort, jsonify
import requests
from bs4 import BeautifulSoup
import logging
from logging.handlers import RotatingFileHandler
import time
import os
import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException
from functools import wraps

# Configuration (can override with env vars)
SMS_FORM_URL = os.getenv("SMS_FORM_URL", "https://sms.link3.net/send_single_sms")
REQUEST_TIMEOUT = float(os.getenv("SMS_TIMEOUT", "15"))
RETRY_COUNT = int(os.getenv("SMS_RETRIES", "2"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "10"))  # seconds
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "3"))  # requests per window per IP
HTTP_PROXY = os.getenv("SMS_HTTP_PROXY", "")  # e.g. "http://user:pass@host:port"

# Setup logging
LOG_FILE = os.getenv("SMS_LOG_FILE", "sms_sender.log")
logger = logging.getLogger("sms_sender")
logger.setLevel(logging.DEBUG)
handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

app = Flask(__name__)

DEFAULT_HEADERS = {
    "User-Agent": os.getenv("SMS_USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64)"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": SMS_FORM_URL,
}

# Simple in-memory rate limiter
_rate_store = {}  # ip -> [timestamps...]

def rate_limited(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        ip = request.remote_addr or "unknown"
        now = time.time()
        arr = _rate_store.get(ip, [])
        # purge old
        arr = [t for t in arr if now - t < RATE_LIMIT_WINDOW]
        if len(arr) >= RATE_LIMIT_MAX:
            logger.warning("Rate limit exceeded for %s", ip)
            abort(429, description="Too many requests, slow down.")
        arr.append(now)
        _rate_store[ip] = arr
        return func(*args, **kwargs)
    return wrapper

def normalize_phone(number_str, default_region="BD"):
    """Return normalized E.164 phone or raise ValueError."""
    try:
        parsed = phonenumbers.parse(number_str, default_region)
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError("Invalid phone number")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except NumberParseException as e:
        raise ValueError(f"Number parse error: {e}")

def get_session(proxies=None):
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    if proxies:
        s.proxies.update(proxies)
    return s

def prepare_form_from_get(session):
    """GET the form URL and parse form inputs to return a dict of defaults."""
    resp = session.get(SMS_FORM_URL, timeout=REQUEST_TIMEOUT)
    logger.debug("GET %s -> %s", SMS_FORM_URL, resp.status_code)
    if resp.status_code != 200:
        logger.warning("Non-200 GET: %s", resp.status_code)
    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form")
    form_data = {}
    if not form:
        # No form found; still return empty dict (some endpoints accept direct POST)
        logger.debug("No <form> found on GET page.")
        return form_data
    # gather inputs
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        value = inp.get("value", "")
        form_data[name] = value
    # also check for textareas with names
    for ta in form.find_all("textarea"):
        name = ta.get("name")
        if name and name not in form_data:
            form_data[name] = ta.text or ""
    logger.debug("Parsed form fields: %s", list(form_data.keys()))
    return form_data

def post_form(session, form_data):
    """POST with retries and return response object."""
    proxies = None
    if HTTP_PROXY:
        proxies = {"http": HTTP_PROXY, "https": HTTP_PROXY}
        logger.debug("Using proxy: %s", HTTP_PROXY)
    last_exc = None
    for attempt in range(1, RETRY_COUNT + 2):
        try:
            resp = session.post(SMS_FORM_URL, data=form_data, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            logger.info("POST attempt %d -> %s", attempt, resp.status_code)
            logger.debug("Request headers: %s", resp.request.headers)
            logger.debug("Request body (first 1000 chars): %s", str(resp.request.body)[:1000])
            logger.debug("Response length: %d", len(resp.text or ""))
            return resp
        except requests.RequestException as e:
            logger.warning("POST attempt %d failed: %s", attempt, e)
            last_exc = e
            time.sleep(1)
    raise last_exc

@app.route("/", methods=["GET"])
def index_get():
    # simple landing -> show form
    html = """
    <!doctype html>
    <html>
      <head><meta charset="utf-8"><title>Send SMS</title></head>
      <body style="background:#0d0d0d;color:#fff;font-family:Arial;">
        <div style="max-width:700px;margin:30px auto;padding:20px;background:#111;border:1px solid #00ffff;border-radius:8px;">
          <h2>Send SMS (Upgraded)</h2>
          <form method="post" action="/" style="display:flex;flex-direction:column;">
            <label>Mobile (e.g. +88017XXXXXXXX or 017XXXXXXXX):</label>
            <input name="mobile" required style="padding:8px;margin-bottom:8px;">
            <label>Message:</label>
            <textarea name="sms_text" rows="5" required style="padding:8px;margin-bottom:8px;"></textarea>
            <label>Optional: override lan (default 1):</label>
            <input name="lan" style="padding:8px;margin-bottom:8px;">
            <button type="submit" style="background:#00ffff;color:#000;padding:10px;border:none;border-radius:6px;cursor:pointer">Send</button>
          </form>
          <p style="color:#bbb;margin-top:12px;font-size:13px;">Logs: <code>{logfile}</code></p>
        </div>
      </body>
    </html>
    """.format(logfile=LOG_FILE)
    return html

@app.route("/", methods=["POST"])
@rate_limited
def index_post():
    mobile_in = request.form.get("mobile", "").strip()
    sms_text = request.form.get("sms_text", "").strip()
    lan = request.form.get("lan", "").strip() or "1"

    try:
        normalized = normalize_phone(mobile_in)
    except ValueError as e:
        logger.info("Invalid phone provided: %s", mobile_in)
        return f"Invalid phone number: {e}", 400

    # build session & parse form
    proxies = None
    if HTTP_PROXY:
        proxies = {"http": HTTP_PROXY, "https": HTTP_PROXY}
    session = get_session(proxies=proxies)
    try:
        form_defaults = prepare_form_from_get(session)
    except Exception as e:
        logger.exception("Error during GET/form parse")
        return f"Error fetching form page: {e}", 500

    # fill/override fields
    form_defaults.update({
        "lan": lan,
        # the SMS gateway might expect mobile in various formats; try normalized and raw variants
        "mobile": normalized,
        "sms_text": sms_text,
        # preserve existing keys if present
        "contact": form_defaults.get("contact", ""),
        "sms_txt": form_defaults.get("sms_txt", "")
    })

    logger.info("Attempting to send SMS to %s (norm: %s) lan=%s", mobile_in, normalized, lan)
    logger.debug("Form data keys: %s", list(form_defaults.keys()))

    try:
        resp = post_form(session, form_defaults)
    except Exception as e:
        logger.exception("POST failed")
        return f"Failed to send request: {e}", 500

    # Save a useful debug snapshot to log
    logger.info("POST response status %s, url: %s", resp.status_code, resp.url)
    snippet = resp.text[:4000]
    logger.debug("Response snippet (4000 chars):\n%s", snippet)

    # Simple heuristics to detect success (provider-dependent, tailor as needed)
    success_markers = ["success", "sent", "message sent", "queued"]
    lower = (resp.text or "").lower()
    success_detected = any(m in lower for m in success_markers)

    result = {
        "http_status": resp.status_code,
        "url": resp.url,
        "success_detected": success_detected,
        "response_snippet": snippet
    }

    # return nice HTML with response snippet
    html = f"""
    <!doctype html><html><head><meta charset="utf-8"><title>Result</title></head>
    <body style="background:#000;color:#fff;font-family:Arial;padding:20px;">
      <h3>Result</h3>
      <p>HTTP Status: {resp.status_code}</p>
      <p>Success heuristic: {result['success_detected']}</p>
      <h4>Response (first 4000 chars)</h4>
      <pre style="white-space:pre-wrap;background:#111;padding:12px;border-radius:6px;border:1px solid #333;color:#ddd">{snippet}</pre>
      <p>Check log file: <code>{LOG_FILE}</code></p>
      <p><a href="/">Send another</a></p>
    </body></html>
    """
    return html

if __name__ == "__main__":
    # host 0.0.0.0 if you want to access externally in VM
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=bool(os.getenv("DEBUG", "1")))
