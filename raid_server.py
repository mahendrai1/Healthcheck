"""MCP server for a small, locally stored RAID log."""

import asyncio
import hmac
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

import jwt
from pydantic import Field
from jwt import PyJWKClient
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

RaidType = Literal["risk", "assumption", "issue", "dependency"]
RaidStatus = Literal["open", "closed"]

DB_PATH = Path(os.environ.get("RAID_DB_PATH", "raid_log.sqlite3"))
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 4000
MAX_OWNER_LENGTH = 120
MAX_REASON_LENGTH = 1000


class EntraTokenVerifier:
    def __init__(self, tenant_id: str, audience: str, required_scope: str):
        self.issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self.audience = audience
        self.required_scope = required_scope
        self.jwks = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await asyncio.to_thread(
                self.jwks.get_signing_key_from_jwt, token
            )
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
            )
        except jwt.PyJWTError:
            return None

        scopes = claims.get("scp", "").split()
        if self.required_scope not in scopes:
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("azp", claims.get("appid", "")),
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=self.audience,
            subject=(
                f"{claims.get('tid', 'unknown')}:"
                f"{claims.get('oid', claims.get('sub', 'unknown'))}"
            ),
            claims=claims,
        )


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
        self.api_key = api_key.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            api_key_headers = [
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"x-api-key"
            ]
            supplied = api_key_headers[0] if len(api_key_headers) == 1 else b""
            if not hmac.compare_digest(supplied, self.api_key):
                response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class AuditedFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        access_token = get_access_token()
        if access_token and access_token.subject:
            actor = access_token.subject
        elif os.environ.get("MCP_TRANSPORT") == "streamable-http":
            actor = "api-key"
        else:
            actor = "local"
        call_id = audit_tool_call(name, actor, "started")
        try:
            result = await super().call_tool(name, arguments)
        except Exception as error:
            audit_tool_call(name, actor, "error", call_id, type(error).__name__)
            raise
        audit_tool_call(name, actor, "success", call_id=call_id)
        return result


def create_server() -> AuditedFastMCP:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        return AuditedFastMCP("raid-log")
    if transport != "streamable-http":
        raise ValueError("MCP_TRANSPORT must be stdio or streamable-http")

    auth_mode = os.environ.get("MCP_AUTH_MODE", "entra")
    if auth_mode == "api-key":
        api_key = os.environ.get("MCP_API_KEY", "")
        if len(api_key) < 32:
            raise RuntimeError("MCP_API_KEY must contain at least 32 characters")
        return AuditedFastMCP(
            "raid-log",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8000")),
        )
    if auth_mode != "entra":
        raise ValueError("MCP_AUTH_MODE must be api-key or entra")

    tenant_id = os.environ.get("MCP_TENANT_ID")
    audience = os.environ.get("MCP_AUDIENCE")
    required_scope = os.environ.get("MCP_REQUIRED_SCOPE")
    resource_url = os.environ.get("MCP_RESOURCE_URL")
    if not all((tenant_id, audience, required_scope, resource_url)):
        raise RuntimeError(
            "HTTP transport requires MCP_TENANT_ID, MCP_AUDIENCE, "
            "MCP_REQUIRED_SCOPE, and MCP_RESOURCE_URL for Entra ID authentication"
        )

    issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    return AuditedFastMCP(
        "raid-log",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        token_verifier=EntraTokenVerifier(tenant_id, audience, required_scope),
        auth=AuthSettings(
            issuer_url=issuer,
            resource_server_url=resource_url,
            validate_token_resource=False,
            required_scopes=[required_scope],
        ),
    )


