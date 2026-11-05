"""Exercise both protocol eras over real HTTP/1.1 sockets, without Fusion."""

import base64
import http.client
import json
import queue
import socket
import threading
import time
import unittest
from unittest.mock import patch

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge import dispatch, tool_surface
from lib.mcp_protocol import META_PREFIX, MODERN_PROTOCOL_VERSION, validate_arguments
from lib.mcp_server import MCPServer, LEGACY_PROTOCOL_VERSIONS, SUPPORTED_PROTOCOL_VERSIONS


def modern(method="tools/list", params=None, request_id=7, version=MODERN_PROTOCOL_VERSION):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {
        **(params or {}), "_meta": {
            META_PREFIX + "protocolVersion": version,
            META_PREFIX + "clientCapabilities": {},
        },
    }}
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json", "MCP-Protocol-Version": version,
        "Mcp-Method": method,
    }
    name = (params or {}).get("uri" if method == "resources/read" else "name")
    if name is not None:
        headers["Mcp-Name"] = name
    return message, headers


def sse_message(response):
    while True:
        line = response.readline()
        if not line:
            raise AssertionError("SSE ended before a message arrived")
        if line.startswith(b"data: "):
            result = json.loads(line[6:])
            if response.readline().strip():
                raise AssertionError("SSE event missing blank separator")
            return result


