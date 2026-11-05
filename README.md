# Autodesk Fusion MCP Server

Standalone MCP server that runs inside Autodesk Fusion (formerly Autodesk
Fusion 360) as an add-in. AI agents connect over **Streamable HTTP** -- the
current MCP transport specification -- with no external proxy, middleware, or
dependencies required.

## Highlights

- **Streamable HTTP transport** -- implements the MCP Streamable HTTP spec
  natively; no legacy SSE polling or sidecar servers.
- **Protocol compatibility** -- supports stateless `2026-07-28` requests and
  retains `2025-11-25`, `2025-06-18`, and `2025-03-26` clients on the same
  endpoint. See [protocol behavior and compatibility](docs/mcp-protocol-compatibility.md).
- **Zero external dependencies** -- uses only Python's standard library and
  the Fusion SDK (`adsk.*`).
- **Thread-safe bridge** -- HTTP requests are relayed to Fusion's main thread
  via a Custom Event / work-queue dispatcher, preventing crashes.
- **14 dedicated MCP tools** -- each with a clean, focused schema for better
  LLM tool selection.

## Supported Platforms

- Windows
- Mac OS

## Installation

### Option A: Install from release zip (recommended)

1. Download `AutodeskFusionMCP-v*.zip` from the
   [Releases](https://github.com/frankhommers/autodesk-fusion-mcp/releases)
   page.
2. Extract the zip into your Fusion add-ins folder:
   - **macOS:** `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
   - **Windows:** `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\`
3. Make sure the extracted folder is named `AutodeskFusionMCP` (rename it if
   the zip extracts to a different name).
4. Open Fusion, press **Shift+S**, select the **Add-Ins** tab, and run
   **AutodeskFusionMCP**.
5. The server listens on `http://127.0.0.1:8765/mcp`.

### Option B: Clone from source

1. Clone this repository into your Fusion add-ins folder:
   - **macOS:** `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
   - **Windows:** `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\`
2. Make sure the directory is named `AutodeskFusionMCP`.
3. Open Fusion, press **Shift+S**, select the **Add-Ins** tab, and run
   **AutodeskFusionMCP**.
4. The server listens on `http://127.0.0.1:8765/mcp`.

## MCP Client Configuration

Add to your MCP client config (Claude Desktop, Cursor, etc.):

```json
{
  "mcpServers": {
    "autodesk-fusion-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

## Tools

| Tool | Description |
|---|---|
| `call_autodesk_api` | Execute a generic Fusion API call via dotted path |
| `execute_python` | Run Python code inside the live Fusion session |
| `capture_viewport` | PNG capture with temporary views, transparency, solid backgrounds, cropping and a clean view that hides sketches/construction/joints |
| `get_viewport` | Read camera state and viewport dimensions |
| `set_viewport` | Standard views, projection, orbit, pan, zoom, fit and camera restoration |
| `get_active_selection` | Get objects currently selected in the viewport |
| `list_bodies` | List B-Rep bodies with occurrence path, visibility, volume, area and world bounding box |
| `describe_body` | One body's surface-type counts, analytic faces (bores vs bosses, planes, tori), center of mass |
| `compute` | Run Fusion's Compute All / Assembly Compute and wait for it to finish |
| `document_versions` | List the active document's cloud versions, newest first |
| `restore_version` | Promote an older version and reopen the active document on it (requires confirm) |
| `manage_annotations` | List, claim, complete or release session-only selection annotations |
| `document_status` | List open documents with active, modified, read-only and folder details; flags duplicate instances of one file |
| `activate_document` | Make an open document active by name or DataFile id |
| `timeline_health` | Report broken timeline items, rolled-back marker and active component; optionally suppress broken items |
| `fusion_dialogs` | macOS: list or dismiss (Cancel/Close/Escape only) modal dialogs blocking Fusion's main thread; runs off the main thread, needs Accessibility |
| `delete_features` | Delete timeline items by name, with dependent checks and a dry-run plan (default) |
| `parameters` | List parameters and what they drive; dry-run or set user parameter expressions |
| `editable_files` | Personal Use: list editable designs against the limit, with hints on which to release (read-only) |
| `project_tree` | Read-only cloud project tree: folders, files, versions, read-only flags and links resolved to paths |
| `move_files` | Move cloud files between folders/projects (dry run by default) with a before/after link check |
| `rename_folder` | Rename a cloud folder, refusing case-insensitive sibling clashes; case-only renames go via a temp name |
| `rename_file` | Rename a cloud file (dry run by default), refusing clashes and open files, with a link check |
| `remove_empty_folders` | List truly empty cloud folders and delete them children-first (dry run by default) |
| `sketch_profile` | Build validated, connected closed sketch outlines (lines + arcs) and single-entity circles, in a new or existing sketch, with world coordinates through an occurrence; list a sketch's profiles |
| `check_solids` | Post-feature sanity check: slivers, tiny faces, lumps, non-solids, interference, volume changes vs a baseline |
| `tail_log` | Read Fusion's own app log (filter by substring, level, time; `save`/`errors` presets); runs off the main thread |
| `save_document` | Save with a version comment and verify a new cloud version actually landed (diagnoses stalled saves) |
| `open_document` | Open a cloud file by id or project path, reusing an already-open instance |
| `set_editable_state` | Make cloud files read-only or editable, one at a time and verified (dry run by default) |
| `close_document` | Close a document and verify it went; reports what stayed open and why, and what closed with it |
| `update_references` | List a document's references with loaded and latest versions; optionally get the latest and verify |
| `probe_geometry` | Read-only sampling in world coordinates: point containment, ray hits, grids, and solid intervals along or around an axis |
| `find_entities` | Find faces and edges by type, direction, size, position, feature or convexity; returns EntityRefs every build tool accepts |
| `fetch_api_documentation` | Search Fusion API metadata via runtime introspection |
| `fetch_online_documentation` | Fetch Autodesk cloudhelp docs for a class/member |
| `fetch_design_guide` | Read the bundled design guide |
| `save_script` | Save a reusable Python script |
| `load_script` | Load a previously saved script |
| `list_scripts` | List saved scripts with metadata |
| `delete_script` | Delete a saved script |
| `bridge_info` | Running vs on-disk add-in code: commits, load time, changed files, modules |
| `reload_addin` | Restart the add-in in place to pick up code changed on disk; re-list tools afterwards (new GET /mcp streams get `tools/list_changed`) |
| `delete_files` | Permanently delete cloud files, parents first; dry run by default, refuses open or outside-linked files, reports the server's reason per file |
| `fusion_status` | Check from outside the main thread whether Fusion is responsive, what is running or queued, and why it looks busy; optionally wait until it is idle |

`capture_viewport` also takes a temporary `camera` (eye, target and up in model
coordinates) or a `model_view` that frames the visible bodies with a chosen
`up_axis`, and warns when the image looks blank.
| `timeline_insert` | Run Python with the marker rolled back before a named timeline item, then always restore it and report what changed (dry run by default) |
| `compare_files` | Compare two design files by occurrences, bodies, volume, component names and timeline length, closing only what it opened |
| `compare_bodies` | Read-only: are two bodies the same solid, and where do they differ, by booleans on temporary copies with a sampled fallback; `unknown` rather than a false `same` |
| `combine_bodies` | Join, cut or intersect existing bodies with `keep_tools` stated every time, a temporary-copy preview compared with what was built, and fresh references to every resulting body |
| `construction_plane` | Make an offset, three-point, angled or point-and-normal plane and report where it really is, normal sign included; anchors are named, listed and hidden, never deleted |
| `apply_thread` | Read Fusion's thread library: narrow by family, size and designation to per-class diameters, or get the nearest thread to a diameter (applying a thread is not available yet) |
| `audit_design` | Read-only health report for the active design: file, timeline, links, bodies, joints, sketches and parameters, worst first; profiles, a time budget, baseline diffs, and findings merged from other tools |

### audit_design findings

Every finding has this shape, and `extra_findings` takes the same one, so another tool can add its own checks without this repo knowing about them:

| Field | Meaning |
| --- | --- |
| `check` | Short rule name. Extra findings may not reuse a built-in one |
| `group` | `file`, `timeline`, `links`, `bodies`, `joints`, `sketches`, `parameters`, or the caller's own |
| `severity` | `error`, `warning` or `info` |
| `where` | Where to look, as `<kind>: <name>`. With `check` it identifies the finding between runs, so keep it stable |
| `message` | What was found |
| `why` | Why it matters (may be null) |
| `fix_tool` | The tool to go to next (may be null) |
| `source` | Extra findings only: who supplied it |

Checks say what they found, never how much it matters: `SEVERITY_TABLE` in `audit_rules.py` sets each check's severity per profile, and a check off there does not run. To add a check, write a function over the gathered facts, add it to `CHECKS`, and give it a row.

### Generic API example

```json
{
  "api_path": "rootComponent.sketches.add",
  "args": ["rootComponent.xYConstructionPlane"],
  "remember_as": "my_sketch"
}
```

Path shortcuts: `app`, `ui`, `design`, `rootComponent`, `$stored_name`.

Constructors accepted in args: `Point3D`, `Vector3D`, `Point2D`,
`ValueInput`, `ObjectCollection`, `Matrix3D`.

### Selection example

The `get_active_selection` tool returns details of objects selected in the
viewport and stores them as `$selection_0`, `$selection_1`, etc. for use in
follow-up API calls.

### Selection annotations

Open **Selection annotations** from the Design workspace's **Utilities → Add-Ins**
panel. Select objects in Fusion, click **Capture selection**, type a
note, and click **Add**. The captured selection stays attached even when
you select something else. Shortcut: right-click a selection and choose
**Annotate selection** to open the panel with that selection already captured.
Ask your agent to process the open annotations.
Adding a note does not automatically start an agent.

Annotations are grouped by document and live only in memory. Closing a document,
stopping the add-in, or exiting Fusion discards them. Closing the palette does
not discard them; reopen it from the toolbar.

To follow geometry through edits, each captured object gets a small Fusion
attribute (a *marker*, group `autodesk-fusion-mcp`) holding only the annotation
ID. Fusion carries markers along when geometry is recomputed or split. Markers
are removed when the annotation is deleted or completed and when the add-in
stops. Adding or removing a marker is an undo step in Fusion, and markers present
when you save are stored with that version. **Remove markers from design** in
the palette removes every marker from the active design, including leftovers
from a crash or an earlier session. Objects in referenced (external) components
cannot carry markers; they are tracked in memory only.

Each captured selection shows its status after later edits:

- **ok:** unchanged.
- **changed:** still present, but its size or position differs from capture.
- **split:** divided into several pieces; the agent receives all of them.
- **missing:** consumed or deleted (for example an edge that became a fillet).
  Claiming is refused; edit the annotation and capture it again.
- **unverified:** the timeline is rolled back, so nothing can be checked.

- **Open:** editable and removable.
- **In progress:** claimed by an agent; editing and reactivation are blocked
  in both the UI and backend. The trash button still removes the row immediately.
  Removing a row does not stop or notify the client. Its existing object references
  remain usable until it completes/releases the claim; the row stays gone.
- **Completed:** retained with the agent's result. Delete it with the trash
  button, or choose **Edit / Reactivate** and save to reopen it.
- **Interrupted / failed:** released by the agent after failure/cancellation;
  retained for review, editing/reactivation or deletion.

The agent workflow uses `manage_annotations`:

1. `{"action":"list"}` returns annotations for the active document without
   claiming or consuming them.
2. `{"action":"claim","id":"…","revision":1}` atomically claims an open
   annotation and returns its `claim_token`, the current `$references` of the
   captured objects, and `warnings` for changed, split or unverified selections.
3. Work with those references in the same active document. Do not use
   `clear_context` while references are needed. Other agents must not work on
   claimed annotations.
4. `{"action":"complete","id":"…","claim_token":"…","result":"…"}` marks it
   done. On failure, cancellation or abandonment, use `"action":"release"`
   instead and explain what happened in `result`. References are removed at
   completion/release; the captured objects remain attached to the annotation.

Always complete or release a claim, including when stopping early. A lock has
**no automatic timeout**: silence does not prove the agent has stopped. If an
agent crashes before releasing, you can remove its row with the trash button.
References for a removed in-progress row are retained until the agent finishes,
the document closes, or the add-in stops.
A released annotation is not automatically retried. The user reactivates it.

Edits use revision checks, so an old UI cannot overwrite a newer annotation.
If captured geometry is missing, claiming it is rejected; edit the annotation
and capture its selection again. Annotations do not freeze geometry or prevent
ordinary modeling changes by the user or other agents.

### Editable files (Fusion for Personal Use)

Personal Use allows a limited number of editable documents (10 at the time of
writing); every other document opens read-only and saving it does nothing.
`editable_files` reports where you stand without changing anything:

- `editable_count`, `editable_limit` and `slots_free`. The count and limit
  come from `Data.personalUseLimits` (`editableFileCount`,
  `maxEditableFileCount`) when Fusion reports them; `editable_limit` overrides
  the limit, and 10 is the fallback.
- `editable_files`: each editable design with its path
  (`project/folder/.../name`), version, and `safe_to_release_hints`. Files in
  `personalUseLimits.editableFiles` that the walk did not reach (another hub,
  the `project` filter, or the caps) are still listed, marked `outside_walk`.
- Hints are heuristics: `likely_safe` is true when the file is not open and
  has not changed for `stale_days` (default 14). Open documents, and above
  all open documents with unsaved changes, are listed as `blockers`.
  `parent_references` names designs that link the file; they still load it
  when it is read-only, but Edit In Place on it from those assemblies needs it
  editable.
- The walk covers the active hub only (`app.data.dataProjects`). It is a
  cloud walk and can take a while: it stops after `max_files` designs
  (default 500), `max_depth` folder levels (default 8) or `time_budget_s`
  seconds (default 90, below the 120 s main-thread limit), reports
  `walk.truncated` with `truncated_reason` (`max_files` or `time_budget`), and
  returns `elapsed_seconds`. Read-only files cost only their name, id and
  read-only flag; versions, dates and links are read for editable files only.
- `in_personal_use_list` matches walked files against
  `personalUseLimits.editableFiles` by lineage id (`dm.lineage:` and `vf.`
  version urns compare equal), falling back to project + name when the name is
  unique there (`matched_by: project_and_name`). When the two disagree the
  report adds `personal_use_list_ids_sample` and `walk_ids_sample` (raw ids).

**Switching a file between editable and read-only must be done by you**, in
the Data Panel (right-click the file, **Make Editable** / **Make Read-Only**). What the installed
API offers (checked against the Fusion API definitions shipped with the app,
`Api/Python/packages/adsk/core.py` and the C++ headers):

- `DataFile.isReadOnly`: read-only property, no setter. Also true when
  someone else is editing the file.
- `Data.personalUseLimits` -> `PersonalUseLimits` with `maxEditableFileCount`,
  `editableFileCount` and `editableFiles`, all read-only (`null` on paid
  licences).
- No `DataFile` or `Document` method makes a file editable or read-only.
- Fusion has UI commands `ActiveDocumentActivateCommand` ("Make Editable") and
  `ActiveDocumentDeactivateCommand` ("Make Read-Only") for the active
  document, and `ActiveDocumentsShowActiveDocumentsListCommand` ("My Editable
  Documents"). They are not API members: they act on the active document
  only, Make Read-Only opens a confirmation dialog (which blocks the bridge),
  and they are undocumented, so this add-in deliberately does not run them.
### Modal dialogs (macOS)

A modal dialog blocks Fusion's main thread, so every other tool fails with
"Request cancelled" or a main-thread timeout. `fusion_dialogs` runs on the HTTP
server thread and reads Fusion's windows through Accessibility (`osascript` +
System Events). Facts from live use:

- Fusion's Qt dialogs expose **no button names** to Accessibility, so `dismiss`
  falls back to the Escape key (verified: Preferences closed by Escape).
- An Accessibility query from inside `execute_python` sees **0 windows**: that
  code runs on Fusion's main thread, which cannot service AX requests meanwhile.
  Only the off-main-thread tool works.
- Each call starts with a short preflight (`UI elements enabled`) so a missing
  Accessibility or Automation permission fails at once with the System Settings
  path. osascript has timed out intermittently (15 s, then 0.5 s on the next
  call), so the read-only scripts are retried once on timeout; the press/Escape
  script is not. Results carry `osascript_attempts` with `elapsed_ms` per run.

## Architecture

```
AI agent
  |  Streamable HTTP (POST/GET/DELETE /mcp)
  v
lib/mcp_server.py           -- HTTP server (threading, stdlib only)
  |
fusion_bridge/dispatch.py   -- Custom Event queue relay
  |
fusion_bridge/operations.py -- per-tool routing
  |
fusion_bridge/selection.py  -- viewport selection reader
fusion_bridge/python_exec.py -- Python execution
fusion_bridge/viewport.py   -- camera controls and viewport capture
fusion_bridge/doc_lookup.py -- API docs introspection
fusion_bridge/script_store.py -- script CRUD
  |
adsk.core / adsk.fusion     -- Fusion 360 SDK (main thread)
```

## Shared code

New tools must use `fusion_bridge/common/` instead of private copies of its helpers. A tool module holds what is specific to its tool; anything a second tool could want goes here.

| Module | Holds |
| --- | --- |
| `results.py` | `ok_result`, `error_result`, `tool_result` |
| `args.py` | Typed argument checks, unknown-key rejection, `error_pair` |
| `api.py` | `safe`, `present`, `attempt` reads; `items` (skips what it cannot read) and `strict_items` (raises); both handle count/item collections and Fusion vectors |
| `geometry.py` | Vector math, `perpendicular_pair`, `rotate_about`, `collinear`, `same_plane`, unit conversion |
| `bodies.py` | Body enumeration, `find_occurrence`, `occurrence_frame`, `bbox`, `native`, box overlap and shift, BodyRef parsing and resolving, `temp_copy`, `volume_of`, face records, snapshots, diffs and the baseline store |
| `timeline.py` | Timeline health, `read_rows`, `find_row`, marker and broken-feature refusals, the rolled-back window flag |
| `entity_refs.py` | EntityRefs: `parse_ref`, `resolve`, `make_ref`, `describe_face`, `describe_edge`, the pure `matches_*` filters, edge convexity |
| `feature_ops.py` | Around every write: `preflight`, `read_value`, `value_input`, `anchor_points`, `add_feature`, `snapshot`, `baseline`, `undo`, `body_changes`, `envelope` |
| `documents.py` | Open documents, instances of a file, the event pump |
| `cloud.py` | Cloud paths, projects, folders, files, the project tree walk |
| `app_log.py` | Locating and scanning Fusion's log, the server's reason for a failed cloud write |

`results`, `args`, `api`, `geometry`, `cloud` and `app_log` must not import `adsk` or `dispatch` (`tests/test_common.py` checks this). `entity_refs` imports `adsk` only inside `FusionScene`, so its filters load anywhere. Tests import these modules as `from fusion_bridge.common import cloud`.

## Testing

### Unit tests

Run the test suite (no Fusion required):

```
python3 -m unittest discover -s tests
```

CI runs these tests on every push and pull request via GitHub Actions.
Real HTTP tests cover both protocol eras, connection reuse, and cancellation
without requiring Fusion. A live Fusion/client smoke test is still needed
before releasing transport changes.

### Protocol compliance with MCP Inspector

The official [MCP Inspector](https://github.com/modelcontextprotocol/inspector)
can verify Streamable HTTP compliance against a running server.

**Against the live Fusion add-in** (port 8765):

```
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8765/mcp --transport http --method tools/list
```

**Against the standalone test server** (no Fusion required):

```
python3 test_server.py                  # starts a dummy server on port 9765
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:9765/mcp --transport http --method tools/list
```

## Reporting Issues

Please report any issues on the [Issues](https://github.com/frankhommers/autodesk-fusion-mcp/issues) page.

## Author

Frank Hommers / [Initialize](https://initialize.nl)

## License

This project is licensed under the terms of the MIT license. See [LICENSE](LICENSE).

## Changelog

- v 1.6.0
  - Track annotated geometry with Fusion attribute markers (group
    `autodesk-fusion-mcp`) so annotations follow recomputes and splits
  - Report each selection as ok, changed, split, missing or unverified in the
    palette and in `manage_annotations`; claims return every piece of a split
    selection plus warnings, and refuse missing geometry
  - Remove markers on delete, completion and add-in stop; **Remove markers from
    design** purges all markers, including leftovers

- v 1.5.0
  - Add session-only selection annotations: a Fusion palette to attach notes
    to captured selections, and the `manage_annotations` tool so agents can
    list, claim, complete or release them with revision and claim-token checks
  - Right-click a selection and choose **Annotate selection** to open the
    palette with that selection already captured
  - Removing a row never aborts client work; references stay valid until the
    agent completes or releases its claim
  - Palette and About dialog prefer Fusion's Artifakt Element font

- v 1.4.1
  - Wait for Fusion's main thread before binding the MCP server; bound
    dispatcher waits and distinguish queued cancellation from running work
  - Scope legacy cancellation by server, session, and request ID; retire
    queued work and background startup tasks when the add-in stops
  - Add regression coverage for cancellation isolation and Stop/Run races;
    133 automated tests pass on Linux, macOS, and Windows

- v 1.4.0
  - Add `get_viewport` and `set_viewport`: camera snapshots, standard views,
    projection, orbit, pan, zoom, and fit
  - Extend `capture_viewport` with temporary views, transparent or solid
    backgrounds, anti-aliasing control, native dimensions, and cropping
  - Support MCP `2026-07-28` while retaining all three supported 2025
    revisions on the same endpoint; persistent Python calls using the new
    protocol require an explicit `session_id`
  - Fix empty-response framing for HTTP keep-alive clients; validate browser
    origins and scope modern cancellation to individual requests
  - Log the Fusion and Python runtime versions; verify the bundled Autodesk
    utilities against Fusion 2705.1.11
  - Validate with 108 automated tests and 35 live viewport checks on macOS;
    expand Python 3.14 CI to Linux, macOS, and Windows

- v 1.3.0
  - `fetch_online_documentation` returns a `preview` flag, so an agent can see
    that a class is preview API before building on it. Autodesk renames these
    between releases without a deprecation period
  - Design guide: fixed every example, which wrapped its arguments in an
    `operation` field that no tool accepts
  - Design guide: new sections on preview APIs and on sheet metal ordering,
    covering `foldFeatures`, `joinByBendFeatures`, `cornerClosureFeatures` and
    flat patterns

- v 1.2.0
  - `fetch_online_documentation` now understands all three Autodesk help
    layouts. Property pages contribute `property_type`, `access` and
    `property_description`; class pages contribute their `methods`,
    `properties` and `accessed_from` member lists
  - Property syntax lines are recognised (they carry no argument list, and
    Autodesk leaves the emphasis tag unclosed)
  - Fixed every sample on a page inheriting the first sample's URL
  - Fixed descriptions coming back empty, as Autodesk emits `<p class="api">`

- v 1.1.0
  - Negotiate the MCP protocol version instead of always answering
    `2025-03-26`; `2025-11-25` and `2025-06-18` are now supported, which fixes
    clients refusing to connect ([#1](https://github.com/frankhommers/autodesk-fusion-mcp/issues/1))
  - Tolerate the `MCP-Protocol-Version` request header introduced in 2025-06-18
  - `/health` now reports `supported_protocols`
  - Fixed `fetch_online_documentation` silently dropping the `syntax` field
    after Autodesk changed its help markup from `<strong>` to `<b>`
  - Per-command execution duration and ISO 8601 timestamps in the log
  - CI now runs on Python 3.14, matching the interpreter Fusion bundles

- v 1.0.0
  - Initial release
  - 11 dedicated MCP tools with individual schemas
  - Streamable HTTP transport (MCP spec 2025-03-26)
  - Thread-safe main-thread dispatch
  - Viewport selection reader (`get_active_selection`)
  - Python code execution with persistent sessions
  - API documentation introspection
  - Script management (save/load/list/delete)
  - Zero external dependencies
