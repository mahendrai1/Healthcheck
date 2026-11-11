import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import raid_server
from mcp.server.fastmcp.exceptions import ToolError


class RaidServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid.sqlite3"
        self.db_path_patch = patch.object(raid_server, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        raid_server.init_db()

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_http_transport_requires_entra_configuration(self):
        with patch.dict("os.environ", {"MCP_TRANSPORT": "streamable-http"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "requires MCP_TENANT_ID"):
                raid_server.create_server()

    def test_http_transport_configures_required_scope_and_audience(self):
        configuration = {
            "MCP_TRANSPORT": "streamable-http",
            "MCP_TENANT_ID": "tenant-id",
            "MCP_AUDIENCE": "api://raid-log",
            "MCP_REQUIRED_SCOPE": "raid.access",
            "MCP_RESOURCE_URL": "https://raid.example/mcp",
        }
        with patch.dict("os.environ", configuration, clear=True):
            server = raid_server.create_server()
        self.assertEqual(server.settings.auth.required_scopes, ["raid.access"])
        self.assertEqual(
            str(server.settings.auth.resource_server_url), "https://raid.example/mcp"
        )

    def test_api_key_mode_requires_a_long_key(self):
        configuration = {
            "MCP_TRANSPORT": "streamable-http",
            "MCP_AUTH_MODE": "api-key",
            "MCP_API_KEY": "too-short",
        }
        with patch.dict("os.environ", configuration, clear=True):
            with self.assertRaisesRegex(RuntimeError, "at least 32 characters"):
                raid_server.create_server()

    def test_api_key_middleware_rejects_missing_key_and_accepts_matching_key(self):
        called = []

        async def app(scope, receive, send):
            called.append(True)

        middleware = raid_server.ApiKeyMiddleware(app, "x" * 40)
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        async def exercise(headers):
            await middleware(
                {"type": "http", "method": "POST", "path": "/mcp", "headers": headers},
                receive,
                send,
            )

        asyncio.run(exercise([]))
        self.assertEqual(messages[0]["status"], 401)
        self.assertEqual(called, [])
        asyncio.run(exercise([(b"x-api-key", b"x" * 40)]))
        self.assertEqual(len(called), 1)

    def test_add_get_and_filter_items(self):
        first = raid_server.add_item(
            "risk", "  Delivery  ", "  Supplier may be late.  ", "Change control approved.", "  Alex  "
        )
        raid_server.add_item("issue", "Outage", "Service is unavailable", "Incident logged.", "Sam")

        self.assertEqual(first["title"], "Delivery")
        self.assertEqual(first["description"], "Supplier may be late.")
        self.assertEqual(first["owner"], "Alex")
        self.assertEqual(raid_server.get_item(first["id"]), first)
        with sqlite3.connect(self.db_path) as connection:
            audit_reason = connection.execute(
                "SELECT action, reason FROM audit_events WHERE item_id = ?",
                (first["id"],),
            ).fetchone()
        self.assertEqual(audit_reason, ("add", "Change control approved."))
        self.assertEqual(
            [item["title"] for item in raid_server.list_raid_items(type="risk", owner="Alex")],
            ["Delivery"],
        )

    def test_missing_item_and_invalid_add_are_reported(self):
        with self.assertRaisesRegex(ValueError, "was not found"):
            raid_server.get_item(999)
        with self.assertRaisesRegex(ValueError, "title must not be empty"):
            raid_server.add_item("risk", "  ", "Description", "Required justification")
        with self.assertRaisesRegex(ValueError, "type must be"):
            raid_server.add_item("other", "Title", "Description", "Required justification")
        with self.assertRaisesRegex(ValueError, "reason must not be empty"):
            raid_server.add_item("risk", "Title", "Description", "  ")
        with self.assertRaisesRegex(ValueError, "at most 200 characters"):
            raid_server.add_item("risk", "T" * 201, "Description", "Required justification")

    def test_close_requires_reason_and_records_audit_event(self):
        item = raid_server.add_item("issue", "Broken build", "Build is failing", "Incident logged.")
        with self.assertRaisesRegex(ValueError, "reason must not be empty"):
            raid_server.close_item(item["id"], "  ")

        closed = raid_server.close_item(item["id"], "Fixed and verified")
        self.assertEqual(closed["status"], "closed")
        self.assertIsNotNone(closed["closed_at"])
        with sqlite3.connect(self.db_path) as connection:
            audit = connection.execute(
                "SELECT item_id, action, reason FROM audit_events WHERE action = 'close'"
            ).fetchone()
        self.assertEqual(audit, (item["id"], "close", "Fixed and verified"))
        with self.assertRaisesRegex(ValueError, "already closed"):
            raid_server.close_item(item["id"], "Again")

    def test_delete_is_blocked_without_changing_item(self):
        item = raid_server.add_item("risk", "Keep history", "Do not remove", "Preserve history.")
        with self.assertRaisesRegex(ValueError, "Deletion is disabled"):
            raid_server.delete_item(item["id"])
        self.assertEqual(raid_server.get_item(item["id"]), item)

    def test_every_tool_call_is_audited_without_storing_arguments(self):
        asyncio.run(
            raid_server.mcp.call_tool(
                "add_item",
                {
                    "type": "risk",
                    "title": "Audit me",
                    "description": "Details",
                    "reason": "Keep the audit trail.",
                },
            )
        )
        with self.assertRaises(ToolError):
            asyncio.run(raid_server.mcp.call_tool("get_item", {"id": 999}))
        with self.assertRaises(ToolError):
            asyncio.run(raid_server.mcp.call_tool("delete_item", {"id": 1}))
        with self.assertRaises(ToolError):
            asyncio.run(raid_server.mcp.call_tool("add_item", {"type": "risk"}))

        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT tool_name, outcome, error_type FROM tool_call_audit ORDER BY id"
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("add_item", "success", None),
                ("get_item", "error", "ToolError"),
                ("delete_item", "error", "ToolError"),
                ("add_item", "error", "ToolError"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
