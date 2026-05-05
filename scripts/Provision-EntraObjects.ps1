<#
.SYNOPSIS
    Provisions the Microsoft Entra objects required for the claude-wif-agentid PoC.

.DESCRIPTION
    Creates:
      1. Agent Identity Blueprint app registration (with client secret or WIF FIC)
      2. Agent Identity service principal, linked to the Blueprint via a FIC

    Note: No Azure RBAC assignment is needed — the agent authenticates to
    api.anthropic.com via Anthropic Workload Identity Federation, not Microsoft Foundry.
    You must configure the matching federation issuer and rule in the Anthropic Console
    (see README for instructions).

    Outputs the values needed in .env (or sidecar env vars for Azure deployment).

.PARAMETER TenantId
    Your Entra tenant ID.

.PARAMETER BlueprintDisplayName
    Display name for the Blueprint app registration (default: "Claude-WIF-Blueprint").

.PARAMETER AgentDisplayName
    Display name for the Agent Identity (default: "Claude-WIF-Agent").

.PARAMETER UseWIF
    When set, configures a Federated Identity Credential on the Blueprint instead of
    a client secret.  Pass the OIDC issuer URL and subject of your workload runtime
    (e.g. GitHub Actions, Azure Kubernetes Service Service Account, etc.).

.PARAMETER WIFIssuer
    OIDC issuer URL for the WIF FIC (required when -UseWIF is set).

.PARAMETER WIFSubject
    Subject claim for the WIF FIC (required when -UseWIF is set).

.EXAMPLE
    # Local dev: client secret
    ./Provision-EntraObjects.ps1 -TenantId "…"

    # Production: WIF with GitHub Actions OIDC
    ./Provision-EntraObjects.ps1 -TenantId "…" `
        -UseWIF `
        -WIFIssuer "https://token.actions.githubusercontent.com" `
        -WIFSubject "repo:myorg/myrepo:ref:refs/heads/main"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $TenantId,

    [string] $BlueprintDisplayName = "Claude-WIF-Blueprint",
    [string] $AgentDisplayName     = "Claude-WIF-Agent",

    [switch] $UseWIF,
    [string] $WIFIssuer,
    [string] $WIFSubject
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Prerequisites ────────────────────────────────────────────────────────────
$requiredModules = @("Microsoft.Entra", "Microsoft.Entra.Beta")
foreach ($mod in $requiredModules) {
    if (-not (Get-Module -ListAvailable -Name $mod)) {
        Write-Host "Installing $mod …" -ForegroundColor Cyan
        Install-Module -Name $mod -Repository PSGallery -Force -AllowClobber -Scope CurrentUser
    }
}

# ── Connect ──────────────────────────────────────────────────────────────────
Write-Host "`n=== Connecting to Microsoft Entra ===" -ForegroundColor Cyan
Connect-Entra -TenantId $TenantId -Scopes `
    "Application.ReadWrite.All",
    "AppRoleAssignment.ReadWrite.All",
    "AgentIdentity.ReadWrite.All",
    "AgentIdentityBlueprint.ReadWrite.All",
    "AgentIdentityBlueprintPrincipal.ReadWrite.All"

# ── 1. Blueprint App Registration ────────────────────────────────────────────
Write-Host "`n[1/3] Creating Blueprint app registration: $BlueprintDisplayName" -ForegroundColor Cyan

$blueprint = New-EntraApplication -DisplayName $BlueprintDisplayName
$blueprintSp = New-EntraServicePrincipal -AppId $blueprint.AppId

Write-Host "      Blueprint App ID:    $($blueprint.AppId)"
Write-Host "      Blueprint Object ID: $($blueprint.Id)"

