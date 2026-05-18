"""
Claude API — Entra Agent ID WIF Proof of Concept
=================================================
This agent demonstrates the sidecar pattern for Workload Identity Federation
using the native Anthropic Python SDK with built-in WIF support:

  Workload runtime (Azure MI / K8s SA / GitHub OIDC)
        │  OIDC assertion (WIF)
        ▼
  Entra Agent ID Sidecar  ──── client credentials (or WIF assertion) ───▶  Entra ID
        │                                                                 (login.microsoft.com)
        │  Entra Agent Identity JWT (scoped to Anthropic WIF audience)
        ▼
  Anthropic SDK  ──── POST /v1/oauth/token (RFC 7523 jwt-bearer) ───▶  Anthropic WIF
        │              (handled internally by the SDK)                  (api.anthropic.com)
        │  Short-lived Claude access token (cached + auto-refreshed)
        ▼
  Anthropic SDK  ──── POST /v1/messages (Bearer <claude_token>) ───▶  Claude API
        │
        │  Claude response
        ▼
  Caller

The agent code never handles Entra credentials, secrets, or token exchange logic.
All Entra identity work is delegated to the Microsoft Entra SDK auth sidecar.
The Anthropic SDK handles the RFC 7523 exchange, token caching, and auto-refresh.
"""

import logging
import os

import anthropic
import requests
from anthropic import WorkloadIdentityCredentials
from flask import Flask, jsonify, request, send_from_directory

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
ANTHROPIC_WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID") or None
DOWNSTREAM_API_NAME = "claude-api"  # matches DownstreamApis__claude-api__* in sidecar config


# ---------------------------------------------------------------------------
# Sidecar integration — identity token provider for the Anthropic SDK
# ---------------------------------------------------------------------------

def _fetch_entra_jwt() -> str:
    """
    Fetch an Entra Agent Identity JWT from the sidecar.

    Conforms to the Anthropic SDK's identity_token_provider interface:
    takes no arguments and returns a raw JWT string.

    The sidecar uses client-credentials (or WIF via managed identity assertion)
    to obtain an Agent Identity token scoped to the Anthropic WIF audience.
    """
    log.info("[WIF] Requesting Entra Agent ID JWT from sidecar")

    resp = requests.get(
        f"{SIDECAR_URL}/AuthorizationHeaderUnauthenticated/"
        f"{DOWNSTREAM_API_NAME}?AgentIdentity={AGENT_APP_ID}",
        headers={"Host": "localhost"},
        timeout=15,
    )
    resp.raise_for_status()

    auth_header = resp.json()["authorizationHeader"]
    log.info("[WIF] Entra JWT received: %s...", auth_header[:80])
    return auth_header.removeprefix("Bearer ")


# ---------------------------------------------------------------------------
# Anthropic SDK client construction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Anthropic SDK client (singleton — lazy init)
# ---------------------------------------------------------------------------

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Lazily initialise and return the singleton Anthropic client.

    The SDK caches the WIF access token and auto-refreshes it before expiry.
    """
    global _client  # noqa: PLW0603
    if _client is None:
        _client = anthropic.Anthropic(
            base_url=ANTHROPIC_API_BASE,
            credentials=WorkloadIdentityCredentials(
                identity_token_provider=_fetch_entra_jwt,
                federation_rule_id=ANTHROPIC_FEDERATION_RULE_ID,
                organization_id=ANTHROPIC_ORGANIZATION_ID,
                service_account_id=ANTHROPIC_SERVICE_ACCOUNT_ID,
                workspace_id=ANTHROPIC_WORKSPACE_ID,
            ),
        )
    return _client


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the SPA."""
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    """Liveness probe."""
    return jsonify({"status": "ok", "agent_app_id": AGENT_APP_ID})


@app.route("/chat", methods=["POST"])
def chat():
    """
    POST /chat
    Body: { "message": "...", "messages": [...], "system": "..." (optional) }

    Returns: { "response": "...", "model": "...", "flow": "autonomous" }
    """
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages")  # multi-turn: [{role, content}, ...]
    message = (data.get("message") or "").strip()
    if not messages and not message:
        return jsonify({"error": "message or messages is required"}), 400

    system = data.get("system")

    try:
        client = _get_client()

        # The SDK handles: sidecar JWT → RFC 7523 exchange → token caching → API call
        kwargs: dict = {
            "model": CLAUDE_MODEL,
            "max_tokens": 1024,
            "messages": messages if messages else [{"role": "user", "content": message}],
        }
        if system:
            kwargs["system"] = system

        log.info("[Claude] messages.create model=%s", CLAUDE_MODEL)
        result = client.messages.create(**kwargs)

        text = result.content[0].text if result.content else ""

        return jsonify({
            "response": text,
            "model": result.model,
            "flow": "autonomous",
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
        })

    except anthropic.APIStatusError as exc:
        log.error("[Error] Anthropic API %s: %s", exc.status_code, exc.message)
        return jsonify({
            "error": f"HTTP {exc.status_code}",
            "details": exc.message,
        }), 502
    except requests.HTTPError as exc:
        log.error("[Error] Sidecar HTTP %s: %s", exc.response.status_code, exc.response.text)
        return jsonify({
            "error": f"Sidecar HTTP {exc.response.status_code}",
            "details": exc.response.text,
        }), 502
    except Exception as exc:  # pylint: disable=broad-except
        log.error("[Error] %s", exc)
        return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=False)
