# Claude WIF AgentID — Proof of Concept

A **minimal runnable PoC** that uses **Microsoft Entra Agent ID** with
**Workload Identity Federation (WIF)** to authenticate to
**Claude via Microsoft Foundry** — _no Anthropic API key required_.

This project follows the **sidecar pattern** established in
[microsoft/entra-agentid-samples](https://github.com/microsoft/entra-agentid-samples/tree/main/sidecar/aws),
adapting it from AWS Bedrock to the direct Claude endpoint on Azure.

---

## Can Entra Agent ID + WIF be used with the Claude API?

**Yes — via Microsoft Foundry.**

Anthropic Claude models (Sonnet, Haiku, Opus) are available through
[Microsoft Foundry](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-claude)
at an endpoint such as:

```
https://<resource>.services.ai.azure.com/anthropic/v1/messages
```

This endpoint uses **Azure AD / Entra authentication** (scope:
`https://cognitiveservices.azure.com/.default`), meaning any service
principal — including an **Entra Agent Identity** — can call Claude with a
standard Bearer token obtained through Entra.

> **Direct `api.anthropic.com`**: the Anthropic public API uses API keys and
> does _not_ accept Entra tokens. For Entra WIF authentication, always use the
> Foundry endpoint.

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
│  │  ② Ask sidecar for auth header        │                                │
│  └────────────────┬──────────────────────┘                                │
│                   │ ③ GET /AuthorizationHeader                            │
│                   │    ?DownstreamApi=claude-api                          │
│                   │    &AgentIdentity=<agentId>                           │
│                   ▼                                                       │
│  ┌───────────────────────────────────────┐  ④ WIF / client-creds         │
│  │  claude-wif-sidecar                   │ ──────────────────────────▶   │
│  │  Microsoft Entra SDK Auth Sidecar     │                   Entra ID    │
│  │  NO host port — network-only          │ ◀──────────────────────────   │
│  └────────────────┬──────────────────────┘  ⑤ Agent ID Bearer token     │
│                   │ ⑥ Authorization: Bearer <Agent ID token>             │
│                   ▼                                                       │
│  ┌───────────────────────────────────────┐                                │
│  │  ⑦ claude-wif-agent calls Foundry:   │                                │
│  │     POST *.services.ai.azure.com/     │                                │
│  │          anthropic/v1/messages        │                                │
│  │     with WIF-derived Bearer token     │                                │
│  └───────────────────────────────────────┘                                │
└───────────────────────────────────────────────────────────────────────────┘
```

### Key insight

Steps ④ and ⑤ — credential handling and token exchange — happen exclusively
inside the sidecar container, on a Docker network the host cannot reach
directly. The agent code at step ② does a plain `GET` to the sidecar; no
MSAL, no certificates, no secrets in agent memory.

---

## What is Workload Identity Federation (WIF)?

WIF eliminates long-lived secrets from workload runtimes:

| Local dev | Azure / WIF production |
|-----------|------------------------|
| Sidecar uses `ClientSecret` from env | Sidecar uses `SignedAssertionFromManagedIdentity` |
| Blueprint secret stored in `.env` | Managed Identity OIDC assertion is sent to Entra as `client_assertion` (RFC 7523) |
| One env-var change, **no code change** | No secret ever stored — true WIF |

The FIC (Federated Identity Credential) on the Blueprint app registration
tells Entra: _"trust assertions signed by this OIDC issuer for this subject"_.
At runtime the sidecar requests the MI OIDC token from Azure IMDS, sends it
as `client_assertion`, and Entra issues a Blueprint token. The Blueprint then
mints an Agent Identity token for the downstream API (Foundry/Claude).

For local dev, `ClientSecret` is a functional equivalent — same API surface,
same agent code, swap one environment variable.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Docker + Docker Compose v2 | Any platform |
| Azure subscription | For the Foundry resource and Entra tenant |
| **Microsoft Foundry resource** | With a Claude model deployed (e.g. `claude-3-5-sonnet-20241022`) |
| PowerShell 7+ | For the provisioning script |
| Azure CLI + `az login` | Used by the provisioning script for RBAC assignment |

---

## Quick start

### 1. Provision Entra objects

```powershell
# Clone and enter the repo
git clone https://github.com/astaykov/claude-wif-agentid.git
cd claude-wif-agentid

# Run the provisioning script (local dev — client secret)
./scripts/Provision-EntraObjects.ps1 `
    -TenantId         "<your-tenant-id>" `
    -FoundryResourceId "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<foundry-name>"
```

The script creates:
- **Blueprint app registration** with a client secret
- **Agent Identity** service principal, linked to the Blueprint via a FIC
- **Cognitive Services User** RBAC role assignment on the Foundry resource

It prints the values to paste into `.env`.

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in all values printed by the script above,
# plus your Foundry endpoint URL.
```

### 3. Run

```bash
docker compose --env-file .env up --build
```

### 4. Test

```bash
# Autonomous (app-only) flow — Agent Identity acts independently
curl -s -X POST http://localhost:3000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Explain Workload Identity Federation in one paragraph."}' | jq .

# Health check
curl http://localhost:3000/health
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
agent-on-behalf-of-user token:

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
    -TenantId         "<your-tenant-id>" `
    -FoundryResourceId "…" `
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
[Agent Identity token (TR)]
  scope: https://cognitiveservices.azure.com/.default
  aud:   your Foundry resource
  xms_par_app_azp: Agent Identity App ID  ← auditable in Entra logs
        │
        │ Bearer TR in Authorization header
        ▼
[Claude via Microsoft Foundry]
  https://<resource>.services.ai.azure.com/anthropic/v1/messages
```

The `xms_par_app_azp` claim in the Agent Identity token tells the Foundry
endpoint _which specific agent_ made the call — enabling per-agent auditing
and Conditional Access policy.

---

## Relation to existing entra-agentid-samples

| Sample | LLM | Identity flow | This project |
|--------|-----|---------------|-------------|
| [`sidecar/dev`](https://github.com/microsoft/entra-agentid-samples/tree/main/sidecar/dev) | Ollama (local) | Client secret / MI | — |
| [`sidecar/aws`](https://github.com/microsoft/entra-agentid-samples/tree/main/sidecar/aws) | AWS Bedrock (Claude) | Client secret / MI | — |
| **This PoC** | **Claude via Microsoft Foundry** | **Client secret / WIF** | ✓ |

The key difference: instead of calling Claude via AWS Bedrock (with separate
AWS credentials), this PoC calls Claude via the **Azure-native Foundry
endpoint using the Entra Agent ID token directly**. No AWS credentials, no
Anthropic API key — just the WIF-derived Entra Bearer token.

---

## n8n workflow equivalent

The three n8n workflows from
[microsoft/entra-agentid-samples/n8n](https://github.com/microsoft/entra-agentid-samples/tree/main/n8n)
(using the [`@astaykov/n8n-nodes-entraagentid`](https://www.npmjs.com/package/@astaykov/n8n-nodes-entraagentid)
community node) can be adapted to call Claude via Foundry:

1. **Autonomous workflow**: use the `EntraAgentID` node to acquire an Azure
   Cognitive Services token, then an HTTP Request node to POST to
   `https://<resource>.services.ai.azure.com/anthropic/v1/messages`.

2. **OBO webhook workflow**: receive a user Entra token via webhook, pass it
   to the `EntraAgentID` node for OBO exchange, then call Claude with the
   resulting agent token.

3. **Autonomous + Graph MCP**: same as (1) but also call Microsoft Graph via
   a second `EntraAgentID` token acquisition before or after the Claude call.

Deploying the full n8n stack on Azure Container Apps is covered in
[astaykov/n8n-aca](https://github.com/astaykov/n8n-aca).

---

## Learn more

- [Microsoft Entra Agent ID overview](https://learn.microsoft.com/en-us/entra/agent-id/)
- [Microsoft Entra SDK Auth Sidecar](https://mcr.microsoft.com/en-us/product/entra-sdk/auth-sidecar/about)
- [Deploy Claude models in Microsoft Foundry](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-claude)
- [Workload Identity Federation](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation)
- [entra-agentid-samples](https://github.com/microsoft/entra-agentid-samples)