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

# Resource constants
RESOURCE_URI = "fusion://design-guide"
RESOURCE_NAME = "Autodesk Fusion Design Guide"
RESOURCE_DESCRIPTION = "Workflow guidance, API patterns, naming rules, and modeling habits for Autodesk Fusion."

STANDARD_VIEWS = ["front", "back", "left", "right", "top", "bottom", "isometric",
                  "iso_top_left", "iso_top_right", "iso_bottom_left", "iso_bottom_right"]
PROJECTIONS = ["orthographic", "perspective", "perspective_with_ortho_faces"]
CLEAN_VIEW_CATEGORIES = ["sketches", "construction", "origin", "joints", "canvases", "decals"]
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
            "to the full SDK. Variables persist across calls within the same session_id."
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
                    "description": "Whether to persist session variables (default: true)",
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
            "common on Personal Use licences) or has unsaved changes. Only the active document "
            "reads reliably; switch with activate_document."
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
    }
    if set(handlers) != _TOOL_NAMES:
        raise RuntimeError(f"Handler registry mismatch: {set(handlers) ^ _TOOL_NAMES}")
    return handlers
