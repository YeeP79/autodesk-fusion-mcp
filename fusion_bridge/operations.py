"""MCP tool routing and generic Fusion API dispatch."""

import os
import traceback

import adsk.core

from . import (
    annotations,
    apply_thread,
    audit_design,
    compare_bodies,
    compare_files,
    delete_files,
    app_log,
    bridge_info,
    bodies,
    delete_features,
    dialogs,
    compute,
    doc_lookup,
    document_versions,
    documents,
    parameters,
    editable_files,
    file_ops,
    project_tree,
    document_io,
    editable_state,
    fusion_status,
    document_lifecycle,
    find_entities,
    probe_geometry,
    python_exec,
    reloader,
    script_store,
    selection,
    timeline_health,
    sketch_profile,
    solid_check,
    timeline_insert,
    tool_surface,
    value_builders,
    viewport,
)
from .common.results import error_result, ok_result
from .dispatch import log


# -- Generic API execution --------------------------------------------------


def _execute_api_call(api_path, args, kwargs, remember_as, return_properties):
    """Resolve *api_path*, invoke it with *args*/*kwargs*, and return a
    human-readable summary string."""
    target = value_builders.resolve_path(api_path)
    resolved_args = [value_builders.coerce_arg(a) for a in args]
    resolved_kwargs = {k: value_builders.coerce_arg(v) for k, v in kwargs.items()}

    result = target(*resolved_args, **resolved_kwargs) if callable(target) else target

    if remember_as:
        value_builders.OBJECT_STORE[remember_as] = result

    result_type, summary = value_builders.format_result(result, return_properties)
    message = f"OK: {api_path} -> {result_type}({summary})"
    if remember_as:
        message += f" [stored as '{remember_as}']"
    message += f"\nContext: {len(value_builders.OBJECT_STORE)} objects stored"
    return message


def _format_call_signature(api_path, args, kwargs):
    parts = [repr(a) for a in args]
    if kwargs:
        parts.extend(f"{k}={v!r}" for k, v in kwargs.items())
    return f"{api_path}({', '.join(parts)})"


# -- Per-tool handler: call_autodesk_api (generic API) ------------------------