mcp = create_server()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS raid_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK (type IN ('risk', 'assumption', 'issue', 'dependency')),
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                owner TEXT,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS tool_call_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                actor TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('started', 'success', 'error')),
                error_type TEXT,
                started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                finished_at TEXT
            );
            """
        )


def audit_tool_call(
    tool_name: str,
    actor: str,
    outcome: Literal["started", "success", "error"],
    call_id: int | None = None,
    error_type: str | None = None,
) -> int:
    with connect() as connection:
        if call_id is None:
            cursor = connection.execute(
                "INSERT INTO tool_call_audit (tool_name, actor, outcome) VALUES (?, ?, ?)",
                (tool_name, actor, outcome),
            )
            return int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE tool_call_audit
            SET outcome = ?, error_type = ?,
                finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (outcome, error_type, call_id),
        )
    return call_id


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def validate_item_id(id: int) -> None:
    if isinstance(id, bool) or not isinstance(id, int) or id <= 0:
        raise ValueError("id must be a positive integer")


@mcp.tool()
def list_raid_items(
    type: Annotated[RaidType | None, Field(description="Filter by RAID type.")] = None,
    status: Annotated[RaidStatus | None, Field(description="Filter by open or closed status.")] = None,
    owner: Annotated[
        str | None, Field(description="Filter by exact owner name.", max_length=MAX_OWNER_LENGTH)
    ] = None,
) -> list[dict]:
    """List RAID log items, optionally filtering by type, status, and exact owner."""
    if type is not None and type not in ("risk", "assumption", "issue", "dependency"):
        raise ValueError("type must be risk, assumption, issue, or dependency")
    if status is not None and status not in ("open", "closed"):
        raise ValueError("status must be open or closed")
    if owner is not None and not isinstance(owner, str):
        raise ValueError("owner must be text or null")
    if owner is not None and len(owner) > MAX_OWNER_LENGTH:
        raise ValueError(f"owner must be at most {MAX_OWNER_LENGTH} characters")
    clauses = []
    values = []
    if type is not None:
        clauses.append("type = ?")
        values.append(type)
    if status is not None:
        clauses.append("status = ?")
        values.append(status)
    if owner is not None:
        clauses.append("owner = ?")
        values.append(owner)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM raid_items{where} ORDER BY id", values
        ).fetchall()
    return [row_to_dict(row) for row in rows]


@mcp.tool()
def get_item(
    id: Annotated[int, Field(description="Positive numeric ID of the RAID item.")],
) -> dict:
    """Get one RAID item by its numeric ID; reports an error if it does not exist."""
    validate_item_id(id)
    with connect() as connection:
        row = connection.execute("SELECT * FROM raid_items WHERE id = ?", (id,)).fetchone()
    if row is None:
        raise ValueError(f"RAID item {id} was not found")
    return row_to_dict(row)


@mcp.tool()
def add_item(
    type: Annotated[
        RaidType, Field(description="RAID type: risk, assumption, issue, or dependency.")
    ],
    title: Annotated[
        str, Field(description="Short, non-empty summary.", min_length=1, max_length=MAX_TITLE_LENGTH)
    ],
    description: Annotated[
        str,
        Field(
            description="Non-empty details for the RAID item.",
            min_length=1,
            max_length=MAX_DESCRIPTION_LENGTH,
        ),
    ],
    reason: Annotated[
        str,
        Field(
            description="Required justification for adding this item.",
            min_length=1,
            max_length=MAX_REASON_LENGTH,
        ),
    ],
    owner: Annotated[
        str | None, Field(description="Optional owner name.", max_length=MAX_OWNER_LENGTH)
    ] = None,
) -> dict:
    """Add an open RAID item after validating inputs and recording a required reason."""
    if not isinstance(type, str) or type not in ("risk", "assumption", "issue", "dependency"):
        raise ValueError("type must be risk, assumption, issue, or dependency")
    if not isinstance(title, str):
        raise ValueError("title must be text")
    if not isinstance(description, str):
        raise ValueError("description must be text")
    if not isinstance(reason, str):
        raise ValueError("reason must be text")
    if owner is not None and not isinstance(owner, str):
        raise ValueError("owner must be text or null")
    title = title.strip()
    description = description.strip()
    reason = reason.strip()
    owner = owner.strip() if owner is not None else None
    if not title:
        raise ValueError("title must not be empty")
    if not description:
        raise ValueError("description must not be empty")
    if not reason:
        raise ValueError("reason must not be empty")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(f"title must be at most {MAX_TITLE_LENGTH} characters")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"description must be at most {MAX_DESCRIPTION_LENGTH} characters")
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(f"reason must be at most {MAX_REASON_LENGTH} characters")
    if owner is not None and len(owner) > MAX_OWNER_LENGTH:
        raise ValueError(f"owner must be at most {MAX_OWNER_LENGTH} characters")
    if owner == "":
        owner = None

    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO raid_items (type, title, description, owner) VALUES (?, ?, ?, ?)",
            (type, title, description, owner),
        )
        connection.execute(
            "INSERT INTO audit_events (item_id, action, reason) VALUES (?, 'add', ?)",
            (cursor.lastrowid, reason),
        )
        row = connection.execute(
            "SELECT * FROM raid_items WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return row_to_dict(row)


@mcp.tool()
def close_item(
    id: Annotated[int, Field(description="Positive numeric ID of the open item.")],
    reason: Annotated[
        str,
        Field(description="Required explanation for closing the item.", min_length=1, max_length=MAX_REASON_LENGTH),
    ],
) -> dict:
    """Close an open item with a required reason and record the action in the audit trail."""
    validate_item_id(id)
    if not isinstance(reason, str):
        raise ValueError("reason must be text")
    reason = reason.strip()
    if not reason:
        raise ValueError("reason must not be empty")
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(f"reason must be at most {MAX_REASON_LENGTH} characters")

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM raid_items WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise ValueError(f"RAID item {id} was not found")
        if row["status"] == "closed":
            raise ValueError(f"RAID item {id} is already closed")
        connection.execute(
            """
            UPDATE raid_items
            SET status = 'closed', closed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (id,),
        )
        connection.execute(
            "INSERT INTO audit_events (item_id, action, reason) VALUES (?, 'close', ?)",
            (id, reason),
        )
        updated = connection.execute(
            "SELECT * FROM raid_items WHERE id = ?", (id,)
        ).fetchone()
    return row_to_dict(updated)


@mcp.tool()
def delete_item(id: Annotated[int, Field(description="ID of the item; deletion is blocked.")]) -> dict:
    """Deletion is intentionally blocked to prevent irreversible loss of RAID history."""
    validate_item_id(id)
    raise ValueError(
        "Deletion is disabled to preserve the RAID log and its audit history; "
        "use close_item with a reason instead."
    )


def main() -> None:
    init_db()
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http" and os.environ.get("MCP_AUTH_MODE", "entra") == "api-key":
        import uvicorn

        app = ApiKeyMiddleware(mcp.streamable_http_app(), os.environ["MCP_API_KEY"])
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8000")),
            log_level="info",
        )
        return
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
