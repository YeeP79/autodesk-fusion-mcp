"""Central definitions for the public MCP tool surface."""

# Tool name constants
CALL_AUTODESK_API = "call_autodesk_api"
EXECUTE_PYTHON = "execute_python"
CAPTURE_VIEWPORT = "capture_viewport"
GET_VIEWPORT = "get_viewport"
SET_VIEWPORT = "set_viewport"
FETCH_API_DOCUMENTATION = "fetch_api_documentation"
FETCH_ONLINE_DOCUMENTATION = "fetch_online_documentation"
FETCH_DESIGN_GUIDE = "fetch_design_guide"
SAVE_SCRIPT = "save_script"
LOAD_SCRIPT = "load_script"
LIST_SCRIPTS = "list_scripts"
DELETE_SCRIPT = "delete_script"
GET_ACTIVE_SELECTION = "get_active_selection"
MANAGE_ANNOTATIONS = "manage_annotations"
COMPUTE = "compute"
DOCUMENT_VERSIONS = "document_versions"
RESTORE_VERSION = "restore_version"
DOCUMENT_STATUS = "document_status"
ACTIVATE_DOCUMENT = "activate_document"
TIMELINE_HEALTH = "timeline_health"
FUSION_DIALOGS = "fusion_dialogs"
DELETE_FEATURES = "delete_features"
LIST_BODIES = "list_bodies"
DESCRIBE_BODY = "describe_body"
PARAMETERS = "parameters"
EDITABLE_FILES = "editable_files"
PROJECT_TREE = "project_tree"
MOVE_FILES = "move_files"
RENAME_FOLDER = "rename_folder"
RENAME_FILE = "rename_file"
REMOVE_EMPTY_FOLDERS = "remove_empty_folders"
SKETCH_PROFILE = "sketch_profile"
CHECK_SOLIDS = "check_solids"
BRIDGE_INFO = "bridge_info"
RELOAD_ADDIN = "reload_addin"
TAIL_LOG = "tail_log"

# Tools the HTTP server thread runs inline instead of dispatching to Fusion's
# main thread. Only for handlers that never call the Fusion API: they must
# keep working while a modal dialog blocks the main thread.
DIRECT_TOOL_NAMES = frozenset({FUSION_DIALOGS, TAIL_LOG})
SAVE_DOCUMENT = "save_document"
OPEN_DOCUMENT = "open_document"
DELETE_FILES = "delete_files"
SET_EDITABLE_STATE = "set_editable_state"

# Resource constants
RESOURCE_URI = "fusion://design-guide"
RESOURCE_NAME = "Autodesk Fusion Design Guide"
RESOURCE_DESCRIPTION = "Workflow guidance, API patterns, naming rules, and modeling habits for Autodesk Fusion."

STANDARD_VIEWS = ["front", "back", "left", "right", "top", "bottom", "isometric",
                  "iso_top_left", "iso_top_right", "iso_bottom_left", "iso_bottom_right"]
PROJECTIONS = ["orthographic", "perspective", "perspective_with_ortho_faces"]
CLEAN_VIEW_CATEGORIES = ["sketches", "construction", "origin", "joints", "canvases", "decals"]
UNITS_SCHEMA = {"type": "string", "enum": ["in", "cm", "mm"],
                "description": "Length unit for output (default: in). Areas/volumes use its square/cube."}
POINT_SCHEMA = {
    "type": "object", "properties": {axis: {"type": "number", "minimum": -1e12, "maximum": 1e12} for axis in ("x", "y", "z")},
    "required": ["x", "y", "z"], "additionalProperties": False,
}
CAMERA_SCHEMA = {
    "type": "object",
    "description": "Camera snapshot from get_viewport. Coordinates/extents in cm; perspective_angle in degrees. Use alone to restore a camera.",
    "properties": {
        "eye": POINT_SCHEMA, "target": POINT_SCHEMA, "up_vector": POINT_SCHEMA,
        "projection": {"type": "string", "enum": PROJECTIONS},
        "extents": {
            "type": "object", "description": "Required for orthographic cameras only, in cm.",
            "properties": {k: {"type": "number", "minimum": 1e-9, "maximum": 1e12} for k in ("width", "height")},
            "required": ["width", "height"], "additionalProperties": False,
        },
        "perspective_angle": {"type": "number", "minimum": 0.01, "maximum": 179,
                              "description": "Required for perspective cameras only; angle in degrees."},
    },
    "required": ["eye", "target", "up_vector", "projection"], "additionalProperties": False,
}

BODY_REF_SCHEMA = {
    "type": "object",
    "properties": {"occurrence_path": {"type": "string", "description": "Occurrence fullPathName, or 'root'."},
                   "body_name": {"type": "string"}},
    "required": ["occurrence_path", "body_name"], "additionalProperties": False,
}

_COORD_SCHEMA = {"type": "array", "items": {"type": "number"},
                 "description": "[x, y] in sketch space or [x, y, z] in model space."}