def handle_generic_api_call(call_data):
    """Handle the call_autodesk_api generic API tool."""
    params = call_data.get("params", {})
    arguments = params.get("arguments", {})
    description = arguments.get("description", "")

    api_path = arguments.get("api_path", "")
    args = arguments.get("args", [])
    kwargs = arguments.get("kwargs", {})
    remember_as = arguments.get("remember_as")
    return_properties = arguments.get("return_properties", [])

    if description and api_path:
        log(f"[MCP] Tool call: call_autodesk_api - {api_path} ({description})")
    elif api_path:
        log(f"[MCP] Tool call: call_autodesk_api - {api_path}")

    # Dispatch special pseudo-paths
    if api_path == "get_pid":
        return ok_result(f"Process ID: {os.getpid()}")

    if api_path == "clear_context":
        cleared = value_builders.clear_object_store()
        return ok_result(f"Cleared {cleared} stored objects")

    try:
        result = _execute_api_call(
            api_path, args, kwargs, remember_as, return_properties
        )
        return ok_result(result)
    except Exception as exc:
        tb = traceback.format_exc()
        error_text = str(exc)
        summary = timeline_health.rollback_summary(error_text)
        if summary:
            error_text = timeline_health.truncate(error_text)
            tb = timeline_health.truncate(tb)
        text = (
            f"Error: {type(exc).__name__}: {error_text}\n"
            f"Call: {_format_call_signature(api_path, args, kwargs)}\n"
            f"Traceback:\n{tb}"
        )
        if summary:
            text = f"{summary}\n\n{text}"
        log(f"Tool call failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        log(tb, adsk.core.LogLevels.ErrorLogLevel)
        return error_result(text)


# -- Wrapper helpers for handlers that take arguments directly ---------------


def _wrap(fn):
    """Wrap a handler that expects an arguments dict to accept call_data envelope."""

    def wrapper(call_data):
        params = call_data.get("params", {})
        arguments = params.get("arguments", {})
        return fn(arguments)

    return wrapper


def _wrap_doc(fn):
    """Wrap doc_lookup handlers that expect (arguments, log_fn)."""

    def wrapper(call_data):
        params = call_data.get("params", {})
        arguments = params.get("arguments", {})
        return fn(arguments, log)

    return wrapper


def _wrap_cancellable(fn):
    """Wrap a handler that also takes the request's cancel event, if any."""

    def wrapper(call_data):
        params = call_data.get("params", {})
        arguments = params.get("arguments", {})
        return fn(arguments, call_data.get("_cancel_event"))

    return wrapper


# -- Handler registry -------------------------------------------------------

TOOL_HANDLERS = tool_surface.build_tool_handlers(
    generic_api_call=handle_generic_api_call,
    run_python=_wrap(python_exec.run_python),
    capture_viewport=_wrap(viewport.capture),
    get_viewport=_wrap(viewport.get_viewport),
    set_viewport=_wrap(viewport.set_viewport),
    fetch_api_documentation=_wrap_doc(doc_lookup.fetch_api_documentation),
    fetch_online_documentation=_wrap_doc(doc_lookup.fetch_online_documentation),
    fetch_design_guide=_wrap_doc(doc_lookup.fetch_design_guide),
    save_script=_wrap(script_store.save_script),
    load_script=_wrap(script_store.load_script),
    list_scripts=_wrap(script_store.list_scripts),
    delete_script=_wrap(script_store.delete_script),
    get_active_selection=_wrap(selection.get_active_selection),
    manage_annotations=_wrap(annotations.manage_annotations),
    timeline_insert=_wrap(timeline_insert.timeline_insert),
    compare_files=_wrap(compare_files.compare_files),
    compare_bodies=_wrap(compare_bodies.compare_bodies),
    audit_design=_wrap(audit_design.audit_design),
    apply_thread=_wrap(apply_thread.apply_thread),
    probe_geometry=_wrap(probe_geometry.probe_geometry),
    find_entities=_wrap(find_entities.find_entities),
    close_document=_wrap(document_lifecycle.close_document),
    update_references=_wrap(document_lifecycle.update_references),
    fusion_status=_wrap_cancellable(fusion_status.fusion_status),
    set_editable_state=_wrap(editable_state.set_editable_state),
    delete_files=_wrap(delete_files.delete_files),
    save_document=_wrap(document_io.save_document),
    open_document=_wrap(document_io.open_document),
    tail_log=_wrap(app_log.tail_log),
    bridge_info=_wrap(bridge_info.bridge_info),
    reload_addin=_wrap(reloader.reload_addin),
    check_solids=_wrap(solid_check.check_solids),
    sketch_profile=_wrap(sketch_profile.sketch_profile),
    project_tree=_wrap(project_tree.project_tree),
    move_files=_wrap(file_ops.move_files),
    rename_folder=_wrap(file_ops.rename_folder),
    rename_file=_wrap(file_ops.rename_file),
    remove_empty_folders=_wrap(file_ops.remove_empty_folders),
    editable_files=_wrap(editable_files.editable_files),
    parameters=_wrap(parameters.parameters),
    list_bodies=_wrap(bodies.list_bodies),
    describe_body=_wrap(bodies.describe_body),
    delete_features=_wrap(delete_features.delete_features),
    fusion_dialogs=_wrap(dialogs.fusion_dialogs),
    document_status=_wrap(documents.document_status),
    activate_document=_wrap(documents.activate_document),
    document_versions=_wrap(document_versions.document_versions),
    restore_version=_wrap(document_versions.restore_version),
    compute=_wrap(compute.compute),
    timeline_health=_wrap(timeline_health.timeline_health),
)
