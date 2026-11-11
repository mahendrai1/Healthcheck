# Healthcheck

## Local RAID-log MCP server

This repository includes a standalone Python server that exposes five RAID-log
tools over MCP. It stores items and close-event audit records in SQLite. The
database defaults to `raid_log.sqlite3` in the current directory; set
`RAID_DB_PATH` to choose another location.

Install the dependency and start the server over stdio:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python raid_server.py
```

### Test with MCP Inspector before connecting it to a client

In another terminal, with the Python dependency installed, inspect the server
directly:

```sh
npx -y @modelcontextprotocol/inspector --cli python raid_server.py
```

The CLI starts the server as a subprocess. List the exposed tools:

```sh
npx -y @modelcontextprotocol/inspector --cli env RAID_DB_PATH=/tmp/raid-inspector.sqlite3 python raid_server.py --method tools/list
```

Call each tool from Inspector before configuring any MCP client:

```sh
npx -y @modelcontextprotocol/inspector --cli env RAID_DB_PATH=/tmp/raid-inspector.sqlite3 python raid_server.py --method tools/call --tool-name list_raid_items
npx -y @modelcontextprotocol/inspector --cli env RAID_DB_PATH=/tmp/raid-inspector.sqlite3 python raid_server.py --method tools/call --tool-name add_item --tool-arg type=risk --tool-arg title="Inspector check" --tool-arg description="Temporary test item" --tool-arg reason="Inspector test"
npx -y @modelcontextprotocol/inspector --cli env RAID_DB_PATH=/tmp/raid-inspector.sqlite3 python raid_server.py --method tools/call --tool-name get_item --tool-arg id=1
npx -y @modelcontextprotocol/inspector --cli env RAID_DB_PATH=/tmp/raid-inspector.sqlite3 python raid_server.py --method tools/call --tool-name close_item --tool-arg id=1 --tool-arg reason="Verified in Inspector"
npx -y @modelcontextprotocol/inspector --cli env RAID_DB_PATH=/tmp/raid-inspector.sqlite3 python raid_server.py --method tools/call --tool-name delete_item --tool-arg id=1
```

`delete_item` is intentionally blocked and should return a tool error. Use a
temporary database when trying Inspector examples so test items do not mix with
real data, as in the commands above.

Run the local behavior tests with:

```sh
python -m unittest discover -s tests
```

## Connect from VS Code

The checked-in [.vscode/mcp.json](.vscode/mcp.json) registers the local
stdio server as `raid-log`. Open the repository in VS Code, install the Python
requirements, then open Copilot Chat in **Agent** mode and enable the server's
tools when prompted. The local database is `raid_log.sqlite3` in the workspace.

Try asking the agent to list open risks, add a RAID item, or close an item with
a reason. Writes require a reason and explicit confirmation in the agent
instructions. `delete_item` is intentionally blocked.

## Reusable Copilot skills

Three repository skills live under `.github/skills/`:

- `weekly-status-update` creates a source-grounded weekly report with RAG,
  leading risks, and explicit asks.
- `raid-triage` checks for duplicates and recommends type, priority, and
  ownership before any requested write. Priority is not a stored field.
- `steering-committee-pack` produces a short, decision-oriented slide outline
  or brief from open RAID items.

Test automatic selection in VS Code Copilot Agent mode with the paraphrased
prompts in [tests/skill-discovery-prompts.md](tests/skill-discovery-prompts.md).
Submit them without naming a skill, and verify both skill selection and the
expected read/write behavior.

The broader 18-case Agent acceptance suite and per-run result log are in
[tests/agent-acceptance-prompts.md](tests/agent-acceptance-prompts.md). Cases
requiring Copilot Agent are marked not run until executed in VS Code; local
server tests do not count as Agent acceptance results.

## Test-runner agent

The repository includes a custom VS Code agent at
[.github/agents/raid-app-tester.agent.md](.github/agents/raid-app-tester.agent.md).
Select **RAID App Tester** from Copilot Chat's agent picker and ask it to run
the app tests or MCP smoke checks. It is instructed to use disposable data,
report exactly which checks ran, and avoid cloud deployment or live connectors.

## Azure Container Apps and Copilot Studio

The [Dockerfile](./Dockerfile) runs Streamable HTTP on port 8000 and stores its
database at `/data/raid_log.sqlite3`. Mount durable Azure Files storage at
`/data`; the container's writable layer is not durable. HTTP mode is
authenticated. For a short-lived internal pilot, set
`MCP_AUTH_MODE=api-key` and inject `MCP_API_KEY` from a secret store (at least
32 characters); requests must include it in `X-API-Key`. The shared key is not
user identity and should be rotated and replaced with Entra ID for production.
The default `MCP_AUTH_MODE=entra` refuses to start unless token validation is
configured with all of these environment variables:

- `MCP_TENANT_ID`: Entra tenant GUID.
- `MCP_AUDIENCE`: API audience/App ID URI for the Entra-protected API.
- `MCP_REQUIRED_SCOPE`: delegated API scope required for tool calls.
- `MCP_RESOURCE_URL`: the deployed MCP endpoint URL, for example
  `https://<container-app-fqdn>/mcp`.

Build and deploy the image to Azure Container Apps using your organization's
approved registry and identity/network policies. The interactive quick-start
below prompts for your subscription ID, resource group, Azure region ID, Entra
tenant, API audience, and delegated scope. Its dummy subscription default is
deliberately rejected; no Azure changes occur unless you enter real values and
confirm `DEPLOY-DEMO`.

```sh
az login
bash scripts/deploy_aca.sh
```

The quick-start is an authenticated demo, not production deployment: it uses
ephemeral container storage for SQLite and asks for an explicit confirmation.
Do not use it for real RAID data. Configure durable storage and your
organization's approved registry, network, and identity policies first. Do not
put client secrets or access tokens in this repository or image. The Azure
deployment itself is not run automatically from this repository.

In Copilot Studio, add the deployed MCP endpoint as an MCP tool, configure
Microsoft Entra OAuth for the API audience and scope above, then switch off
**Allow all** and enable only the desired tools (for example
`list_raid_items`, `get_item`, `add_item`, and `close_item`). Keep
`delete_item` disabled; it is also blocked server-side.

Add an Outlook connector in the same Copilot Studio agent and complete its
tenant authorization. After testing both connections and restricting the
enabled tools, a prompt such as “Draft the weekly update from open high-
priority risks” can use the RAID log and Outlook together. The RAID schema
doesn't currently have a priority field, so define a priority convention or
extend the schema before relying on “high-priority” filtering.

## Safety controls

The server validates input types and length limits, blocks deletion, requires
reasons for add/close writes, records write reasons, and audits every tool call
without copying tool arguments into the call audit. Entra calls are attributed
by tenant and user object ID where available; API-key pilot calls are attributed
to the shared key, not an individual.

The repository agent policy is in
[.github/copilot-instructions.md](.github/copilot-instructions.md). Platform
deployment must also configure the Power Platform DLP policy, approved
connectors, and platform-enforced human approval before any send action. See
[docs/platform-controls.md](docs/platform-controls.md). Agent confirmation
instructions are not a substitute for platform approval enforcement.