# Each tool: {"name", "description", "inputSchema"}
TOOL_DEFINITIONS = [
    {
        "name": CALL_AUTODESK_API,
        "description": (
            "Execute a generic Autodesk Fusion API call. "
            "Resolve a dotted API path, invoke it with args/kwargs, and optionally store the result.\n\n"
            "Path shortcuts: app, ui, design, rootComponent, $stored_name\n\n"
            "Constructors accepted in args/kwargs: Point3D, Vector3D, Point2D, "
            "ValueInput, ObjectCollection, Matrix3D\n\n"
            'Example: {"api_path": "rootComponent.sketches.add", '
            '"args": ["rootComponent.xYConstructionPlane"], "remember_as": "my_sketch"}'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "api_path": {
                    "type": "string",
                    "description": (
                        "Dotted path to Fusion API method/property "
                        "(e.g. 'rootComponent.sketches.add'). "
                        "Shortcuts: app, ui, design, rootComponent, $stored_var"
                    ),
                },
                "args": {
                    "type": "array",
                    "items": {},
                    "description": (
                        "Positional arguments. Can be literals, API paths, "
                        "$references, or constructors like "
                        '{"type": "Point3D", "x": 0, "y": 0, "z": 0}'
                    ),
                },
                "kwargs": {
                    "type": "object",
                    "description": "Keyword arguments for the API call",
                },
                "remember_as": {
                    "type": "string",
                    "description": "Store the result with this name for later use via $name",
                },
                "return_properties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Which properties to return from the result object",
                },
                "description": {
                    "type": "string",
                    "description": "Short description of what this API call does",
                },
            },
        },
    },
    {
        "name": EXECUTE_PYTHON,
        "description": (
            "Run Python code inside the active Fusion 360 session with access "
            "to the full SDK. Calls with the same session_id share one namespace, like a "
            "REPL: variables, imports, functions and classes carry over, including names "
            "bound before a call failed. session_variables lists plain values and "
            "session_definitions the modules, classes and functions. Injected names "
            "(app, ui, adsk, os, json, time, ...) are reset on every call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Explicit identifier for related persistent Python calls. "
                        "Required with persistent=true on MCP 2026-07-28; choose a unique value. "
                        "Legacy clients may omit it (default: 'default')."
                    ),
                },
                "persistent": {
                    "type": "boolean",
                    "description": "Whether to run in the session's namespace (default: true); false runs in a fresh one and keeps nothing",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Short description of what the code does, "
                        "shown in Fusion console"
                    ),
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": CAPTURE_VIEWPORT,
        "description": (
            "Capture the active Fusion viewport as a PNG. Optional view and fit are temporary: "
            "the original camera is restored even on failure. Background can be viewport, "
            "transparent, or #RRGGBB. Crop uses pixels in the rendered image, origin top-left. "
            "Returns only the cropped region when crop is provided; limit 16 megapixels. "
            "clean_view temporarily hides sketches, construction geometry, origin, joints, canvases "
            "and decals (never bodies) and restores each flag afterwards, even on failure; a text item "
            "reports which categories were hidden and whether all were restored."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {
                    "type": "integer",
                    "minimum": 0, "maximum": 8192,
                    "description": "Rendered image width in pixels (default: 800; 0 uses viewport width)",
                },
                "height": {
                    "type": "integer",
                    "minimum": 0, "maximum": 8192,
                    "description": "Rendered image height in pixels (default: 600; 0 uses viewport height)",
                },
                "view": {"type": "string", "enum": STANDARD_VIEWS, "description": "Temporary ViewCube-relative standard view; omit to keep current view."},
                "fit": {"type": "boolean", "description": "Temporarily fit all graphics before capture (default: false)."},
                "background": {"type": "string", "description": "viewport (default), transparent, or a solid #RRGGBB color."},
                "anti_aliasing": {"type": "boolean", "description": "Smooth rendered edges (default: true)."},
                "clean_view": {"type": "boolean", "description": "Temporarily hide non-body clutter for this capture (default: false)."},
                "clean_view_keep": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(CLEAN_VIEW_CATEGORIES)},
                    "description": "Categories to leave visible when clean_view is true.",
                },
                "crop": {
                    "type": "object", "description": "Rectangle inside the rendered image; output dimensions equal crop width/height.",
                    "properties": {k: {"type": "integer", "minimum": 0 if k in ("x", "y") else 1, "maximum": 8192}
                                   for k in ("x", "y", "width", "height")},
                    "required": ["x", "y", "width", "height"], "additionalProperties": False,
                },
            },
        },
    },
    {
        "name": GET_VIEWPORT,
        "description": (
            "Read active viewport pixel dimensions and camera eye, target, up_vector, projection, "
            "and extents or perspective_angle. Lengths are cm and angles degrees. "
            "Pass the returned camera object to set_viewport to restore it. All clients share this viewport."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": SET_VIEWPORT,
        "description": (
            "Control the active Fusion camera. Changes apply in order: projection/view, fit, orbit, pan, zoom. "
            "Standard views follow the user's ViewCube orientation. Orbit angles use right-hand rotation: "
            "yaw about camera up, pitch about camera right, roll about the viewing direction. "
            "Pan translates the camera along screen right/up in cm. Zoom >1 zooms in; <1 zooms out. "
            "Alternatively pass a complete camera snapshot alone. Returns actual camera state. "
            "This changes the shared viewport for all clients, without modifying model geometry."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "camera": CAMERA_SCHEMA,
                "view": {"type": "string", "enum": STANDARD_VIEWS},
                "projection": {"type": "string", "enum": PROJECTIONS},
                "fit": {"type": "boolean", "description": "Fit all graphics (default: false)."},
                "orbit": {"type": "object", "additionalProperties": False,
                          "properties": {k: {"type": "number", "minimum": -360, "maximum": 360} for k in ("yaw", "pitch", "roll")}},
                "pan": {"type": "object", "additionalProperties": False,
                        "properties": {k: {"type": "number", "minimum": -1e9, "maximum": 1e9} for k in ("x", "y")}},
                "zoom": {"type": "number", "minimum": 0.01, "maximum": 100},
                "description": {"type": "string"},
            },
        },
    },
    {
        "name": FETCH_API_DOCUMENTATION,
        "description": (
            "Search live Fusion API metadata through runtime introspection. "
            "Returns scored results with class overviews, properties, "
            "and function signatures."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_term": {
                    "type": "string",
                    "description": (
                        "Search term (e.g. 'BRepBody', 'sketches', "
                        "'adsk.fusion.Sketch.add')"
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Search category: class_name, member_name, description, or all"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 3)",
                },
            },
            "required": ["search_term"],
        },
    },
    {
        "name": FETCH_ONLINE_DOCUMENTATION,
        "description": (
            "Fetch Autodesk cloudhelp documentation for a specific "
            "Fusion API class or member."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "class_name": {
                    "type": "string",
                    "description": "API class name (e.g. 'BRepBody', 'Sketch')",
                },
                "member_name": {
                    "type": "string",
                    "description": "Optional member name (e.g. 'add', 'name')",
                },
            },
            "required": ["class_name"],
        },
    },
    {
        "name": FETCH_DESIGN_GUIDE,
        "description": (
            "Read the bundled Fusion design guide with workflow guidance, "
            "API patterns, naming rules, and modeling habits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": SAVE_SCRIPT,
        "description": "Save a reusable Python script to the user scripts directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Script filename (e.g. 'my_script.py')",
                },
                "code": {
                    "type": "string",
                    "description": "Python code to save",
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of the script",
                },
            },
            "required": ["filename", "code"],
        },
    },
    {
        "name": LOAD_SCRIPT,
        "description": "Load a previously saved Python script by filename.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Script filename to load",
                },
            },
            "required": ["filename"],
        },
    },
    {
        "name": LIST_SCRIPTS,
        "description": (
            "List all saved Python scripts with metadata "
            "(filename, size, modified date)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": DELETE_SCRIPT,
        "description": "Delete a saved Python script by filename.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Script filename to delete",
                },
            },
            "required": ["filename"],
        },
    },
    {
        "name": GET_ACTIVE_SELECTION,
        "description": (
            "Get the objects currently selected by the user in the Fusion 360 viewport. "
            "Returns detailed info per item (type, name, entityToken, parent component, "
            "and type-specific properties like area, volume, material). "
            "Each selected object is stored as $selection_0, $selection_1, etc. "
            "for use in follow-up API calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": MANAGE_ANNOTATIONS,
        "description": (
            "Read and process the user's session-only selection annotations in the active document. "
            "list is read-only; claim requires id and current revision, locks editing, and returns "
            "a private claim_token and $references to the captured objects. Claim before working. "
            "complete requires id, claim_token and result; marks done and unlocks. "
            "On failure, cancellation or abandonment, release with id, claim_token and result explaining "
            "what happened; this unlocks and marks failed for user review. Never leave a claim unfinished. "
            "Do not act on annotations claimed by another agent. No automatic agent execution or timeout. "
            "Keep the same active document while working. References expire on completion/release. "
            "Each selection has a status: ok, changed (geometry differs from capture), split (references "
            "cover every piece), unverified (timeline rolled back) or missing (claim refused; ask the user "
            "to recapture). claim returns warnings for changed/split/unverified selections: check them "
            "before modifying anything."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "claim", "complete", "release"]},
                "id": {"type": "string", "description": "Annotation ID from list."},
                "revision": {"type": "integer", "minimum": 1, "description": "Required for claim; use revision from list."},
                "claim_token": {"type": "string", "description": "Token returned by your claim; required to complete/release."},
                "result": {"type": "string",
                           "description": "Required to complete/release. User-visible summary or failure reason."},
            },
            "required": ["action"],
        },
    },
    {
        "name": COMPUTE,
        "description": (
            "Run Fusion's own Compute All (scope 'all') or Assembly Compute (scope 'assembly') command "
            "and wait for it to finish. Use after API edits to parameters, sketches or joints: "
            "Design.computeAll() does not always regenerate geometry or re-solve joint positions "
            "the way the UI command does. Returns elapsed time and a timeline health summary "
            "(healthy/warning/error counts and the names of features in error)."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "scope": {"type": "string", "enum": ["all", "assembly"],
                          "description": "'all' = Modify > Compute All (default); 'assembly' = Assembly Compute."},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 3600,
                                    "description": "How long to wait for the compute to finish (default 300)."},
            },
        },
    },
    {
        "name": DOCUMENT_VERSIONS,
        "description": (
            "List versions of the active document's cloud file, newest first: version number, "
            "date created (ISO 8601 UTC), description, milestone name and creator when available. "
            "is_current marks the version loaded in the open document; is_latest the newest. Read-only."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                          "description": "Maximum versions to return (default: 20)."},
                "description": {"type": "string"},
            },
        },
    },
    {
        "name": RESTORE_VERSION,
        "description": (
            "Restore an older version of the active document. Promotes that version to a new latest "
            "version (history is kept), closes the document without saving and reopens it so the "
            "restored content is loaded and active. Closing also closes documents it links. "
            "Refuses unless confirm is true (the refusal describes what would happen), and refuses when "
            "the document or other open documents have unsaved changes unless discard_unsaved is true. "
            "Returns the new version number."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "version_number": {"type": "integer", "minimum": 1,
                                   "description": "Version to restore, from document_versions."},
                "confirm": {"type": "boolean", "description": "Must be true to restore."},
                "discard_unsaved": {"type": "boolean",
                                    "description": "Discard unsaved changes instead of refusing (default: false)."},
                "description": {"type": "string"},
            },
            "required": ["version_number", "confirm"],
        },
    },
    {
        "name": DOCUMENT_STATUS,
        "description": (
            "List every open document: name, whether it is active, document/product types, "
            "isModified, isSaved and, when saved to the cloud, DataFile id, version number, "
            "isReadOnly ('unknown' if unreadable), project, folder path and which open documents "
            "reference it. Warns when the active document is read-only (saves will not stick, "
            "common on Personal Use licences) or has unsaved changes. Documents sharing a DataFile "
            "id are marked duplicate_instances/loaded_versions with a warning: saving a non-primary "
            "instance can silently do nothing, so close the extra one without saving and reopen. "
            "Only the active document reads reliably; switch with activate_document."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    {
        "name": ACTIVATE_DOCUMENT,
        "description": (
            "Make an open document the active one. Give exactly one of name (document name such "
            "as 'Frame v3', or the DataFile name 'Frame') or data_file_id (from document_status). "
            "Ambiguous names are refused with the candidates and their folder paths. Returns the "
            "newly active document and its warnings."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "description": "Open document or DataFile name."},
                "data_file_id": {"type": "string", "description": "DataFile id from document_status."},
            },
        },
    },
    {
        "name": TIMELINE_HEALTH,
        "description": (
            "Report the active design's timeline health: marker position vs item count (warns if rolled back), "
            "the active component (warns if not root), healthy/warning/error counts, and every non-healthy item "
            "with index, name, entity type, health, Fusion's error/warning message and suppressed flag. "
            "Run this when an edit is refused because Fusion rolled it back: API edits fail while the timeline "
            "has broken features. suppress_broken with confirm: true suppresses error-state items earliest first "
            "(skipping already-suppressed ones) and reports what it suppressed; this changes the design."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "suppress_broken": {"type": "boolean",
                                    "description": "Suppress error-state timeline items (default false). Requires confirm: true."},
                "confirm": {"type": "boolean", "description": "Must be true alongside suppress_broken."},
            },
        },
    },
    {
        "name": FUSION_DIALOGS,
        "description": (
            "macOS only. List or dismiss modal dialogs that block Fusion's main thread "
            "(symptom: other tools fail with 'Request cancelled' or 'Timed out waiting for the "
            "Fusion main thread'). Runs outside the main thread via Accessibility, so it works "
            "while Fusion is blocked. list (default) reports modal windows/sheets with title, "
            "role/subrole and button names. dismiss acts on the first modal dialog (optionally "
            "filtered by title_contains): it presses Cancel or Close, otherwise sends Escape; it "
            "never presses Save, Delete, Yes, OK, Replace or Continue. Fusion's Qt dialogs expose "
            "no button names, so expect dismiss to send Escape (verified on Preferences). Needs "
            "Accessibility permission for Fusion; a fast preflight reports a missing permission "
            "at once. Read-only osascript runs retry once on timeout; osascript_attempts gives "
            "elapsed_ms per attempt. Do not query Accessibility from execute_python: on Fusion's "
            "main thread it sees 0 windows."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "dismiss"],
                           "description": "list (default) or dismiss."},
                "title_contains": {"type": "string",
                                   "description": "Only consider dialogs whose title contains this text (case-insensitive)."},
            },
        },
    },
    {
        "name": DELETE_FEATURES,
        "description": (
            "Delete timeline items (features, sketches, construction geometry, ...) by name in the ACTIVE "
            "parametric design. Use this instead of BRepBody.deleteMe() (bodies regenerate from their "
            "features) or deleting everything after a timeline index. Every name must resolve to exactly "
            "one item, optionally scoped to a component; ambiguous or missing names refuse the whole call "
            "with candidates. Items in linked (external) components are never touched. Later items that "
            "consume a target (sketch profiles/paths, created bodies, construction geometry) are dependents: "
            "the call refuses unless include_dependents=true, which deletes them too. Items that only use a "
            "body the target modified are reported as possibly_affected and kept. dry_run defaults to true "
            "and returns the plan (delete order, latest first) without changing anything. After deleting, "
            "returns timeline health counts and items that went to error."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "names": {"type": "array", "items": {"type": "string"},
                          "description": "Timeline item names exactly as shown in the timeline/browser."},
                "component": {"type": "string",
                              "description": "Only match items owned by this component (by name). Default: any "
                                             "component of the active design except linked ones."},
                "include_dependents": {"type": "boolean",
                                       "description": "Also delete detected dependents (default false: refuse)."},
                "dry_run": {"type": "boolean",
                            "description": "Return the plan without deleting (default true)."},
            },
            "required": ["names"],
        },
    },
    {
        "name": LIST_BODIES,
        "description": (
            "List B-Rep bodies in the active design: occurrence fullPathName ('root' for root bodies), "
            "component, linked (referenced component), body name and body_index, visibility of body and "
            "owning occurrence, solid vs surface, volume (in3 and cm3), area, and world-coordinate bounding box. "
            "Hidden bodies are excluded by default because they are often stale reference geometry. "
            "Filters are case-insensitive substrings. Output is capped with total_matched and truncated."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "include_hidden": {"type": "boolean", "description": "Include hidden bodies and bodies in hidden occurrences (default: false)."},
                "name_contains": {"type": "string", "description": "Only bodies whose name contains this text."},
                "component_contains": {"type": "string", "description": "Only bodies whose component name contains this text."},
                "units": UNITS_SCHEMA,
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "Row cap (default: 200)."},
            },
        },
    },
    {
        "name": DESCRIBE_BODY,
        "description": (
            "Describe one body (identify it with occurrence_path and body_name from list_bodies; add body_index "
            "when names repeat). Ambiguous or missing matches are refused with candidates. Reports face counts by "
            "surface type, edge/shell/lump counts, isSolid, center of mass and principal axes, and analytic faces "
            "in world coordinates, largest first: cylinders (diameter, axis, length along axis, sweep, concave "
            "bore vs convex boss, creating feature), planes (point, outward normal, area), cones, tori (major/minor "
            "radius) and spheres. Fillet faces can look like bores by radius: check concavity, sweep_deg and created_by."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "occurrence_path": {"type": "string", "description": "Occurrence fullPathName from list_bodies, or 'root'."},
                "body_name": {"type": "string"},
                "body_index": {"type": "integer", "minimum": 0, "description": "Index of the body within its occurrence (from list_bodies)."},
                "units": UNITS_SCHEMA,
                "max_faces": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Cap per face type (default: 25)."},
            },
            "required": ["occurrence_path", "body_name"],
        },
    },
    {
        "name": PARAMETERS,
        "description": (
            "Read the active design's parameters and what they drive, or set user parameter expressions. "
            "action 'list' (default): user parameters with expression, value in the parameter's own unit, "
            "comment, favorite, direct dependents and the features/sketches they drive (transitively, "
            "depth-capped); include_model adds model parameters with owning feature/sketch and role. "
            "Filter with name_contains / comment_contains; rows are capped by limit with truncation info. "
            "Pass name for one parameter in detail: what it references and its full dependents tree. "
            "action 'set': updates = [{name, expression}] for USER parameters only (model parameters are "
            "refused). dry_run defaults to true and shows old -> new expression, unknown identifiers, cycles "
            "and the dependents that would change. With dry_run false the expressions are set in order, "
            "stopping at the first failure; the result says which applied and the timeline health. "
            "Run the compute tool afterwards: API recompute does not always regenerate geometry like the UI."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "set"], "description": "Default 'list'."},
                "include_model": {"type": "boolean",
                                  "description": "list: also return model parameters (default false)."},
                "name_contains": {"type": "string", "description": "list: case-insensitive name filter."},
                "comment_contains": {"type": "string", "description": "list: case-insensitive comment filter."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000,
                          "description": "list: maximum rows per section (default 100)."},
                "name": {"type": "string", "description": "list: show this one parameter in detail."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "How many levels of dependents to follow (default 5)."},
                "updates": {
                    "type": "array",
                    "description": "set: 1-50 user parameters to change, applied in this order.",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "expression": {"type": "string",
                                           "description": "Fusion expression, e.g. '1.75 in' or 'width / 2'."},
                        },
                        "required": ["name", "expression"],
                    },
                },
                "dry_run": {"type": "boolean", "description": "set: preview only (default true)."},
            },
        },
    },
    {
        "name": EDITABLE_FILES,
        "description": (
            "Read-only report for Fusion for Personal Use (limited number of editable documents). "
            "Walks the active hub's projects (or one project) and lists every Fusion design that is "
            "editable, the count against the limit (from Data.personalUseLimits when available), path, "
            "version and heuristic 'safe to release' hints: not open, not changed for stale_days, "
            "and which designs link it. Never changes anything: the API cannot toggle read-only; "
            "the user does it in the Data Panel (right-click > Make Read-Only / Make Editable). "
            "The cloud walk can be slow; capped by max_files/max_depth and a time budget "
            "(walk.truncated_reason 'max_files' or 'time_budget'), elapsed time reported."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "project": {"type": "string",
                            "description": "Only walk projects whose name matches (case-insensitive; exact match wins, else substring)."},
                "editable_limit": {"type": "integer", "minimum": 1,
                                   "description": "Override the editable limit (default: personalUseLimits.maxEditableFileCount, else 10)."},
                "max_files": {"type": "integer", "minimum": 1, "maximum": 20000,
                              "description": "Stop the walk after this many design files (default 500)."},
                "max_depth": {"type": "integer", "minimum": 0, "maximum": 64,
                              "description": "Folder levels below each project root to walk (default 8)."},
                "stale_days": {"type": "number", "minimum": 0,
                               "description": "A file unchanged for this many days counts as stale (default 14)."},
                "include_read_only": {"type": "boolean",
                                      "description": "Also list read-only designs found by the walk (default false)."},
                "time_budget_s": {"type": "number", "minimum": 1, "maximum": 110,
                                  "description": "Stop the walk after this many seconds (default 90; Fusion's main thread allows 120 per call)."},
            },
        },
    },
    {
        "name": PROJECT_TREE,
        "description": (
            "Read-only listing of a Fusion cloud project (active hub) as nested folders and files. "
            "Each file has name, extension/type, version, is_read_only, date_modified (ISO UTC), id and path. "
            "With include_links, 'uses' (designs it references) and 'used_by' (designs referencing it) are "
            "resolved by id to project/folder/name paths. File names are not unique: same-name copies are "
            "grouped in summary.duplicate_names; always identify files by id. Folder names are case-insensitive. "
            "Empty folders are flagged. Cloud walks are slow: the result reports elapsed time and whether it "
            "was truncated (max_files, depth, time budget). list_projects=true just lists project names/ids."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "project": {"type": "string",
                            "description": "Project name or id. Required unless list_projects is true."},
                "folder": {"type": "string",
                           "description": "Folder path inside the project, e.g. 'Chassis/Front'. Default: project root."},
                "depth": {"type": "integer", "minimum": 0, "maximum": 50,
                          "description": "Subfolder levels to descend below 'folder' (default 3; 0 = that folder only)."},
                "max_files": {"type": "integer", "minimum": 1, "maximum": 5000,
                              "description": "Stop after listing this many files (default 500)."},
                "include_links": {"type": "boolean",
                                  "description": "Resolve each file's uses/used_by links to paths (default true; slower)."},
                "name_contains": {"type": "string",
                                  "description": "Only list files whose name contains this text (case-insensitive)."},
                "list_projects": {"type": "boolean",
                                  "description": "List the active hub's project names and ids instead of a tree."},
            },
        },
    },
    {
        "name": MOVE_FILES,
        "description": (
            "Move cloud files (active hub) into another folder, optionally in another project. dry_run defaults to "
            "true and only reports the plan; pass dry_run=false to move. Files are given as paths "
            "('Folder/Sub/Name', case-insensitive) or ids ('urn:...'). Refuses, changing nothing, when a path "
            "matches several files (lists path, version and id of each), when the target folder already has a "
            "file of that name and type (ignoring case), or when a file is open in Fusion (close it yourself; "
            "the tool never closes documents). A link check snapshots every moved file and every file linking "
            "to or from it before and after, and reports changed paths and lost or unresolvable links."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "Project (name or id) the files are in."},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "1 to 50 file paths inside the project (e.g. 'Chassis/Tube') or file ids."},
                "to_folder": {"type": "string",
                              "description": "Target folder path inside the target project ('' = its root)."},
                "to_project": {"type": "string",
                               "description": "Target project (name or id) for a cross-project move. Default: 'project'."},
                "create_missing": {"type": "boolean",
                                   "description": "Create missing folders on the to_folder path (default false)."},
                "dry_run": {"type": "boolean", "description": "Only report the plan (default true)."},
            },
            "required": ["project", "files", "to_folder"],
        },
    },
    {
        "name": RENAME_FOLDER,
        "description": (
            "Rename a cloud folder. dry_run defaults to true. Folder names are case-insensitive in Fusion: "
            "refuses when a sibling already has the new name in any case. A case-only rename of the same folder "
            "is done in two steps through a temporary name, reported step by step (and restored if the second "
            "step fails). Files inside are link-checked before and after."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "Project name or id."},
                "folder": {"type": "string", "description": "Path of the folder to rename, e.g. 'Chassis/drivetrain'."},
                "new_name": {"type": "string", "description": "New folder name (a name, not a path)."},
                "dry_run": {"type": "boolean", "description": "Only report the plan (default true)."},
            },
            "required": ["project", "folder", "new_name"],
        },
    },
    {
        "name": RENAME_FILE,
        "description": (
            "Rename a cloud file (name without extension). dry_run defaults to true. Refuses on ambiguous paths, "
            "on a sibling file with the same name and type (ignoring case), and on files open in Fusion. "
            "Links are checked before and after."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "Project name or id."},
                "file": {"type": "string", "description": "File path inside the project or file id ('urn:...')."},
                "new_name": {"type": "string", "description": "New file name without extension."},
                "dry_run": {"type": "boolean", "description": "Only report the plan (default true)."},
            },
            "required": ["project", "file", "new_name"],
        },
    },
    {
        "name": REMOVE_EMPTY_FOLDERS,
        "description": (
            "List truly empty cloud folders (no files, no non-empty subfolders) under a project or a folder "
            "subtree; the start folder itself is never removed. dry_run defaults to true; with dry_run=false "
            "they are deleted children first, each re-checked as still empty just before deletion."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "Project name or id."},
                "folder": {"type": "string", "description": "Only look inside this folder path. Default: project root."},
                "dry_run": {"type": "boolean", "description": "Only list them (default true)."},
            },
            "required": ["project"],
        },
    },
    {
        "name": SKETCH_PROFILE,
        "description": (
            "Build a closed sketch outline safely, or inspect a sketch's profiles. "
            "closed_polyline: new sketch on sketch_plane (xy|xz|yz of the component) or plane_entity_token; "
            "points are [x,y] in sketch space or, with space 'model', [x,y,z] in the component's model space "
            "(mapped with Sketch.modelToSketchSpace; on XZ, sketch y is model -z). The loop closes itself; "
            "every curve shares SketchPoints with its neighbors, so moving a vertex keeps the profile closed. "
            "arcs replace segment i (points i -> i+1) with a 3-point arc via mid_point, or bulge "
            "(DXF tan(sweep/4), + = counterclockwise about the sketch normal, 1 = semicircle). "
            "The outline is refused before anything is created if it self-intersects, repeats a point, "
            "has a zero-length segment, doubles back, encloses no area or (model space) leaves the plane. "
            "Returns sketch and profile entity tokens, warns when more than one profile results, and stores "
            "$sketch_profile_sketch / $sketch_profile_profile for follow-up calls. "
            "profiles: for a sketch by name (in component) or sketch_entity_token, list every profile with "
            "area, centroid (sketch space) and loop counts, the largest_profile, and with point the "
            "profile_containing it. Areas are in units squared. Profile tokens change when the sketch recomputes."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["closed_polyline", "profiles"]},
                "units": {"type": "string", "enum": ["in", "cm", "mm"],
                          "description": "Units of every coordinate in and out."},
                "space": {"type": "string", "enum": ["sketch", "model"],
                          "description": "Coordinate space of points/mid_point/point (default sketch)."},
                "sketch_plane": {"type": "string", "enum": ["xy", "xz", "yz"],
                                 "description": "closed_polyline: construction plane of the component."},
                "plane_entity_token": {"type": "string",
                                       "description": "closed_polyline: construction plane or planar face token."},
                "component": {"type": "string", "description": "Component name (default root)."},
                "points": {"type": "array", "items": _COORD_SCHEMA,
                           "description": "closed_polyline: 2-500 outline vertices; the loop closes itself."},
                "arcs": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "index": {"type": "integer", "minimum": 0, "description": "Segment to replace."},
                            "bulge": {"type": "number"},
                            "mid_point": {**_COORD_SCHEMA, "description": "A point on the arc between its ends."},
                        },
                        "required": ["index"],
                    },
                },
                "name": {"type": "string",
                         "description": "closed_polyline: new sketch name (must be unused). profiles: sketch to inspect."},
                "sketch_entity_token": {"type": "string", "description": "profiles: sketch to inspect."},
                "point": {**_COORD_SCHEMA, "description": "profiles: report which profile contains this point."},
            },
            "required": ["action", "units"],
        },
    },
    {
        "name": CHECK_SOLIDS,
        "description": (
            "Geometry sanity check to run after modeling features (read-only). Checks: validity "
            "(isSolid, volume > 0, faces below min_face_area, edges below min_edge_length), slivers "
            "(long thin faces whose mean width 2*area/perimeter is below sliver_width, and bodies whose "
            "face count is far above the median of the checked bodies: twisted lofts), lumps (several "
            "lumps or extra shells in one body: union/base-feature artifacts), interference (overlap "
            "volume per body pair) and volume_change (diff against a baseline). Default checks: all "
            "except interference (passing interference_pairs adds it). Default bodies: every visible "
            "body in the active design; non-solid bodies are flagged and skipped by interference. "
            "volume_change: a call without baseline_token snapshots every body in the design (hidden "
            "too) and returns baseline_token; call again after the feature with that token to list "
            "bodies whose volume or face count changed, appeared or disappeared. Pass edited_component "
            "(component name or occurrence path) so changes elsewhere, such as a cut that also removed "
            "material from another component, are flagged as outside_edited_component. Result: ok, "
            "summary (counts by severity and type) first, then problems sorted by severity and capped "
            "at max_details. Thresholds are in the chosen units (defaults: 1e-4 in^2, 0.001 in, "
            "0.002 in, 1e-6 in^3)."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "bodies": {"type": "array", "items": BODY_REF_SCHEMA,
                           "description": "Only check these bodies (hidden ones included; at most 500). "
                                          "Default: all visible bodies."},
                "checks": {"type": "array",
                           "items": {"type": "string",
                                     "enum": ["validity", "slivers", "lumps", "interference", "volume_change"]}},
                # No "type": the value is either the string "all" or a list of pairs
                # (the owned validator has no anyOf); the handler checks the shape.
                "interference_pairs": {
                    "description": "'all' (every pair of checked solid bodies, the default) or a list of pairs, "
                                   "each a list of two body references. Limited to 300 pairs.",
                    "items": {"type": "array", "items": BODY_REF_SCHEMA},
                },
                "baseline_token": {"type": "string",
                                   "description": "Token from an earlier check_solids call; reports changes since then."},
                "snapshot": {"type": "boolean",
                             "description": "Store a new baseline and return its token (default: true when no baseline_token)."},
                "edited_component": {"type": "string",
                                     "description": "Component name or occurrence path the caller edited; "
                                                    "changes outside it are highlighted."},
                "units": {"type": "string", "enum": ["in", "cm", "mm"], "description": "Report and threshold units (default in)."},
                "min_face_area": {"type": "number", "minimum": 1e-12},
                "min_edge_length": {"type": "number", "minimum": 1e-12},
                "sliver_width": {"type": "number", "minimum": 1e-12},
                "sliver_aspect": {"type": "number", "minimum": 1e-12,
                                  "description": "Minimum length/width for a thin face to count as a sliver (default 50)."},
                "face_count_factor": {"type": "number", "minimum": 1e-12,
                                      "description": "Flag bodies with more than this times the median face count (default 5)."},
                "min_overlap_volume": {"type": "number", "minimum": 1e-12},
                "max_details": {"type": "integer", "minimum": 1, "maximum": 500,
                                "description": "Maximum problem rows returned (default 50)."},
            },
        },
    },
    {
        "name": BRIDGE_INFO,
        "description": (
            "Report which add-in code is running versus what is on disk: running commit, load time, "
            "reload generation and last reload outcome; disk commit and source files changed since load "
            "(up_to_date is false when a reload would pick up new code); loaded modules by name relative "
            "to the add-in package (e.g. fusion_bridge.bodies) with file paths. "
            "Read-only; use after reload_addin to confirm the new code is live."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": RELOAD_ADDIN,
        "description": (
            "Restart this add-in in place so code changed on disk takes effect, without the user using "
            "Utilities > Add-Ins. Replies immediately, then stops the server, re-imports the add-in's "
            "modules and starts it again; the connection drops for a few seconds. Afterwards call "
            "bridge_info: generation is one higher and last_reload.status is 'reloaded'. If the new code "
            "fails, the previous code is started again (status 'restored', error included). Re-list tools "
            "afterwards: the server announces tools/list_changed on new GET /mcp streams, but clients "
            "may keep a cached list. Resets session state: stored $objects, execute_python sessions and "
            "annotations."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": TAIL_LOG,
        "description": (
            "Read Fusion's own application log (newest AppLogFile*.log by modification time). Runs "
            "outside the main thread and never touches the Fusion API, so it works while Fusion is "
            "busy or blocked by a dialog. Returns the newest matching entries, oldest first, each "
            "'<yyyymmddThhmmss local> <level I/W/E/D> <thread> <message>' with continuation lines "
            "indented. Filters combine (all must match): contains (any of the substrings, "
            "case-insensitive), level (letters, e.g. \"WE\"), since (ISO local time or relative "
            "30s/10m/2h/1d). Presets: save = save/upload workflow lines (a 'Workflow start: save' with "
            "no '[CFSUploader] Uploaded document' after it means the save did not land); errors = "
            "levels W and E. file: latest (default), list (log files with sizes/mtimes) or a file "
            "name from list. The add-in's own error-level messages are written to this log too."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "lines": {"type": "integer", "minimum": 1, "maximum": 1000,
                          "description": "Maximum entries to return (default 50)."},
                "contains": {"type": "array", "items": {"type": "string"},
                             "description": "Keep entries containing any of these substrings (case-insensitive)."},
                "level": {"type": "string",
                          "description": "Keep only these levels: any of I, W, E, D (e.g. \"E\" or \"WE\")."},
                "since": {"type": "string",
                          "description": "Only entries at or after this time: ISO (2026-11-04T17:08) or relative (10m, 2h)."},
                "file": {"type": "string",
                         "description": "latest (default), list, or a file name returned by list."},
                "preset": {"type": "string", "enum": ["save", "errors"],
                           "description": "save: save/upload workflow lines; errors: levels W and E."},
            },
        },
    },
    {
        "name": SAVE_DOCUMENT,
        "description": (
            "Save a document as a new cloud version with a version comment, and VERIFY it landed: "
            "Document.save() can return True while nothing uploads. Polls until the document is no "
            "longer modified AND the DataFile's latest version number has gone up (bounded by "
            "timeout_seconds). Targets the active document unless name or data_file_id is given. "
            "Refuses up front, without saving, a read-only document or an instance that is not the "
            "newest loaded version of a file open more than once. On failure reports the open "
            "instances of the file (name, visibility, loaded version, modified), any '(1)' title "
            "suffix, the read-only flag and the matching lines from Fusion's app log, plus the fix. "
            "Never closes documents."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "description": {"type": "string",
                                "description": "Version comment recorded with the new version."},
                "name": {"type": "string", "description": "Open document name ('Frame v3') or DataFile name ('Frame')."},
                "data_file_id": {"type": "string", "description": "DataFile id of an open document."},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 110,
                                    "description": "How long to wait for the new version (default 60, max 110)."},
            },
            "required": ["description"],
        },
    },
    {
        "name": OPEN_DOCUMENT,
        "description": (
            "Open a cloud file and make it active, reusing an open instance instead of loading a "
            "second copy (a duplicate in-memory instance makes saves silently stall). Give "
            "data_file_id, or project (name or id) plus path ('Folder/Sub/File name'). Optional "
            "version opens that version; default is the latest. If an open document already has "
            "that file at that version it is activated (reused: true). If only another version is "
            "open, the requested one is opened and a warning says two instances now exist. Returns "
            "the resulting document's status and every open instance of the file."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "data_file_id": {"type": "string", "description": "DataFile id (lineage id)."},
                "project": {"type": "string", "description": "Project name or id (with path)."},
                "path": {"type": "string", "description": "Folder path and file name inside the project, e.g. 'Chassis/Front/Knuckle'."},
                "version": {"type": "integer", "minimum": 1, "description": "Version number to open (default latest)."},
            },
        },
    },
    {
        "name": DELETE_FILES,
        "description": (
            "Permanently delete cloud files (every version), parents before the files they link. dry_run defaults "
            "to true and returns the order, set sizes and every outside link; dry_run=false also needs confirm=true. "
            "The whole call is refused, deleting nothing, if a file is open in Fusion, is linked from outside the "
            "set, or a path is missing or ambiguous. Each file is re-checked just before deletion and errors are "
            "per file, with the server's reason from Fusion's app log. Projects cannot be deleted: DataProject has "
            "no delete in the Fusion API."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "Project name or id."},
                "files": {
                    "type": "array", "items": {"type": "string"},
                    "description": "File paths inside the project ('folder/sub/name') or ids ('urn:...').",
                },
                "folder": {
                    "type": "string",
                    "description": "Delete every file directly in this folder path; subfolders are not included.",
                },
                "include_empty_folder": {
                    "type": "boolean",
                    "description": "Remove 'folder' afterwards if it ends up empty (default false).",
                },
                "dry_run": {"type": "boolean", "description": "Only report the plan (default true)."},
                "confirm": {"type": "boolean", "description": "Must be true when dry_run is false."},
            },
            "required": ["project"],
        },
    },
    {
        "name": SET_EDITABLE_STATE,
        "description": (
            "Make cloud files read-only or editable, the API having no setter: runs Fusion's Make Read-Only / "
            "Make Editable command on each file in turn. dry_run defaults to true and only reports the plan. "
            "Per file: skip if already there, reuse an open instance or open it, wait for Fusion to go quiet, run "
            "the command, verify through a fresh DataFile lookup, close the document only if this call opened it. "
            "The starting document is re-activated at the end. Refuses a document with unsaved changes (never "
            "discards) and, for 'editable', files beyond the free Personal Use slots. Stops at the first failure. "
            "Each file gets about 25 s and the call stays under 120 s; files not reached come back as "
            "'not_attempted' (ids in remaining_files) so you can call again. Per-file status: changed, already, "
            "skipped, failed, not_attempted. On a timeout the command may still run later, so the target stays "
            "active (command_pending); Make Editable may have opened a dialog: check fusion_dialogs. If this call "
            "itself times out, a dialog is the likely cause too."
        ),
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "1 to 10 files: DataFile ids ('urn:...'), or paths inside 'project' "
                                         "('Folder/Sub/Name')."},
                "project": {"type": "string",
                            "description": "Project (name or id) the paths are in. Required for paths."},
                "state": {"type": "string", "enum": ["read_only", "editable"], "description": "State to set."},
                "dry_run": {"type": "boolean", "description": "Only report the plan (default true)."},
            },
            "required": ["files", "state"],
        },
    },
]

