"""
Claude API — Entra Agent ID WIF Proof of Concept
=================================================
This agent demonstrates the sidecar pattern for Workload Identity Federation:

  Workload runtime (Azure MI / K8s SA / GitHub OIDC)
        │  OIDC assertion (WIF)
        ▼
  Entra Agent ID Sidecar  ──── client credentials (or WIF assertion) ───▶  Entra ID
        │                                                                 (login.microsoft.com)
        │  Entra Agent Identity JWT (scoped to Anthropic WIF audience)
        ▼
  This agent  ──── POST /v1/oauth/token (RFC 7523 jwt-bearer) ───▶  Anthropic WIF
        │                                                            (api.anthropic.com)
        │  Short-lived Claude access token
        ▼
  This agent  ──── POST /v1/messages (Bearer <claude_token>) ───▶  Claude API
        │
        │  Claude response
        ▼
  Caller

The agent code never handles Entra credentials, secrets, or token exchange logic.
All Entra identity work is delegated to the Microsoft Entra SDK auth sidecar.
The only logic here is the RFC 7523 exchange of the Entra JWT for a Claude token.

Supported flows:
  - Autonomous (app-only):  no user token required
  - OBO (on-behalf-of):     pass an Entra user Bearer token in the Authorization header
"""

import logging
import os