class HTTPFixture:
    def start_server(self, **kwargs):
        definitions = [{"name": "echo", "inputSchema": {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }}]
        defaults = {"port": 0, "tools": definitions, "tool_handlers": {"echo": self.echo}}
        self.server = MCPServer(**{**defaults, **kwargs})
        self.server.resources.append({"uri": "fusion://guide", "name": "Guide", "content_fn": lambda: "guide text"})
        self.server.start()
        self.addCleanup(self.server.stop)
        self.port = self.server.httpd.server_address[1]
        self.connection = self.connect()

    @staticmethod
    def echo(call):
        return {"content": [{"type": "text", "text": call["params"]["arguments"]["text"]}]}

    def connect(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(conn.close)
        return conn

    def send(self, message, headers, connection=None):
        connection = connection or self.connection
        connection.request("POST", "/mcp", json.dumps(message), headers)
        return connection.getresponse()

    def rpc(self, method="tools/list", params=None, **kwargs):
        return self.send(*modern(method, params, **kwargs))

    def assert_error(self, response, status, code):
        self.assertEqual(response.status, status)
        body = json.loads(response.read())
        self.assertEqual(body["error"]["code"], code)
        return body

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for server/dispatcher state")
            time.sleep(0.01)


class ProtocolHTTPTests(HTTPFixture, unittest.TestCase):
    def setUp(self):
        self.start_server()

    def test_discovery_without_handshake_or_session(self):
        message, headers = modern("server/discover")
        headers["Mcp-Session-Id"] = "ignored-modern-session"
        response = self.send(message, headers)
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader("Mcp-Session-Id"))
        result = json.loads(response.read())["result"]
        self.assertEqual(result["supportedVersions"], list(SUPPORTED_PROTOCOL_VERSIONS))
        self.assertEqual(result["capabilities"], {"tools": {}, "resources": {}})
        self.assertEqual(result["_meta"][META_PREFIX + "serverInfo"]["name"], "autodesk-fusion-mcp")
        self.assertEqual(self.server.sessions, {})

    def test_cacheable_results_and_resource_contents(self):
        for method, params, key in (
            ("server/discover", {}, "supportedVersions"),
            ("tools/list", {}, "tools"), ("resources/list", {}, "resources"),
            ("resources/templates/list", {}, "resourceTemplates"),
            ("resources/read", {"uri": "fusion://guide"}, "contents"),
        ):
            with self.subTest(method=method):
                response = self.rpc(method, params)
                self.assertEqual(response.status, 200)
                result = json.loads(response.read())["result"]
                self.assertEqual(result["resultType"], "complete")
                self.assertEqual((result["ttlMs"], result["cacheScope"]), (0, "private"))
                self.assertIn(key, result)
                if method == "resources/read":
                    self.assertEqual(result[key][0]["text"], "guide text")

    def test_tool_sse_terminates_and_connection_can_be_reused(self):
        response = self.rpc("tools/call", {"name": "echo", "arguments": {"text": "hello"}})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(response.getheader("X-Accel-Buffering"), "no")
        sock = self.connection.sock
        result = sse_message(response)["result"]
        self.assertEqual(result["content"][0]["text"], "hello")
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(response.read(), b"")
        response = self.rpc()
        self.assertEqual(response.status, 200)
        response.read()
        self.assertIs(self.connection.sock, sock)

    def test_all_legacy_lifecycles_interleave_with_modern_requests(self):
        for version in LEGACY_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                headers = {"Accept": "application/json, text/event-stream"}
                if version != "2025-03-26":
                    headers["MCP-Protocol-Version"] = version
                response = self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": version, "clientInfo": {"name": "legacy", "version": "1"}, "capabilities": {},
                }}, headers)
                session = response.getheader("Mcp-Session-Id")
                result = json.loads(response.read())["result"]
                self.assertEqual(result["protocolVersion"], version)
                self.assertEqual(result["capabilities"], {"tools": {"listChanged": True}, "resources": {}})
                self.assertNotIn("resultType", result)
                headers["Mcp-Session-Id"] = session
                response = self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
                self.assertEqual((response.status, response.read()), (202, b""))
                for method in ("tools/list", "resources/list", "ping"):
                    response = self.send({"jsonrpc": "2.0", "id": 7, "method": method}, headers)
                    result = json.loads(response.read())["result"]
                    self.assertNotIn("resultType", result)
                    self.assertNotIn("ttlMs", result)
                    response = self.rpc("server/discover")
                    self.assertEqual(json.loads(response.read())["result"]["resultType"], "complete")
                response = self.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
                    "name": "echo", "arguments": {"text": version},
                }}, headers)
                result = sse_message(response)["result"]
                self.assertEqual(result["content"][0]["text"], version)
                self.assertNotIn("resultType", result)
                self.assertEqual(response.read(), b"")
                self.connection.request("DELETE", "/mcp", headers=headers)
                response = self.connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, b""))
                self.assertNotIn(session, self.server.sessions)

    def test_initialize_never_negotiates_stateless_version(self):
        message, headers = modern("initialize")
        message["params"] = {"protocolVersion": MODERN_PROTOCOL_VERSION}
        response = self.send(message, headers)
        self.assertEqual(json.loads(response.read())["result"]["protocolVersion"], "2025-11-25")

    def test_legacy_get_retained_modern_get_and_delete_rejected(self):
        self.server.sessions["legacy"] = {}
        for method in ("GET", "DELETE"):
            self.connection.request(method, "/mcp", headers={
                "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION, "Mcp-Session-Id": "legacy",
            })
            response = self.connection.getresponse()
            self.assertEqual((response.status, response.read()), (405, b""))
            self.assertEqual(response.getheader("Allow"), "POST")
            self.assertIn("legacy", self.server.sessions)
        self.connection.request("GET", "/mcp", headers={"Accept": "text/event-stream", "Mcp-Session-Id": "legacy"})
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.readline(), b"event: message\n")
        self.assertEqual(json.loads(response.readline()[6:]),
                         {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        self.assertEqual(response.readline(), b"\n")
        self.assertEqual(response.readline(), b": keepalive\n")
        response.close()

    def test_every_new_legacy_stream_announces_tools_list_changed(self):
        # Once per stream, including after a restart on the same port: the
        # server cannot know what tool list a reconnecting client cached.
        for _ in range(2):
            conn = self.connect()
            conn.request("GET", "/mcp", headers={"Accept": "text/event-stream"})
            response = conn.getresponse()
            self.assertEqual(sse_message(response)["method"], "notifications/tools/list_changed")
            response.close()
        port = self.port
        self.server.stop()
        self.start_server(port=port)
        conn = self.connect()
        conn.request("GET", "/mcp", headers={"Accept": "text/event-stream"})
        response = conn.getresponse()
        self.assertEqual(sse_message(response)["method"], "notifications/tools/list_changed")
        response.close()

    def test_modern_discovery_advertises_no_list_changed(self):
        result = json.loads(self.rpc("server/discover").read())["result"]
        self.assertEqual(result["capabilities"]["tools"], {})

    def test_header_validation_and_metadata_cannot_downgrade(self):
        for header in ("MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"):
            for replacement in (None, "2025-11-25", "wrong"):
                with self.subTest(header=header, value=replacement):
                    message, headers = modern("tools/call", {"name": "echo"})
                    if replacement is None:
                        del headers[header]
                    else:
                        headers[header] = replacement
                    self.assert_error(self.send(message, headers), 400, -32020)

    def test_missing_or_invalid_metadata(self):
        for meta in (None, {}, {META_PREFIX + "protocolVersion": MODERN_PROTOCOL_VERSION},
                     {META_PREFIX + "protocolVersion": MODERN_PROTOCOL_VERSION, META_PREFIX + "clientCapabilities": []}):
            message, headers = modern()
            message["params"]["_meta"] = meta
            self.assert_error(self.send(message, headers), 400, -32602)

    def test_header_names_are_case_insensitive_but_values_are_not(self):
        message, headers = modern()
        response = self.send(message, {k.lower(): v for k, v in headers.items()})
        self.assertEqual(response.status, 200)
        response.read()
        headers["Mcp-Method"] = "TOOLS/LIST"
        self.assert_error(self.send(message, headers), 400, -32020)

    def test_duplicate_header_is_rejected(self):
        message, headers = modern()
        body = json.dumps(message)
        self.connection.putrequest("POST", "/mcp")
        for k, v in headers.items():
            self.connection.putheader(k, v)
        self.connection.putheader("mcp-method", "tools/call")
        self.connection.putheader("Content-Length", str(len(body)))
        self.connection.endheaders(body.encode())
        self.assert_error(self.connection.getresponse(), 400, -32020)

    def test_base64_resource_names_and_literal_sentinel(self):
        for uri in ("fusion://café", "=?base64?literal?="):
            with self.subTest(uri=uri):
                self.server.resources.append({"uri": uri, "name": uri, "content_fn": lambda: "ok"})
                message, headers = modern("resources/read", {"uri": uri})
                headers["Mcp-Name"] = "=?base64?" + base64.b64encode(uri.encode()).decode() + "?="
                response = self.send(message, headers)
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["result"]["contents"][0]["uri"], uri)
        headers["Mcp-Name"] = "=?base64?@@@?="
        self.assert_error(self.send(message, headers), 400, -32020)

    def test_unsupported_version_advertises_recovery_version(self):
        response = self.rpc(version="2099-01-01")
        body = self.assert_error(response, 400, -32022)
        self.assertEqual(body["error"]["data"]["requested"], "2099-01-01")
        self.assertIn(MODERN_PROTOCOL_VERSION, body["error"]["data"]["supported"])

    def test_removed_and_unknown_methods_have_protocol_errors(self):
        for method, params, status, code in (
            ("initialize", {}, 404, -32601), ("ping", {}, 404, -32601),
            ("unknown", {}, 404, -32601), ("tools/call", {"name": "missing"}, 400, -32602),
            ("resources/read", {"uri": "fusion://missing"}, 400, -32602),
            ("tools/list", {"cursor": "unknown"}, 400, -32602),
        ):
            with self.subTest(method=method):
                self.assert_error(self.rpc(method, params), status, code)

    def test_modern_batches_responses_and_notifications_rejected(self):
        message, headers = modern()
        for body in ([message], [], None, "string", {"jsonrpc": "2.0", "id": 7, "result": {}},
                     {"jsonrpc": "2.0", "method": "notifications/cancelled"}):
            with self.subTest(body=body):
                self.assert_error(self.send(body, headers), 400, -32600)

    def test_invalid_request_id_omitted_in_error(self):
        for request_id in (None, False, 3.5, {}, []):
            body = self.assert_error(self.rpc(request_id=request_id), 400, -32600)
            self.assertNotIn("id", body)

    def test_malformed_json_omits_modern_error_id_and_preserves_legacy_error_shape(self):
        for version in (MODERN_PROTOCOL_VERSION, "2025-11-25"):
            for body in ('{"jsonrpc":', '{"id": NaN}', b'\xff'):
                headers = modern(version=version)[1]
                self.connection.request("POST", "/mcp", body, headers)
                error = self.assert_error(self.connection.getresponse(), 400, -32700)
                self.assertEqual("id" in error, version != MODERN_PROTOCOL_VERSION)

    def test_zero_and_string_ids_are_echoed(self):
        for request_id in (0, "", "some-id"):
            response = self.rpc(request_id=request_id)
            self.assertEqual(json.loads(response.read())["id"], request_id)

    def test_invalid_tool_input_returns_tool_error_without_execution(self):
        calls = []
        self.server.tool_handlers["echo"] = calls.append
        for args in ({}, {"text": 17}):
            response = self.rpc("tools/call", {"name": "echo", "arguments": args})
            self.assertEqual(response.status, 200)
            result = json.loads(response.read())["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(result["resultType"], "complete")
        self.assertEqual(calls, [])
        self.assert_error(self.rpc("tools/call", {"name": "echo", "arguments": []}), 400, -32602)

    def test_handler_exception_produces_complete_tool_error(self):
        def fail(call):
            raise RuntimeError("test failure")
        self.server.tool_handlers["echo"] = fail
        response = self.rpc("tools/call", {"name": "echo", "arguments": {"text": "x"}})
        result = sse_message(response)["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(response.read(), b"")

    def test_bad_result_does_not_write_second_http_response_inside_sse(self):
        self.server.tool_handlers["echo"] = lambda call: None
        response = self.rpc("tools/call", {"name": "echo", "arguments": {"text": "x"}})
        self.assertEqual(sse_message(response)["error"]["code"], -32603)
        self.assertEqual(response.read(), b"")

    def test_origin_validation_for_both_eras(self):
        for method, path in (("GET", "/health"), ("GET", "/mcp"), ("POST", "/mcp"), ("DELETE", "/mcp")):
            conn = self.connect()
            conn.request(method, path, headers={"Origin": "https://attacker.example"})
            response = conn.getresponse()
            self.assertEqual((response.status, response.read()), (403, b""))
        for origin in (f"http://localhost:{self.port}", f"http://127.0.0.1:{self.port}"):
            self.connection.request("GET", "/health", headers={"Origin": origin})
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()

    def test_subscription_acknowledges_supported_subset_and_stops_gracefully(self):
        response = self.rpc("subscriptions/listen", {"notifications": {"toolsListChanged": True}})
        self.assertEqual(response.status, 200)
        notice = sse_message(response)
        self.assertEqual(notice["method"], "notifications/subscriptions/acknowledged")
        self.assertEqual(notice["params"]["notifications"], {})
        self.assertEqual(notice["params"]["_meta"][META_PREFIX + "subscriptionId"], 7)
        self.server.stop()
        result = sse_message(response)["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["_meta"][META_PREFIX + "subscriptionId"], 7)
        self.assertEqual(response.read(), b"")

    def test_modern_requires_both_accept_types(self):
        for accept in ("application/json", "text/event-stream", "*/*"):
            message, headers = modern()
            headers["Accept"] = accept
            self.assert_error(self.send(message, headers), 406, -32600)

    def test_explicit_python_state_for_modern_only(self):
        tool = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "execute_python")
        self.server.tools.append(tool)
        calls = []
        def run(call):
            calls.append(call)
            return {"content": []}
        self.server.tool_handlers["execute_python"] = run
        args = {"code": "x = 1", "description": "test"}
        response = self.rpc("tools/call", {"name": "execute_python", "arguments": args})
        self.assertTrue(json.loads(response.read())["result"]["isError"])
        self.assertEqual(calls, [])
        for extra in ({"session_id": "explicit-handle"}, {"persistent": False}):
            response = self.rpc("tools/call", {"name": "execute_python", "arguments": {**args, **extra}})
            self.assertNotIn("isError", sse_message(response)["result"])
            response.read()
        response = self.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "execute_python", "arguments": args,
        }}, {"Accept": "application/json, text/event-stream"})
        self.assertNotIn("isError", sse_message(response)["result"])
        response.read()
        self.assertEqual(len(calls), 3)


