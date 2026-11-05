"""Stateless HTTP request handling, alongside the legacy MCP transport.

This mixin uses the server's existing tool/resource handlers. All cancellation
tokens and metadata belong to a single POST, including on reused connections.
"""

import json
import selectors
import socket
import threading
import time

from .mcp_protocol import (
    META_PREFIX,
    SUPPORTED_PROTOCOL_VERSIONS,
    ProtocolError,
    complete_result,
    validate_arguments,
    validate_modern_request,
)


class ModernHTTPMixin:
    def _validate_origin(self):
        origins = self.headers.get_all("Origin", [])
        if not origins:
            return True  # Native clients do not send Origin.
        allowed = self.mcp_server.allowed_origins
        port = self.server.server_address[1]
        local = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
        if len(origins) == 1 and origins[0] in local | allowed:
            return True
        self._send_empty(403, close=True)
        return False

    def _send_empty(self, status, *, close=False, allow=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        if allow:
            self.send_header("Allow", allow)
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()

    def _handle_modern_request(self, message):
        request_id = message.get("id") if isinstance(message, dict) else None
        try:
            params = validate_modern_request(message, self.headers)
            accept = self.headers.get("Accept", "")
            media_types = {part.split(";", 1)[0].strip() for part in accept.split(",")}
            if not {"application/json", "text/event-stream"} <= media_types:
                raise ProtocolError(-32600, "Accept must include JSON and event-stream", status=406)
            method = message["method"]
            version = params["_meta"][META_PREFIX + "protocolVersion"]
            self.mcp_server.log(f"Stateless MCP {version}: {method!r} (id={request_id!r})")
            if method == "tools/call":
                self._handle_modern_tool(message, params)
                return
            if method == "subscriptions/listen":
                self._handle_subscription(message, params)
                return
            if method in ("tools/list", "resources/list", "resources/templates/list"):
                cursor = params.get("cursor")
                if cursor is not None:
                    raise ProtocolError(-32602, "Invalid cursor: this server returns one page")
            if method == "server/discover":
                result = {
                    "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "capabilities": {"tools": {}, "resources": {}},
                }
            elif method == "tools/list":
                result = self._handle_tools_list(params)
            elif method == "resources/list":
                result = self._handle_resources_list(params)
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "resources/read":
                if not any(r["uri"] == params["uri"] for r in self.mcp_server.resources):
                    raise ProtocolError(-32602, "Resource not found")
                result = self._handle_resources_read(params)
            else:
                raise ProtocolError(-32601, f"Method not found: {method}", status=404)
            self._send_json_response(200, self._modern_response(message, result))
        except ProtocolError as exc:
            self._send_json_response(exc.status, exc.response(request_id))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            self.mcp_server.log(f"Modern request failed: {exc}")
            error = ProtocolError(-32603, "Internal server error", status=500)
            if getattr(self, "_chunked_sse", False):
                # Headers already went out: the error belongs on this stream.
                try:
                    self._write_sse_event(json.dumps(error.response(request_id)), event="message")
                    self._end_sse()
                except OSError:
                    self.close_connection = True
            else:
                self._send_json_response(error.status, error.response(request_id))

    def _modern_response(self, message, result):
        return {
            "jsonrpc": "2.0", "id": message["id"],
            "result": complete_result(result, message["method"], self.mcp_server.server_info),
        }

    def _handle_modern_tool(self, message, params):
        server = self.mcp_server
        name = params["name"]
        handler = server.tool_handlers.get(name)
        tool = next((t for t in server.tools if t["name"] == name), None)
        if handler is None or tool is None:
            raise ProtocolError(-32602, f"Unknown tool: {name}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ProtocolError(-32602, "Tool arguments must be an object")
        validation_error = validate_arguments(arguments, tool["inputSchema"])
        python_call = name == "execute_python" or (
            name == "call_autodesk_api" and arguments.get("operation") == "execute_python"
        )
        if python_call and arguments.get("persistent", True):
            if not isinstance(arguments.get("session_id"), str) or not arguments["session_id"]:
                validation_error = (
                    "Persistent Python calls require an explicit session_id. "
                    "Choose a unique identifier and pass it on related calls, "
                    "or set persistent=false for an independent execution."
                )
        if validation_error:
            self._send_json_response(200, self._modern_response(message, self._tool_error(validation_error)))
            return
        if not server._tool_slots.acquire(blocking=False):
            raise ProtocolError(-32603, "Too many in-flight tool requests; wait for one to finish", status=429)

        cancelled = threading.Event()
        done = threading.Event()
        outcome = []

        def run():
            try:
                if not cancelled.is_set():
                    result = handler({
                        "params": {"name": name, "arguments": arguments},
                        "_cancel_event": cancelled,
                    })
                    outcome.append(result)
            except Exception as exc:
                server.log(f"Tool {name} failed: {exc}")
                outcome.append(self._tool_error(f"Tool execution failed: {exc}"))
            finally:
                server._tool_slots.release()
                done.set()

        try:
            self._start_sse()
            thread = threading.Thread(target=run, name="MCP-tool", daemon=True)
            thread.start()
        except Exception:
            server._tool_slots.release()
            raise

        started = time.monotonic()
        last_keepalive = started
        try:
            while not done.wait(0.05):
                if self._client_disconnected() or server._stop_event.is_set():
                    cancelled.set()
                    self.close_connection = True
                    return
                if time.monotonic() - started >= server.tool_timeout:
                    cancelled.set()
                    result = self._tool_error(
                        "Timeout: stopped waiting for the tool. Work that already "
                        "started may still complete. Do NOT blindly retry; inspect "
                        "the design first to avoid duplicate changes."
                    )
                    break
                if time.monotonic() - last_keepalive >= 15:
                    self._write_sse_keepalive()
                    last_keepalive = time.monotonic()
            else:
                result = outcome[0] if outcome else self._tool_error("Request cancelled")
            if self._client_disconnected():
                cancelled.set()
                self.close_connection = True
                return
            self._write_sse_event(json.dumps(self._modern_response(message, result)), event="message")
            self._end_sse()
        except (BrokenPipeError, ConnectionResetError, OSError):
            cancelled.set()
            self.close_connection = True

    @staticmethod
    def _tool_error(text):
        return {"content": [{"type": "text", "text": text}], "isError": True}

    def _client_disconnected(self):
        # selectors, not select.select: Fusion holds well over 1024 files once it
        # has run for a while, and select() raises ValueError on higher fds.
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.connection, selectors.EVENT_READ)
                readable = selector.select(0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _handle_subscription(self, message, params):
        notifications = params.get("notifications")
        if not isinstance(notifications, dict):
            raise ProtocolError(-32602, "notifications filter must be an object")
        for name in ("toolsListChanged", "resourcesListChanged", "promptsListChanged"):
            if name in notifications and type(notifications[name]) is not bool:
                raise ProtocolError(-32602, f"{name} must be a boolean")
        if "resourceSubscriptions" in notifications and (
            not isinstance(notifications["resourceSubscriptions"], list)
            or not all(isinstance(uri, str) for uri in notifications["resourceSubscriptions"])
        ):
            raise ProtocolError(-32602, "resourceSubscriptions must contain URI strings")
        meta = {META_PREFIX + "subscriptionId": message["id"]}
        self._start_sse()
        try:
            # Our tool list and bundled resources are static. Acknowledge no
            # change filters rather than advertising events we cannot emit.
            self._write_sse_event(json.dumps({
                "jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged",
                "params": {"_meta": meta, "notifications": {}},
            }), event="message")
            last_keepalive = time.monotonic()
            while not self.mcp_server._stop_event.wait(0.1):
                if self._client_disconnected():
                    self.close_connection = True
                    return
                if time.monotonic() - last_keepalive >= 15:
                    self._write_sse_keepalive()
                    last_keepalive = time.monotonic()
            if not self._client_disconnected():
                self._write_sse_event(json.dumps(self._modern_response(message, {"_meta": meta})), event="message")
                self._end_sse()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._chunked_sse = True

    def _end_sse(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self._chunked_sse = False

    def _write_sse_keepalive(self):
        self.wfile.write(b"D\r\n: keepalive\n\n\r\n")
        self.wfile.flush()
