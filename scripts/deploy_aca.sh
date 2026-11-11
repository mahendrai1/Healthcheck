#!/usr/bin/env bash
set -euo pipefail

dummy_subscription="00000000-0000-0000-0000-000000000000"
read -r -p "Azure subscription ID [$dummy_subscription]: " subscription_id
subscription_id="${subscription_id:-$dummy_subscription}"
read -r -p "Resource group name [raid-log-demo]: " resource_group
resource_group="${resource_group:-raid-log-demo}"
read -r -p "Azure region ID (for example eastus): " location
read -r -p "Entra tenant ID: " tenant_id
read -r -p "API audience / App ID URI: " audience
read -r -p "Required delegated API scope (for example raid.access): " required_scope
read -r -p "Container App name [raid-log-demo]: " app_name
app_name="${app_name:-raid-log-demo}"

if [[ "$subscription_id" == "$dummy_subscription" ]]; then
    echo "The subscription ID is still the dummy value; no Azure resources were changed." >&2
    exit 2
fi
if [[ ! "$subscription_id" =~ ^[0-9a-fA-F-]{36}$ || ! "$tenant_id" =~ ^[0-9a-fA-F-]{36}$ ]]; then
    echo "Subscription ID and tenant ID must be GUIDs." >&2
    exit 2
fi
if [[ -z "$resource_group" || -z "$location" || -z "$audience" || -z "$required_scope" ]]; then
    echo "Resource group, region, audience, and required scope are required." >&2
    exit 2
fi
if [[ ! "$app_name" =~ ^[a-zA-Z0-9-]+$ ]]; then
    echo "Container App name may contain only letters, numbers, and hyphens." >&2
    exit 2
fi
if ! command -v az >/dev/null 2>&1; then
    echo "Azure CLI (az) is required. Install it and run 'az login' first." >&2
    exit 127
fi

cat <<'WARNING'
This quick-start deploys an authenticated external demo, but its SQLite database
uses ephemeral container storage. Do not use it for real RAID data. Configure
durable storage and your organization's network/security requirements before
production use.
WARNING
read -r -p "Type DEPLOY-DEMO to continue, or anything else to cancel: " confirm
if [[ "$confirm" != "DEPLOY-DEMO" ]]; then
    echo "Cancelled; no Azure resources were changed."
    exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

az account set --subscription "$subscription_id"
az group create --name "$resource_group" --location "$location" --output none

az containerapp up \
    --name "$app_name" \
    --resource-group "$resource_group" \
    --location "$location" \
    --source . \
    --ingress external \
    --target-port 8000 \
    --env-vars \
        MCP_TRANSPORT=streamable-http \
        MCP_AUTH_MODE=entra \
        MCP_TENANT_ID="$tenant_id" \
        MCP_AUDIENCE="$audience" \
        MCP_REQUIRED_SCOPE="$required_scope" \
        MCP_RESOURCE_URL=https://placeholder.invalid/mcp \
        RAID_DB_PATH=/data/raid_log.sqlite3

fqdn="$(az containerapp show \
    --name "$app_name" \
    --resource-group "$resource_group" \
    --query properties.configuration.ingress.fqdn \
    --output tsv)"
if [[ -z "$fqdn" ]]; then
    echo "Could not determine the deployed Container App hostname." >&2
    exit 1
fi

az containerapp update \
    --name "$app_name" \
    --resource-group "$resource_group" \
    --set-env-vars "MCP_RESOURCE_URL=https://$fqdn/mcp" \
    --output none

printf 'MCP endpoint: https://%s/mcp\n' "$fqdn"
echo "Configure the Entra app registration and Copilot Studio OAuth connection before use."