class CancellationHTTPTests(HTTPFixture, unittest.TestCase):
    def setUp(self):
        self.queue = queue.Queue()
        self.halt = threading.Event()
        for name, value in (("_pending", self.queue), ("_halt", self.halt),
                            ("_deferred_messages", []), ("_callback_impl", self.echo)):
            patcher = patch.object(dispatch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app = type("App", (), {"fireCustomEvent": lambda *args: None})()
        patcher = patch.object(dispatch, "get_app", return_value=app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start_server(tool_handlers={"echo": dispatch.dispatch_to_main_thread}, max_tool_requests=2)

    def open_tool(self, text):
        conn = self.connect()
        response = self.send(*modern("tools/call", {"name": "echo", "arguments": {"text": text}}), connection=conn)
        self.assertEqual(response.status, 200)
        return conn, response

    @staticmethod
    def disconnect(conn, response):
        conn.sock.shutdown(socket.SHUT_RDWR)
        response.close()
        conn.close()

    def test_disconnect_cancels_own_queued_work_with_duplicate_ids(self):
        conn_a, response_a = self.open_tool("A")
        conn_b, response_b = self.open_tool("B")
        self.wait_for(lambda: self.queue.qsize() == 2)
        self.disconnect(conn_a, response_a)
        self.wait_for(lambda: self.queue.qsize() == 1)
        executed = []
        dispatch._callback_impl = lambda call: (executed.append(call["params"]["arguments"]["text"]) or self.echo(call))
        dispatch._flush_pending()
        result = sse_message(response_b)["result"]
        self.assertEqual(result["content"][0]["text"], "B")
        self.assertEqual(executed, ["B"])
        self.assertEqual(response_b.read(), b"")

    def test_cancelled_envelope_never_executes_even_before_worker_removes_it(self):
        conn, response = self.open_tool("cancelled")
        self.wait_for(lambda: self.queue.qsize() == 1)
        with self.queue.mutex:
            envelope = self.queue.queue[0]
            envelope["payload"]["_cancel_event"].set()
        called = []
        dispatch._callback_impl = called.append
        dispatch._flush_pending()
        self.assertEqual(called, [])
        self.disconnect(conn, response)

    def test_running_work_can_finish_after_disconnect(self):
        started = threading.Event()
        finish = threading.Event()
        completed = threading.Event()
        self.addCleanup(finish.set)
        def run(call):
            started.set()
            finish.wait(3)
            completed.set()
            return self.echo(call)
        dispatch._callback_impl = run
        conn, response = self.open_tool("running")
        self.wait_for(lambda: self.queue.qsize() == 1)
        with self.queue.mutex:
            cancel_event = self.queue.queue[0]["payload"]["_cancel_event"]
        # Simulate the Fusion event pump on a dedicated thread so the test can
        # close the socket while the callback is executing.
        pump = threading.Thread(target=dispatch._flush_pending)
        pump.start()
        self.assertTrue(started.wait(2))
        self.disconnect(conn, response)
        self.assertTrue(cancel_event.wait(2))
        finish.set()
        pump.join(2)
        self.assertTrue(completed.is_set())
        self.assertFalse(pump.is_alive())

    def test_timeout_removes_queued_mutation_and_warns_against_blind_retry(self):
        self.server.tool_timeout = 0.1
        conn, response = self.open_tool("timed out")
        result = sse_message(response)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Do NOT blindly retry", result["content"][0]["text"])
        self.assertEqual(response.read(), b"")
        self.wait_for(self.queue.empty)
        called = []
        dispatch._callback_impl = called.append
        dispatch._flush_pending()
        self.assertEqual(called, [])

    def test_shutdown_cancels_queued_work(self):
        conn, response = self.open_tool("stopped")
        self.wait_for(lambda: self.queue.qsize() == 1)
        self.server.stop()
        self.wait_for(self.queue.empty)
        response.close()

    def test_concurrency_limit_rejects_before_dispatch(self):
        conn_a, response_a = self.open_tool("A")
        conn_b, response_b = self.open_tool("B")
        self.wait_for(lambda: self.queue.qsize() == 2)
        self.assert_error(self.rpc("tools/call", {"name": "echo", "arguments": {"text": "C"}}), 429, -32603)
        self.assertEqual(self.queue.qsize(), 2)
        self.disconnect(conn_a, response_a)
        self.disconnect(conn_b, response_b)
        self.wait_for(self.queue.empty)


class OwnedSchemaTests(unittest.TestCase):
    def test_all_owned_schema_keywords_are_supported(self):
        supported = {"type", "description", "properties", "required", "items", "enum", "minimum", "maximum", "additionalProperties"}
        def visit(schema):
            self.assertLessEqual(schema.keys(), supported)
            for child in schema.get("properties", {}).values():
                visit(child)
            if "items" in schema:
                visit(schema["items"])
        for tool in tool_surface.TOOL_DEFINITIONS:
            with self.subTest(tool=tool["name"]):
                visit(tool["inputSchema"])

    def test_nested_schema_and_json_number_semantics(self):
        schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "integer", "minimum": 1}}}}
        self.assertIsNone(validate_arguments({"items": [1, 2.0]}, schema))
        for value in (True, 0, "2", 1.5):
            self.assertIsNotNone(validate_arguments({"items": [value]}, schema))
        self.assertIsNone(validate_arguments(10 ** 500, {"type": "number"}))


if __name__ == "__main__":
    unittest.main()
