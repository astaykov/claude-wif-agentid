# Claude WIF AgentID — Proof of Concept

A **minimal runnable PoC** that uses **Microsoft Entra Agent ID** with
**Workload Identity Federation (WIF)** to authenticate directly to the
**Anthropic Claude API** — no Anthropic API key required.

This project follows the **sidecar pattern** established in
[microsoft/entra-agentid-samples](https://github.com/microsoft/entra-agentid-samples/tree/main/sidecar/aws),
combining it with [Anthropic's native WIF support](https://platform.claude.com/docs/en/build-with-claude/workload-identity-federation).

---

## Can Entra Agent ID + WIF be used with the Anthropic Claude API?

**Yes — via Anthropic's Workload Identity Federation.**

Anthropic supports passwordless authentication through
[Workload Identity Federation](https://platform.claude.com/docs/en/build-with-claude/workload-identity-federation):
any OIDC-capable identity provider — including **Microsoft Entra** — can issue
a signed JWT that is exchanged at `POST https://api.anthropic.com/v1/oauth/token`
(RFC 7523 jwt-bearer grant) for a short-lived Claude access token.

This means an **Entra Agent Identity** service principal can call Claude
directly, with zero long-lived secrets stored anywhere.

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│                        claude-wif-network (Docker bridge)                 │
│                                                                           │
│  You (curl / browser)                                                     │
│   http://localhost:3000  ────────────────────────────────┐                │
│                                                           ▼               │
│  ┌───────────────────────────────────────┐                                │
│  │  claude-wif-agent  (Flask)            │                                │
│  │  :3000                                │                                │
│  │                                       │                                │
│  │  ① Receive user query                 │                                │
│  │  ② Ask sidecar for Entra JWT          │                                │
│  └────────────────┬──────────────────────┘                                │
│                   │ ③ GET /AuthorizationHeader                            │
│                   │    ?DownstreamApi=claude-api                          │
│                   │    &AgentIdentity=<agentId>                           │
│                   ▼                                                       │
│  ┌───────────────────────────────────────┐  ④ WIF / client-creds         │
│  │  claude-wif-sidecar                   │ ──────────────────────────▶   │
│  │  Microsoft Entra SDK Auth Sidecar     │                   Entra ID    │
│  │  NO host port — network-only          │ ◀──────────────────────────   │
│  └────────────────┬──────────────────────┘  ⑤ Entra Agent ID JWT        │
│                   │ ⑥ raw JWT (aud = Anthropic WIF audience)             │
│                   ▼                                                       │
│  ┌───────────────────────────────────────┐  ⑦ POST /v1/oauth/token      │
│  │  claude-wif-agent exchanges JWT:      │ ──────────────────────────▶   │
│  │  RFC 7523 jwt-bearer grant            │              api.anthropic.com │
│  │                                       │ ◀──────────────────────────   │
│  │  ⑧ short-lived Claude access token   │  short-lived access token     │
│  │                                       │                                │
│  │  ⑨ POST /v1/messages                 │ ──────────────────────────▶   │
│  │     Authorization: Bearer <token>     │              api.anthropic.com │
│  └───────────────────────────────────────┘                                │
└───────────────────────────────────────────────────────────────────────────┘
```

### Key insight

Steps ④ and ⑤ — Entra credential handling and token exchange — happen
exclusively inside the sidecar container. Step ⑦ is the only Anthropic-specific
step the agent performs: exchanging the Entra JWT for a Claude token via the
standard RFC 7523 oauth token endpoint. No MSAL, no certificates, no API keys
in agent memory.

---

## What is Workload Identity Federation (WIF)?

### Entra side (sidecar)
The sidecar authenticates the Blueprint app registration to Entra using either a
client secret (local dev) or a Managed Identity OIDC assertion (Azure/production).
It then mints an **Entra Agent Identity JWT** scoped to the audience you configured
in the Anthropic Console federation issuer.

### Anthropic side (agent)
The agent exchanges that Entra JWT at `POST /v1/oauth/token` using the
`urn:ietf:params:oauth:grant-type:jwt-bearer` grant type. Anthropic validates the
JWT against the federation rules you defined in the Anthropic Console and returns
a short-lived Claude access token. No Anthropic API key is ever used or stored.

| Local dev | Azure / WIF production |
|-----------|------------------------|
| Sidecar uses `ClientSecret` from env | Sidecar uses `SignedAssertionFromManagedIdentity` |
| Secret stored in `.env` | Managed Identity OIDC assertion is the `client_assertion` (RFC 7523) |
| One env-var change, **no code change** | No secret ever stored — true WIF end-to-end |

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Docker + Docker Compose v2 | Any platform |
| Azure subscription | For the Entra tenant |
| PowerShell 7+ | For the provisioning script |
| **Anthropic Console access** | To create a service account and configure WIF |

---

## Setup

### 1. Provision Entra objects

```powershell
# Clone and enter the repo
git clone https://github.com/astaykov/claude-wif-agentid.git
cd claude-wif-agentid

# Run the provisioning script (local dev — client secret)
./scripts/Provision-EntraObjects.ps1 -TenantId "<your-tenant-id>"
```

The script creates:
- **Blueprint app registration** with a client secret
- **Agent Identity** service principal, linked to the Blueprint via a FIC

It prints the values to paste into `.env` **and** the exact issuer/audience to
enter in the Anthropic Console.

### 2. Configure Anthropic Console

In the [Anthropic Console](https://console.anthropic.com/) → Settings:

1. **Create a Service Account** — note the `svac_...` ID → `ANTHROPIC_SERVICE_ACCOUNT_ID`.
2. **Note your Organization ID** (UUID on the Organization settings page) → `ANTHROPIC_ORGANIZATION_ID`.
3. **Create a Federation Issuer**:
   - Issuer URL: `https://login.microsoftonline.com/<tenant-id>/v2.0`
   - Audience: `api://<blueprint-app-id>` ← printed by the provisioning script
4. **Create a Federation Rule** linking the issuer to the service account.
   Match on the `appid` claim equal to your Agent Identity Client ID.
   Note the `fdrl_...` rule ID → `ANTHROPIC_FEDERATION_RULE_ID`.

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in all values printed by the provisioning script,
# plus ANTHROPIC_SERVICE_ACCOUNT_ID, ANTHROPIC_ORGANIZATION_ID,
# and ANTHROPIC_FEDERATION_RULE_ID from the Anthropic Console.
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

## OBO (On-Behalf-Of) flow

When a signed-in user's Entra Bearer token is available, pass it in the
`Authorization` header. The sidecar performs the OBO exchange and mints an
agent-on-behalf-of-user token, which is then exchanged with Anthropic WIF:

```bash
curl -s -X POST http://localhost:3000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <entra-user-token>" \
  -d '{"message": "Summarise my recent emails."}' | jq .
```

The response will show `"flow": "obo"`.

---

## Switching to true WIF (Azure deployment)

Change **two environment variables** in `docker-compose.yml` (or your
Container Apps/Kubernetes deployment manifest) — no agent code changes:

```yaml
# docker-compose.yml — sidecar service
- AzureAd__ClientCredentials__0__SourceType=SignedAssertionFromManagedIdentity
- AzureAd__ClientCredentials__0__ManagedIdentityResourceId=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/<mi-name>
```

Also add a FIC on the Blueprint app registration:

```powershell
# PowerShell — after provisioning, add WIF FIC for your MI
./scripts/Provision-EntraObjects.ps1 `
    -TenantId  "<your-tenant-id>" `
    -UseWIF `
    -WIFIssuer "https://token.actions.githubusercontent.com" `
    -WIFSubject "repo:myorg/myrepo:ref:refs/heads/main"
```

---

## Token flow (detailed)

```
[Workload runtime]
  MI / K8s SA / GitHub OIDC token
        │
        │ WIF exchange (sidecar → Entra)
        ▼
[Blueprint token (T1)]
  scope: api://<Blueprint AppId>
  used by sidecar only
        │
        │ Agent Identity FIC exchange
        ▼
[Entra Agent Identity JWT]
  aud:  api://<Blueprint AppId>  ← matches Anthropic federation issuer audience
  iss:  https://login.microsoftonline.com/<tenantId>/v2.0
  appid: Agent Identity App ID   ← matched by Anthropic federation rule
        │
        │ RFC 7523 jwt-bearer exchange
        │ POST api.anthropic.com/v1/oauth/token
        │   grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
        │   assertion=<Entra JWT>
        │   federation_rule_id=fdrl_...
        │   organization_id=<Anthropic org UUID>
        │   service_account_id=svac_...
        ▼
[Short-lived Claude access token]
        │
        │ Authorization: Bearer <claude_token>
        ▼
[Claude API]
  https://api.anthropic.com/v1/messages
```

---

## Relation to existing entra-agentid-samples

| Sample | LLM | Identity flow | This project |
|--------|-----|---------------|-------------|
| [`sidecar/dev`](https://github.com/microsoft/entra-agentid-samples/tree/main/sidecar/dev) | Ollama (local) | Client secret / MI | — |
| [`sidecar/aws`](https://github.com/microsoft/entra-agentid-samples/tree/main/sidecar/aws) | AWS Bedrock (Claude) | Client secret / MI | — |
| **This PoC** | **Claude API (direct)** | **Client secret / WIF** | ✓ |

The key difference: instead of routing Claude through a cloud provider's gateway
(Foundry or Bedrock), this PoC uses **Anthropic's native WIF** to exchange an
Entra JWT directly for a Claude access token — no cloud LLM proxy, no
provider-specific credentials.

---

## Learn more

- [Anthropic Workload Identity Federation](https://platform.claude.com/docs/en/build-with-claude/workload-identity-federation)
- [Microsoft Entra Agent ID overview](https://learn.microsoft.com/en-us/entra/agent-id/)
- [Microsoft Entra SDK Auth Sidecar](https://mcr.microsoft.com/en-us/product/entra-sdk/auth-sidecar/about)
- [Workload Identity Federation (Entra)](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation)
- [entra-agentid-samples](https://github.com/microsoft/entra-agentid-samples)
