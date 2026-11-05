# MCP protocol compatibility

The same `http://127.0.0.1:8765/mcp` endpoint supports MCP `2026-07-28`
and the existing `2025-11-25`, `2025-06-18`, and `2025-03-26` revisions.
Client configuration does not need to change. This server does not implement
stdio or the separate HTTP+SSE endpoints from `2024-11-05`.

| Behavior | MCP 2026-07-28 | Supported 2025 revisions |
| --- | --- | --- |
| Starting a conversation | Per-request metadata; optional `server/discover` | `initialize`, then `notifications/initialized` |
| Version selection | Each POST declares its version | `initialize` echoes a supported 2025 version, otherwise offers `2025-11-25` |
| Session header | Ignored; no session created or returned | `Mcp-Session-Id` retained |
| Request headers | `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` where applicable | Existing headers accepted; version header may be omitted |
| Result format | `resultType: complete`; cache hints on cacheable methods | Existing result format |
| Change notifications | Request-scoped `subscriptions/listen` | Standalone `GET /mcp` SSE stream |
| Session termination | No sessions; GET/DELETE with the modern version return 405 | `DELETE /mcp` retained |
| Batches | Rejected | Existing batch support retained |

Version selection is performed for each POST, so unrelated clients can use
both eras simultaneously, including over reused HTTP connections. Modern
metadata cannot bypass validation by supplying a legacy version header.
An `initialize` without modern metadata always uses legacy semantics, even
when the client asks for `2026-07-28`.

## Modern requests

Every request supplies `params._meta` with:

- `io.modelcontextprotocol/protocolVersion`: `2026-07-28`.
- `io.modelcontextprotocol/clientCapabilities`: an object; `{}` is sufficient
  for all current tools.
- `io.modelcontextprotocol/clientInfo`: optional client name/version metadata.

Headers must match the body. Missing or mismatched mirrored headers produce
HTTP 400 / `-32020`; unsupported versions produce HTTP 400 / `-32022` with
supported versions. An unknown RPC produces HTTP 404 / `-32601`.
Non-ASCII `Mcp-Name` values use the specification's UTF-8 Base64 sentinel.

For example, this request needs no handshake:

```sh
curl http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: server/discover' \
  --data '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Discovery, tool/resource listings, resource-template listings, and resource
reads return conservative cache hints: `ttlMs: 0`, `cacheScope: private`.
Within one server instance the tool and resource lists are static, so
`subscriptions/listen` acknowledges an empty supported filter and sends no
fabricated change events; with `ttlMs: 0` a stateless client re-lists anyway.
The list can change across instances (Fusion restart or `reload_addin`), so
legacy `initialize` advertises `tools.listChanged` and every new legacy
`GET /mcp` stream starts with `notifications/tools/list_changed`: the server
cannot know what a reconnecting client cached. A stopped server's streams are
closed, so a client that opens no stream on the new server must re-list itself.
Its stream stays open until disconnection or server shutdown; graceful
shutdown sends a complete result.

Tools do not request sampling, elicitation, or roots and do not advertise
optional extensions or prompts. They finish with `resultType: complete`,
including tool errors. No MRTR client input is required by the current tools.
Input validation covers the JSON Schema 2020-12 vocabulary used in the
add-in's owned tool schemas; it is not a general validator for third-party
schemas and does not fetch external references.

## Cancellation and application state

Closing a modern tool's SSE response cancels that request. Pending Fusion
work is removed or skipped before execution. Work already started on Fusion's
main thread can finish; cancellation cannot roll back a CAD operation.
Cancellation tokens belong to individual requests, so identical JSON-RPC IDs
on two clients do not interfere. Final SSE responses terminate the HTTP body
using chunked transfer encoding, allowing connection reuse.

Modern tool requests have a 120-second waiting deadline and a limit of 16
concurrent handler calls (`MCPServer.tool_timeout` and `max_tool_requests`).
A timeout cancels queued work; its tool error explicitly warns that work
already started may finish. Inspect the design before retrying a mutation.
Over-capacity requests return HTTP 429 before dispatch.

Legacy `notifications/cancelled` targets the originating server instance,
session, and request ID. Different sessions can reuse the same request ID
without interfering. Calls without a session header remain supported, but
cannot be targeted by cancellation notifications. A duplicate in-flight ID
within one session returns a tool error and does not replace the original.

Startup waits for a main-thread readiness round-trip before binding the port.
`MCP_MAIN_THREAD_TIMEOUT` bounds dispatcher waits (default 120 seconds): queued
work is cancelled; already-running work may complete and must not be blindly
retried. Stopping cancels queued work and retires that startup generation so
old workers, timers, and queued requests cannot resume after a restart.

Persistent `execute_python` calls using the modern protocol must provide an
explicit, nonempty `session_id`, reused on related calls. Set
`persistent: false` for independent execution. Legacy calls retain the
existing `default` Python session. Application handles such as Python
`session_id`, `$name` references and script filenames are independent of MCP
transport sessions; all clients operate on the same active Fusion design.

## Local endpoint and verification

The server binds to `127.0.0.1`. Native clients may omit `Origin`; when present,
it must exactly match `http://localhost:<server-port>` or
`http://127.0.0.1:<server-port>`, or a value explicitly provided through the
`MCPServer.allowed_origins` constructor option. Invalid origins receive 403
for both eras. Browser frontends on other origins need an explicit allowlist;
this is not an authentication mechanism. The add-in does not provide remote
hosting or OAuth authorization.

Run `python3 -m unittest discover -s tests`. HTTP tests exercise actual sockets:
all three legacy handshakes, notifications, session deletion, tools/resources,
modern discovery/headers/results, keep-alive reuse across both eras, and
cancellation while work is queued or running. Fusion's event pump is mocked;
these tests do not replace a live Fusion smoke test with client applications.

Local validation on 2026-09-06: 108 tests passed with Python 3.14 on macOS,
including camera geometry, camera restoration on capture errors, and PNG
cropping/compositing with color metadata preservation.
Inspector 0.21.2 and 2.5.0 both completed `tools/list` and `tools/call`
against `test_server.py`. The official `@modelcontextprotocol/client` 2.0.0
also passed with `versionNegotiation: {mode: {pin: '2026-07-28'}}`, covering
discovery, tool listing/calling, resource/template listing, and subscription
acknowledgment/closure. Pinning matters: this SDK defaults to legacy mode.
The CI matrix additionally targets Linux and Windows; those runs require a
push to GitHub.

A live smoke test also passed on Fusion 2705.1.11 with its bundled Python
3.14.0 on macOS: `/health` advertised all four revisions, a stateless
`execute_python` call read the Fusion version and loaded add-in path, and a
2025-11-25 client initialized, listed all 11 tools, and terminated its session.
The tool call used `persistent: false` and made no design changes. This smoke
test preceded the viewport additions (the current tool list has 13 entries).
The viewport additions subsequently passed 35 live checks on the same Fusion
and Python versions, including get/set/capture through all three legacy
revisions. See [viewport validation](viewport-validation.md). The ordinary
add-in was restored afterward, with its startup setting enabled and the
development add-in's startup setting disabled.

References:

- [Versioning and compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
- [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- [Subscriptions](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions)
