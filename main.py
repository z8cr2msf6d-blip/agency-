"""
Agency Connect — Drayton's private MCP connector for Mission Control.

Exposes real actions to Claude (chat, the Mission Control app, scheduled tasks):
  - send_sms(to, body)        Twilio, from your registered number
  - text_drayton(body)        Twilio, to OWNER_PHONE
  - stripe_snapshot(days)     Stripe money in (optional, needs STRIPE_SECRET_KEY)
  - server_status()           what is configured

Auth: every MCP request must carry  Authorization: Bearer <MCP_TOKEN>.
Claude.ai custom connector -> "No sign in" + request header Authorization.

Deploy: uvicorn main:app --host 0.0.0.0 --port $PORT   (see Procfile)
Env vars: MCP_TOKEN, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM,
          OWNER_PHONE, STRIPE_SECRET_KEY (optional)
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

MCP_TOKEN = os.environ.get("MCP_TOKEN", "").strip()
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM = os.environ.get("TWILIO_FROM", "").strip()
OWNER_PHONE = os.environ.get("OWNER_PHONE", "").strip()
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
SMS_PER_HOUR = int(os.environ.get("SMS_PER_HOUR", "40"))

_sent_times: deque = deque()


def e164(number: str) -> str:
    """Normalize a US number to E.164 (+1XXXXXXXXXX). Raises ValueError if it can't."""
    digits = re.sub(r"\D", "", str(number or ""))
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if str(number).strip().startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    raise ValueError(f"'{number}' is not a valid phone number. Use 918-555-0123 or +19185550123.")


def http_form_post(url: str, data: dict, user: str, password: str) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"message": str(e)}
        raise RuntimeError(f"Twilio error {e.code}: {err.get('message', err)}")


def http_get_bearer(url: str, key: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"error": {"message": str(e)}}
        raise RuntimeError(f"Stripe error {e.code}: {err.get('error', {}).get('message', err)}")


def _twilio_send(to: str, body: str) -> dict:
    if not (TWILIO_SID and TWILIO_AUTH and TWILIO_FROM):
        raise RuntimeError("Twilio is not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM on the server.")
    now = time.time()
    while _sent_times and now - _sent_times[0] > 3600:
        _sent_times.popleft()
    if len(_sent_times) >= SMS_PER_HOUR:
        raise RuntimeError(f"Rate guard: {SMS_PER_HOUR} texts in the last hour. Wait before sending more.")
    to_num = e164(to)
    text = str(body or "").strip()
    if not text:
        raise RuntimeError("Message body is empty.")
    if len(text) > 1500:
        raise RuntimeError("Message is too long; keep texts under 1500 characters (ideally under 160).")
    result = http_form_post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
        {"To": to_num, "From": TWILIO_FROM, "Body": text},
        TWILIO_SID,
        TWILIO_AUTH,
    )
    _sent_times.append(now)
    return {"ok": True, "to": to_num, "sid": result.get("sid"), "status": result.get("status"), "segments": result.get("num_segments")}


mcp = MCPServer(
    "Agency Connect",
    instructions=(
        "Private connector for Drayton's AI automation agency (Catoosa/Tulsa, OK). "
        "send_sms texts a prospect or client from Drayton's registered Twilio number: business-to-business only, "
        "identify Drayton, one clear ask, keep under 160 characters, never text anyone who replied STOP. "
        "text_drayton sends Drayton himself a short update (reports, alerts). "
        "stripe_snapshot reads real money in from Stripe when it is configured."
    ),
)


@mcp.tool()
def send_sms(to: str, body: str) -> dict:
    """Send an SMS from Drayton's Twilio number to a prospect or client. `to` is a US phone number
    (918-555-0123 or +19185550123); `body` is the plain-text message (keep under 160 characters, identify
    Drayton, one ask). Returns {ok, to, sid, status}. Fails if Twilio is not configured or the hourly guard is hit."""
    try:
        return _twilio_send(to, body)
    except (RuntimeError, ValueError) as e:
        raise ToolError(str(e))


@mcp.tool()
def text_drayton(body: str) -> dict:
    """Text Drayton himself (the owner) a short update: a report summary, a hot reply, an alert.
    `body` is plain text under 480 characters. Returns {ok, to, sid, status}."""
    if not OWNER_PHONE:
        raise ToolError("OWNER_PHONE is not set on the server.")
    try:
        return _twilio_send(OWNER_PHONE, str(body or "")[:480])
    except (RuntimeError, ValueError) as e:
        raise ToolError(str(e))


