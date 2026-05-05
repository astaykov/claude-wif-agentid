"""
Claude via Microsoft Foundry — Entra Agent ID WIF Proof of Concept
===================================================================
This agent demonstrates the sidecar pattern for Workload Identity Federation:

  Workload runtime (Azure MI / K8s SA / GitHub OIDC)
        │  OIDC assertion (WIF)
        ▼
  Entra Agent ID Sidecar  ──── client credentials (or WIF assertion) ───▶  Entra ID
        │                                                                 (login.microsoft.com)
        │  Authorization: Bearer <Agent ID token for cognitiveservices>
        ▼
  This agent  ──── POST /anthropic/v1/messages (Bearer token) ───▶  Claude via Microsoft Foundry
        │                                                            (*.services.ai.azure.com)
        │  Claude response
        ▼
  Caller

The agent code never handles credentials, secrets, or token exchange logic.
All identity work is delegated to the Microsoft Entra SDK auth sidecar.

Supported flows:
  - Autonomous (app-only):  no user token required
  - OBO (on-behalf-of):     pass an Entra user Bearer token in the Authorization header
"""

import json
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
FOUNDRY_ENDPOINT = os.environ.get("FOUNDRY_ENDPOINT", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
DOWNSTREAM_API_NAME = "claude-api"  # matches DownstreamApis__claude-api__* in sidecar config


# ---------------------------------------------------------------------------
# Sidecar integration
# ---------------------------------------------------------------------------

def get_agent_auth_header(user_token: str | None = None) -> str:
    """
    Ask the Entra SDK sidecar for an Authorization header scoped to
    https://cognitiveservices.azure.com/.default (Microsoft Foundry / Claude).

    Autonomous flow: sidecar does client-credentials (or WIF via managed identity
    assertion) to obtain an Agent Identity token.  No user_token needed.

    OBO flow: the caller's Entra Bearer token is forwarded to the sidecar, which
    performs the On-Behalf-Of exchange to mint an agent-on-behalf-of-user token.
    """
    params = {
        "DownstreamApi": DOWNSTREAM_API_NAME,
        "AgentIdentity": AGENT_APP_ID,
    }
    headers = {}
    if user_token:
        headers["Authorization"] = f"Bearer {user_token}"

    log.info("[WIF] Requesting Agent ID token from sidecar (flow=%s)",
             "obo" if user_token else "autonomous")

    resp = requests.get(
        f"{SIDECAR_URL}/AuthorizationHeader",
        params=params,
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()

    auth_header = resp.text.strip()
    # Log only the token type prefix for debugging, never the full token
    log.info("[WIF] Token received: %s...", auth_header[:20])
    return auth_header


# ---------------------------------------------------------------------------
# Claude via Microsoft Foundry
# ---------------------------------------------------------------------------

def call_claude(auth_header: str, message: str, system: str | None = None) -> dict:
    """
    Call Claude via the Microsoft Foundry endpoint using the Entra Agent ID
    Bearer token obtained from the sidecar.  The Authorization header is the
    WIF-derived token — no Anthropic API key is used.
    """
    if not FOUNDRY_ENDPOINT:
        raise ValueError("FOUNDRY_ENDPOINT is not configured")

    headers = {
        "Authorization": auth_header,
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

    log.info("[Claude] POST %s model=%s", FOUNDRY_ENDPOINT, CLAUDE_MODEL)
    resp = requests.post(FOUNDRY_ENDPOINT, headers=headers, json=payload, timeout=60)
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
        # Step 1 — WIF: ask sidecar for Agent Identity token for Azure Cognitive Services
        agent_auth_header = get_agent_auth_header(user_token)

        # Step 2 — Call Claude via Microsoft Foundry with the WIF-derived token
        result = call_claude(agent_auth_header, message, system)

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