_TOOL_NAMES = {t["name"] for t in TOOL_DEFINITIONS}


def build_tool_handlers(
    *,
    generic_api_call,
    run_python,
    capture_viewport,
    get_viewport,
    set_viewport,
    fetch_api_documentation,
    fetch_online_documentation,
    fetch_design_guide,
    save_script,
    load_script,
    list_scripts,
    delete_script,
    get_active_selection,
    manage_annotations,
    compute,
    document_versions,
    restore_version,
    document_status,
    activate_document,
    timeline_health,
    fusion_dialogs,
    delete_features,
    list_bodies,
    describe_body,
    parameters,
    editable_files,
    project_tree,
    move_files,
    rename_folder,
    rename_file,
    remove_empty_folders,
    sketch_profile,
    check_solids,
    bridge_info,
    reload_addin,
    tail_log,
    save_document,
    open_document,
    delete_files,
    set_editable_state,
):
    """Build a dict mapping tool name to handler function.

    All tool names must have a corresponding handler. A RuntimeError
    is raised if the handler keys don't match TOOL_DEFINITIONS.
    """
    handlers = {
        CALL_AUTODESK_API: generic_api_call,
        EXECUTE_PYTHON: run_python,
        CAPTURE_VIEWPORT: capture_viewport,
        GET_VIEWPORT: get_viewport,
        SET_VIEWPORT: set_viewport,
        FETCH_API_DOCUMENTATION: fetch_api_documentation,
        FETCH_ONLINE_DOCUMENTATION: fetch_online_documentation,
        FETCH_DESIGN_GUIDE: fetch_design_guide,
        SAVE_SCRIPT: save_script,
        LOAD_SCRIPT: load_script,
        LIST_SCRIPTS: list_scripts,
        DELETE_SCRIPT: delete_script,
        GET_ACTIVE_SELECTION: get_active_selection,
        MANAGE_ANNOTATIONS: manage_annotations,
        COMPUTE: compute,
        DOCUMENT_VERSIONS: document_versions,
        RESTORE_VERSION: restore_version,
        DOCUMENT_STATUS: document_status,
        ACTIVATE_DOCUMENT: activate_document,
        TIMELINE_HEALTH: timeline_health,
        FUSION_DIALOGS: fusion_dialogs,
        DELETE_FEATURES: delete_features,
        LIST_BODIES: list_bodies,
        DESCRIBE_BODY: describe_body,
        PARAMETERS: parameters,
        EDITABLE_FILES: editable_files,
        PROJECT_TREE: project_tree,
        MOVE_FILES: move_files,
        RENAME_FOLDER: rename_folder,
        RENAME_FILE: rename_file,
        REMOVE_EMPTY_FOLDERS: remove_empty_folders,
        SKETCH_PROFILE: sketch_profile,
        CHECK_SOLIDS: check_solids,
        BRIDGE_INFO: bridge_info,
        RELOAD_ADDIN: reload_addin,
        TAIL_LOG: tail_log,
        SAVE_DOCUMENT: save_document,
        OPEN_DOCUMENT: open_document,
        DELETE_FILES: delete_files,
        SET_EDITABLE_STATE: set_editable_state,
    }
    if set(handlers) != _TOOL_NAMES:
        raise RuntimeError(f"Handler registry mismatch: {set(handlers) ^ _TOOL_NAMES}")
    return handlers