@mcp.tool()
def stripe_snapshot(days: int = 30) -> dict:
    """Money in from Stripe for the last `days` days (default 30): successful charge count and total in USD,
    plus available and pending balance. Only works when STRIPE_SECRET_KEY is set; otherwise says so."""
    if not STRIPE_KEY:
        return {"configured": False, "note": "Stripe is not connected. Set STRIPE_SECRET_KEY on the server (a restricted read-only key is enough)."}
    days = max(1, min(int(days or 30), 365))
    since = int(time.time()) - days * 86400
    try:
        return _stripe(days, since)
    except RuntimeError as e:
        raise ToolError(str(e))


def _stripe(days: int, since: int) -> dict:
    charges = http_get_bearer(f"https://api.stripe.com/v1/charges?limit=100&created[gte]={since}", STRIPE_KEY)
    ok = [c for c in charges.get("data", []) if c.get("status") == "succeeded" and not c.get("refunded")]
    total = sum(int(c.get("amount", 0)) for c in ok) / 100.0
    bal = http_get_bearer("https://api.stripe.com/v1/balance", STRIPE_KEY)
    avail = sum(int(b.get("amount", 0)) for b in bal.get("available", [])) / 100.0
    pend = sum(int(b.get("amount", 0)) for b in bal.get("pending", [])) / 100.0
    recent = [
        {"date": time.strftime("%Y-%m-%d", time.gmtime(int(c.get("created", 0)))), "amount": int(c.get("amount", 0)) / 100.0,
         "description": (c.get("description") or c.get("billing_details", {}).get("name") or "")[:60]}
        for c in ok[:15]
    ]
    return {"configured": True, "days": days, "successfulCharges": len(ok), "totalInUSD": round(total, 2),
            "balanceAvailableUSD": round(avail, 2), "balancePendingUSD": round(pend, 2), "recent": recent,
            "truncated": charges.get("has_more", False)}


@mcp.tool()
def server_status() -> dict:
    """What this connector can do right now: which integrations are configured and the SMS guard."""
    return {
        "twilio": bool(TWILIO_SID and TWILIO_AUTH and TWILIO_FROM),
        "twilioFrom": TWILIO_FROM[-4:].rjust(len(TWILIO_FROM), "•") if TWILIO_FROM else "",
        "ownerPhoneSet": bool(OWNER_PHONE),
        "stripe": bool(STRIPE_KEY),
        "smsSentLastHour": len(_sent_times),
        "smsPerHourGuard": SMS_PER_HOUR,
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "Agency Connect", "twilio": bool(TWILIO_SID and TWILIO_FROM), "stripe": bool(STRIPE_KEY), "authRequired": bool(MCP_TOKEN)})


_inner = mcp.streamable_http_app(
    json_response=True,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


OPEN_METHODS = {"initialize", "notifications/initialized", "ping", "tools/list", "prompts/list", "resources/list", "resources/templates/list"}


def _method_names(body: bytes) -> list:
    try:
        data = json.loads(body.decode() or "null")
    except Exception:
        return ["?"]
    items = data if isinstance(data, list) else [data]
    return [str((i or {}).get("method", "?")) if isinstance(i, dict) else "?" for i in items]


async def app(scope, receive, send):
    """ASGI wrapper. /health is open. The MCP handshake and tool listing are open so Claude can add the
    connector; every action (tools/call and anything else) needs  Authorization: Bearer <MCP_TOKEN>."""
    if scope.get("type") != "http" or scope.get("path", "") == "/health":
        return await _inner(scope, receive, send)
    if not MCP_TOKEN:
        resp = JSONResponse({"error": "MCP_TOKEN is not set on the server; refusing all requests."}, status_code=503)
        return await resp(scope, receive, send)
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    authorized = headers.get("authorization", "").strip() == f"Bearer {MCP_TOKEN}"
    if authorized or scope.get("method", "GET") != "POST":
        return await _inner(scope, receive, send)
    # Unauthenticated POST: buffer the body, allow only the open JSON-RPC methods, then replay the body.
    chunks = []
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    body = b"".join(chunks)
    if all(m in OPEN_METHODS for m in _method_names(body)):
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await _inner(scope, replay, send)
    resp = JSONResponse({"error": "unauthorized: this action needs the Authorization: Bearer <MCP_TOKEN> header"}, status_code=401)
    await resp(scope, receive, send)