# ── 2. Credential: client secret (dev) or WIF FIC (prod) ────────────────────
$secretText = $null
if ($UseWIF) {
    if (-not $WIFIssuer -or -not $WIFSubject) {
        throw "Both -WIFIssuer and -WIFSubject are required when -UseWIF is set."
    }
    Write-Host "`n[2/3] Adding Federated Identity Credential (WIF) to Blueprint" -ForegroundColor Cyan
    $fic = @{
        Name        = "wif-credential"
        Issuer      = $WIFIssuer
        Subject     = $WIFSubject
        Description = "WIF credential for claude-wif-agentid PoC"
        Audiences   = @("api://AzureADTokenExchange")
    }
    New-EntraApplicationFederatedIdentityCredential -ApplicationId $blueprint.Id @fic | Out-Null
    Write-Host "      FIC configured: issuer=$WIFIssuer  subject=$WIFSubject"
} else {
    Write-Host "`n[2/3] Adding client secret to Blueprint (local dev)" -ForegroundColor Cyan
    $secretResult = Add-EntraApplicationPassword -ApplicationId $blueprint.Id `
        -PasswordCredential @{ DisplayName = "dev-secret" }
    $secretText = $secretResult.SecretText
    Write-Host "      Secret created (copy now — not shown again)"
}

# ── 3. Agent Identity ────────────────────────────────────────────────────────
Write-Host "`n[3/3] Creating Agent Identity: $AgentDisplayName" -ForegroundColor Cyan

# Use the Beta cmdlet for Agent Identity (Entra Agent ID preview feature)
$agentApp = New-EntraBetaApplication -DisplayName $AgentDisplayName
$agentSp  = New-EntraBetaServicePrincipal -AppId $agentApp.AppId

Write-Host "      Agent Client ID:   $($agentApp.AppId)"
Write-Host "      Agent Object ID:   $($agentApp.Id)"
Write-Host "      Agent SP ID:       $($agentSp.Id)"

# Link Agent Identity to Blueprint via Federated Identity Credential
# (Blueprint acts as the trusted issuer for the Agent Identity)
$agentFic = @{
    Name        = "blueprint-fic"
    Issuer      = "api://$($blueprint.AppId)"
    Subject     = $agentApp.AppId
    Description = "Blueprint-issued FIC for Agent Identity"
    Audiences   = @("api://AzureADTokenExchange")
}
New-EntraBetaApplicationFederatedIdentityCredential -ApplicationId $agentApp.Id @agentFic | Out-Null
Write-Host "      Agent FIC linked to Blueprint"

# ── Summary ──────────────────────────────────────────────────────────────────
Write-Host "`n=== Provisioning complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Copy the following values into your .env file:" -ForegroundColor Yellow
Write-Host ""
Write-Host "TENANT_ID=$TenantId"
Write-Host "BLUEPRINT_APP_ID=$($blueprint.AppId)"
if ($secretText) {
    Write-Host "BLUEPRINT_CLIENT_SECRET=$secretText"
} else {
    Write-Host "# WIF configured — no client secret. Set sidecar credential source to:"
    Write-Host "# AzureAd__ClientCredentials__0__SourceType=SignedAssertionFromManagedIdentity"
}
Write-Host "AGENT_CLIENT_ID=$($agentApp.AppId)"
Write-Host "ENTRA_WIF_SCOPE=api://$($blueprint.AppId)/.default"
Write-Host ""
Write-Host "Next steps — Anthropic Console:" -ForegroundColor Yellow
Write-Host "  1. Create a Service Account in the Anthropic Console."
Write-Host "     Set ANTHROPIC_SERVICE_ACCOUNT_ID=<svac_...> in your .env."
Write-Host "  2. Note your Organization ID from the Console Organization settings page."
Write-Host "     Set ANTHROPIC_ORGANIZATION_ID=<UUID> in your .env."
Write-Host "  3. Create a Federation Issuer:"
Write-Host "     Issuer URL : https://login.microsoftonline.com/$TenantId/v2.0"
Write-Host "     Audience   : api://$($blueprint.AppId)"
Write-Host "  4. Create a Federation Rule mapping the Agent Identity's token claims"
Write-Host "     (e.g. appid=$($agentApp.AppId)) to your service account."
Write-Host "     Set ANTHROPIC_FEDERATION_RULE_ID=<fdrl_...> in your .env."
Write-Host "  5. See README for full instructions."
Write-Host ""

if ($UseWIF) {
    Write-Host "WIF deployment notes:" -ForegroundColor Cyan
    Write-Host "  - Assign a Managed Identity to your sidecar container / Container App."
    Write-Host "  - Add a FIC on the Blueprint app trusting that MI's OIDC issuer + subject."
    Write-Host "  - Change sidecar env var:"
    Write-Host "      AzureAd__ClientCredentials__0__SourceType=SignedAssertionFromManagedIdentity"
    Write-Host "      AzureAd__ClientCredentials__0__ManagedIdentityResourceId=<MI resource ID>"
}
