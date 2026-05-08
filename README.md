# Claude WIF AgentID — Proof of Concept

A **minimal runnable PoC** that uses **Microsoft Entra Agent ID** with **Workload Identity Federation (WIF)** to authenticate directly to the **Anthropic Claude API** — an Anthropic API subscription required, but no Anthropic API key.

This project follows the [**Entra Auth SDK (sidecar)**](https://learn.microsoft.com/en-us/entra/agent-id/authentication-with-auth-sdk-sidecar) for Entra Agent ID authentication, combining it with [Anthropic's native WIF support](https://platform.claude.com/docs/en/build-with-claude/workload-identity-federation).

---

## Can Entra Agent ID be used with the Anthropic Claude API?

**Yes — via Anthropic's Workload Identity Federation.**

Anthropic supports passwordless authentication through [Workload Identity Federation](https://platform.claude.com/docs/en/build-with-claude/workload-identity-federation): any OIDC-capable identity provider, including **Microsoft Entra**, can issue a signed JWT that is exchanged at `POST https://api.anthropic.com/v1/oauth/token` (RFC 7523 jwt-bearer grant) for a short-lived Claude access token.

This means an AI Agent using **Entra Agent Identity** can call Claude APIs utilizing its native agentic identity via Entra Agent ID.

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│                        claude-wif-network (Docker bridge)                 │
│                                                                           │
│  You (curl / browser)                                                     │
│   http://localhost:4192  ────────────────────────────────┐                │
│                                                           ▼               │
│  ┌───────────────────────────────────────┐                                │
│  │  claude-wif-agent  (Flask)            │                                │
│  │  :4192                                │                                │
│  │                                       │                                │
│  │  (1) Receive user query               │                                │
│  │  (2) Ask sidecar for Entra JWT        │                                │
│  └────────────────┬──────────────────────┘                                │
│                   │ (3) GET /AuthorizationHeaderUnauthenticated           │
│                   │    ?DownstreamApi=claude-api                          │
│                   │    &AgentIdentity=<agentId>                           │
│                   ▼                                                       │
│  ┌───────────────────────────────────────┐  (4) client-creds              │
│  │  claude-wif-sidecar                   │ ──────────────────────────▶   │
│  │  Microsoft Entra SDK Auth Sidecar     │                   Entra ID     │
│  │  NO host port — network-only          │ ◀──────────────────────────   │
│  └────────────────┬──────────────────────┘  (5) Entra Agent ID JWT        │
│                   │ (6) raw JWT (aud = app reg. for Anthropic APIs)       │
│                   ▼                                                       │
│  ┌───────────────────────────────────────┐  (7) POST /v1/oauth/token      │
│  │  claude-wif-agent exchanges JWT:      │ ──────────────────────────▶    │
│  │  RFC 7523 jwt-bearer grant            │              api.anthropic.com │
│  │                                       │ ◀──────────────────────────    │
│  │  (8) short-lived Claude access token  │  short-lived access token       │
│  │                                       │                                │
│  │  (9) POST /v1/messages                │ ──────────────────────────▶   │
│  │     Authorization: Bearer <token>     │              api.anthropic.com │
│  └───────────────────────────────────────┘                                │
└───────────────────────────────────────────────────────────────────────────┘
```

### Key insight

Steps (4) and (5) — An angetic application uses Entra Agent ID to obtain access token for calling the Anthropic APIs. The pattern, or SDK, used is Entra Auth SDK for Agent ID via sidecar container. Step (7) is the only Anthropic-specific step the agent performs: exchanging the Entra JWT for a Claude token via the standard RFC 7523 oauth token endpoint. No MSAL, no certificates, no API keys in agent memory.

---

## What is Workload Identity Federation (WIF)?


### Entra side
We have two key components from the Entra Side:
  1. An **application registration** that represents Anthropoic APIs and the established trust between Anthropic workload identity federation and Entra. 
  2. An Entra Agent Identity (with Blueprint, of course) that the agentic application will be using.

This proof-of-concept uses sidecar container to facilitate the token for Agent ID. For demo and proof-of-concept purposes we will use a `client secret` associated with the Agent Identity Blueprint, and provided in the `.env` file. 

> **Note:** do not use client secrets in production environments! Always use **managed identity**!

### Anthropic side (Anthropic APIs)
An active Anthropic APIs subscription where a Workload Identity Federation is configured following the guidance at [Workload Identity Federation](https://platform.claude.com/docs/en/build-with-claude/workload-identity-federation).
The agentic application will exchange an Entra JWT token for an Anthropic's access token. Anthropic validates the Entra issued JWT against the federation rules you defined in the Anthropic Console and returns a short-lived Claude access token. No Anthropic API key is ever used or stored. In fact, API Keys can be disabled on Anthropic's Claude workspace.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Docker + Docker Compose v2 | Any platform |
| Microsoft Entra ID tenant | For Agent Identity and workload identity configuration |
| **Anthropic API Subscription with active credits** | To create a service account and configure WIF |
| **Microsoft Entra Agent ID enabled** on the tenant | [Entra Agent ID overview](https://learn.microsoft.com/en-us/entra/agent-id/) |

---

## Required Entra objects (read this first)

This PoC requires **three distinct Microsoft Entra objects**. They are easy to confuse because two of them sound similar ("blueprint" / "agent identity") and the third ("the API app") is implicit in WIF and easy to miss.

### 1. Entra Agent Identity Blueprint

A first-class object in the Microsoft Entra Agent ID platform — **not** a regular app registration. It is the template/foundation for one or more agent identities and is the object that holds the credentials (client secret, certificate, or federated identity credential) used to acquire tokens for every agent identity created from it.

Reference: [Agent identity blueprints](https://learn.microsoft.com/en-us/entra/agent-id/agent-blueprint),
[Blueprint principals](https://learn.microsoft.com/en-us/entra/agent-id/agent-blueprint#agent-identity-blueprint-principals).

You will need the `client_id` and `client_secret` for the blueprint you created.

### 2. Entra Agent Identity (parented by the Blueprint)

A first-class **agent identity** object. It represents the runtime identity of one specific AI agent and **has no credentials of its own.**.

Reference: [Agent identities](https://learn.microsoft.com/en-us/entra/agent-id/agent-identities).

You will need `agent identity` object id for the agnet identity you create.

### 3. Entra App Registration representing the Anthropic API

This is the piece that is easy to miss but is **critical for the WIF concept to work end-to-end**. Anthropic's WIF rule validates an Entra-issued JWT, and the only way that JWT carries the right `aud` (audience) claim is if Entra issues it **for a registered resource application** whose `id` matches what you configured as the `Audience` of the federation issuer in the Anthropic Console.

This app registration:

- Is a **regular Entra application** (no special Agent ID API)
- Uses `requestedAccessTokenVersion: 2` — the app **must** request v2.0 tokens for the optional claims to work.
- Has `acceptMappedClaims: true` so the optional claims below are emitted.
- Does **not** need to expose OAuth2 permission scopes, because there will be no human impersonation (`oauth2PermissionScopes` can be empty). The sidecar requests the
  `/.default` scope of this app's identifier URI   (e.g. `api://anthropic.ai.dayzure.com/.default`) which is set as   `ENTRA_WIF_SCOPE`.
- Does **not* require any API Permissions, because it does not authenticate users. You may remove any default API Permissions requests, such as `User.Read`.
- **Requires specific Optional Claims** on the **access token**. These Microsoft-extended (`xms_*`) claims provide the token-provenance metadata that Anthropic's federation rule needs to match the incoming JWT:

  | Claim | Purpose |
  |-------|---------|
  | `xms_par_app_azp` | Parent application of the authorized party — identifies the parent application (blueprint) that requested the token on behalf of the agent identity. We will use this claim to create a rule at Anthropic's console to match all tokens issued by the same blueprint. |

> **Note:** You may want to check what other claims you can send to Claude's WIF platform to create more flexible and more robust federation rules: [token claims reference for agents](https://learn.microsoft.com/en-us/entra/agent-id/agent-token-claims)

  The JSON representation of the `optionalClaims` section is:

  ```json
  {
    "accessToken": [
      { "name": "xms_par_app_azp", "essential": false, "additionalProperties": [], "source": null }
    ],
    "idToken": [],
    "saml2Token": []
  }
  ```

  > **Without this claim** we must create a validation rule for each individual agent identity, instead of the blueprint.

Conceptually, the agent calls **this app** (it's the "downstream API"), and Entra issues a token with:

- `iss` = `https://login.microsoftonline.com/<tenant-id>/v2.0`
- `aud` = `98342-cd34...` (this the id of the app registration that represents anthropic APIs)
- `appid` = the **Agent Identity**'s client ID (object 2)
- `oid` = the **Agent Identity**'s object ID
- `xms_par_app_azp` = the agent identity blueprint id (the optional claim configured on this app — see table above)

That token is what Anthropic's WIF endpoint validates against your federation issuer + federation rule. No Anthropic API key is involved.

### How the three objects relate

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Microsoft Entra tenant                                                  │
│                                                                          │
│   ┌────────────────────────────┐                                         │
│   │ (1) Agent Identity         │  credentials live here                  │
│   │     Blueprint              │  (client secret / FIC / cert)           │
│   │     + Blueprint Principal  │  AgentIdentity.CreateAsManager          │
│   └──────────────┬─────────────┘                                         │
│                  │ provisions / impersonates                             │
│                  ▼                                                       │
│   ┌────────────────────────────┐                                         │
│   │ (2) Agent Identity         │  no credentials — blueprint mints       │
│   │     (one per AI agent)     │  tokens on its behalf                   │
│   └──────────────┬─────────────┘                                         │
│                  │ requests token for                                    │
│                  ▼                                                       │
│   ┌────────────────────────────┐                                         │
│   │ (3) App Registration:      │  Application ID URI →  aud claim        │
│   │     "Anthropic API"        │  Exposed scope        →  ENTRA_WIF_SCOPE│
│   │     + Optional Claims      │  Optional Claims      →  match rule     │
│   └────────────────────────────┘                                         │
└──────────────────────────────────────────────────────────────────────────┘
                  │
                  │  Entra-issued JWT  (aud = 98342-cd34...)
                  ▼
        Anthropic WIF endpoint  (federation issuer + federation rule)
                  │
                  ▼
        Short-lived Claude access token  → api.anthropic.com/v1/messages
```
---

## Setup

### 1. Provision Entra objects

> **Important:** read [Required Entra objects (read this first)](#required-entra-objects-read-this-first). The PoC requires **three** Entra objects: Agent Blueprint, Agent Identity, Anthropic-API app registration.


### 2. Configure Anthropic Console

In the [Anthropic Console](https://console.anthropic.com/) → Settings:

1. **Create a Service Account** — note the `svac_...` ID → `ANTHROPIC_SERVICE_ACCOUNT_ID`.
2. **Note your Organization ID** (UUID on the Organization settings page) → `ANTHROPIC_ORGANIZATION_ID`.
3. **Create a Federation Issuer**:
   - Issuer URL: `https://login.microsoftonline.com/<tenant-id>/v2.0`
   - Audience: `api://<anthropic-api-app-identifier-uri>` ← the Application
     ID URI of the **Anthropic API app registration** (object 3), **not** the
     blueprint's app ID. Printed by the provisioning script.
4. **Create a Federation Rule** linking the issuer to the service account.
   Match on the `appid` claim equal to your Agent Identity Client ID.
   Note the `fdrl_...` rule ID → `ANTHROPIC_FEDERATION_RULE_ID`.

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in all values from Entra and the Anthropic Console.
```

### 4. Run

```bash
docker compose --env-file .env up --build
```

### 5. Test

```bash
# Autonomous (app-only) flow — Agent Identity acts independently
curl -s -X POST http://localhost:4192/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Explain Workload Identity Federation in one paragraph."}' | jq .

curl -s -X POST http://localhost:4192/chat -H "Content-Type: application/json" -d '{"message": "Explain Workload Identity Federation in one paragraph."}'

# Health check
curl http://localhost:4192/health
```

Expected response:

```json
{
  "response": "Workload Identity Federation (WIF) is a mechanism that allows…",
  "model": "claude-3-5-sonnet-20241022",
  "flow": "autonomous",
  "usage": { "input_tokens": 18, "output_tokens": 120 }
}
```

---

## Learn more

- [Anthropic Workload Identity Federation](https://platform.claude.com/docs/en/build-with-claude/workload-identity-federation)
- [Microsoft Entra Agent ID overview](https://learn.microsoft.com/en-us/entra/agent-id/)
- [Microsoft Entra SDK Auth Sidecar](https://mcr.microsoft.com/en-us/product/entra-sdk/auth-sidecar/about)
- [Workload Identity Federation (Entra)](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation)
- [entra-agentid-samples](https://github.com/microsoft/entra-agentid-samples)