import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration (all supplied via environment variables)
# ---------------------------------------------------------------------------
SIDECAR_URL = os.environ.get("SIDECAR_URL", "http://sidecar:5000")
AGENT_APP_ID = os.environ.get("AGENT_APP_ID", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
ANTHROPIC_API_BASE = os.environ.get("ANTHROPIC_API_BASE", "https://api.anthropic.com")
ANTHROPIC_SERVICE_ACCOUNT_ID = os.environ.get("ANTHROPIC_SERVICE_ACCOUNT_ID", "")
ANTHROPIC_ORGANIZATION_ID = os.environ.get("ANTHROPIC_ORGANIZATION_ID", "")
ANTHROPIC_FEDERATION_RULE_ID = os.environ.get("ANTHROPIC_FEDERATION_RULE_ID", "")
DOWNSTREAM_API_NAME = "claude-api"  # matches DownstreamApis__claude-api__* in sidecar config


# ---------------------------------------------------------------------------
# Sidecar integration
# ---------------------------------------------------------------------------

def get_entra_agent_jwt(user_token: str | None = None) -> str:
    """
    Ask the Entra SDK sidecar for an Entra Agent Identity JWT scoped to the
    audience configured for Anthropic WIF (ENTRA_WIF_SCOPE in sidecar config).

    Autonomous flow: sidecar does client-credentials (or WIF via managed identity
    assertion) to obtain an Agent Identity token.  No user_token needed.

    OBO flow: the caller's Entra Bearer token is forwarded to the sidecar, which
    performs the On-Behalf-Of exchange to mint an agent-on-behalf-of-user token.

    Returns the raw JWT string (without the "Bearer " prefix) for use in the
    Anthropic RFC 7523 token exchange.
    """
    params = {
        "DownstreamApi": DOWNSTREAM_API_NAME,
        "AgentIdentity": AGENT_APP_ID,
    }
    headers = {}
    if user_token:
        headers["Authorization"] = f"Bearer {user_token}"

    headers["Host"] = "localhost"

    log.info("[WIF] Requesting Entra Agent ID JWT from sidecar (flow=%s)",
             "obo" if user_token else "autonomous")

    resp = requests.get(
        f"{SIDECAR_URL}/AuthorizationHeaderUnauthenticated/{DOWNSTREAM_API_NAME}?AgentIdentity={AGENT_APP_ID}",
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()

    auth_header = resp.json()["authorizationHeader"]
    log.info("[WIF] Entra JWT received: %s...", auth_header[:80])
    return auth_header.removeprefix("Bearer ")

# ---------------------------------------------------------------------------
# Anthropic Workload Identity Federation — token exchange
# ---------------------------------------------------------------------------

def exchange_entra_jwt_for_claude_token(entra_jwt: str) -> str:
    """
    Exchange an Entra Agent Identity JWT for a short-lived Anthropic access token
    using the RFC 7523 jwt-bearer grant (Anthropic Workload Identity Federation).

    POST /v1/oauth/token  (application/x-www-form-urlencoded)
      grant_type         = urn:ietf:params:oauth:grant-type:jwt-bearer
      assertion          = <raw Entra JWT>
      federation_rule_id = fdrl_... (rule configured in Anthropic Console)
      organization_id    = <Anthropic org UUID>
      service_account_id = svac_...

    Returns the short-lived Claude access token string.
    """
    if not ANTHROPIC_SERVICE_ACCOUNT_ID:
        raise ValueError("ANTHROPIC_SERVICE_ACCOUNT_ID is not configured")
    if not ANTHROPIC_ORGANIZATION_ID:
        raise ValueError("ANTHROPIC_ORGANIZATION_ID is not configured")
    if not ANTHROPIC_FEDERATION_RULE_ID:
        raise ValueError("ANTHROPIC_FEDERATION_RULE_ID is not configured")

    token_url = f"{ANTHROPIC_API_BASE}/v1/oauth/token"
    log.info("[WIF] Exchanging Entra JWT for Claude token at %s", token_url)

    resp = requests.post(
        token_url,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": entra_jwt,
            "federation_rule_id": ANTHROPIC_FEDERATION_RULE_ID,
            "organization_id": ANTHROPIC_ORGANIZATION_ID,
            "service_account_id": ANTHROPIC_SERVICE_ACCOUNT_ID,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()

    body = resp.json()
    claude_token = body["access_token"]
    log.info("[WIF] Claude access token obtained (expires_in=%s)", body.get("expires_in", "?"))
    return claude_token


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def call_claude(claude_access_token: str, message: str, system: str | None = None) -> dict:
    """
    Call the Anthropic Claude API using the short-lived access token obtained
    from the Anthropic WIF token exchange.  No Anthropic API key is used.
    """
    messages_url = f"{ANTHROPIC_API_BASE}/v1/messages"
    headers = {
        "Authorization": f"Bearer {claude_access_token}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    payload: dict = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": message}],
    }
    if system:
        payload["system"] = system

    log.info("[Claude] POST %s model=%s", messages_url, CLAUDE_MODEL)
    resp = requests.post(messages_url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    """Liveness probe."""
    return jsonify({"status": "ok", "agent_app_id": AGENT_APP_ID})


@app.route("/chat", methods=["POST"])
def chat():
    """
    POST /chat
    Body: { "message": "...", "system": "..." (optional) }
    Authorization header (optional): Bearer <Entra user token>  → triggers OBO flow

    Returns: { "response": "...", "model": "...", "flow": "autonomous|obo" }
    """
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    system = data.get("system")

    # Detect OBO flow: caller passes their own Entra Bearer token
    user_token: str | None = None
    incoming_auth = request.headers.get("Authorization", "")
    if incoming_auth.startswith("Bearer "):
        user_token = incoming_auth[len("Bearer "):]

    flow = "obo" if user_token else "autonomous"

    try:
        # Step 1 — Sidecar: obtain Entra Agent Identity JWT (WIF or client-credentials)
        entra_jwt = get_entra_agent_jwt(user_token)

        # Step 2 — Anthropic WIF: exchange Entra JWT for a short-lived Claude access token
        claude_token = exchange_entra_jwt_for_claude_token(entra_jwt)

        # Step 3 — Call Claude API directly with the WIF-derived access token
        result = call_claude(claude_token, message, system)

        content_blocks = result.get("content", [])
        text = content_blocks[0].get("text", "") if content_blocks else ""

        return jsonify({
            "response": text,
            "model": result.get("model"),
            "flow": flow,
            "usage": result.get("usage"),
        })

    except requests.HTTPError as exc:
        log.error("[Error] HTTP %s: %s", exc.response.status_code, exc.response.text)
        return jsonify({
            "error": f"HTTP {exc.response.status_code}",
            "details": exc.response.text,
        }), 502
    except Exception as exc:  # pylint: disable=broad-except
        log.error("[Error] %s", exc)
        return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=False)
