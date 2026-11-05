"""Boolean existing bodies and hand back fresh references: combine_bodies.

Fusion's references do not outlive an edit: a body wrapper from before a base
feature's ``finishEdit`` is dead and a combine fails on it. So nothing here is
held between calls. Each body is found again from its handle, in the same
main-thread call as the add: a token if it still names a valid body, else
occurrence path, name and index. Afterward the bodies are read from
``feature.bodies``, because a cut can leave several and the target wrapper
names at most one of them.

``keep_tools`` is required: Fusion's default consumes the tools, which nobody
notices until a tube they cut a gusset with has gone. The same preview serves a
dry run and a build. Temporary copies say what should come out, and the built
volume is compared with it, so a boolean that went somewhere else is ``suspect``
rather than ``built``.

Everything above ``FusionCombine`` takes duck-typed collaborators, so fakes can
drive it. Internal values are cm.
"""

import traceback

import adsk.core
import adsk.fusion

from .common import feature_ops as fo
from .common import geometry
from .common.api import safe, strict_items
from .common.args import bool_arg, error_pair, reject_unknown, require_object, text_arg
from .common.bodies import (
    ROOT_PATH,
    VOLUME_ABS_TOLERANCE_CM3,
    bbox_overlap,
    label,
    native,
    parse_body_ref,
    resolve_targets,
    temp_copy,
    volume_of,
)
from .common.entity_refs import FusionScene, body_ref, same_entity
from .common.geometry import UNIT_FACTORS
from .common.results import error_result
from .dispatch import get_app, log

_KNOWN = {"target", "tools", "operation", "keep_tools", "feature_name", "result_name", "on_collateral", "units",
          "dry_run"}

OPERATIONS = {"join": "JoinFeatureOperation", "cut": "CutFeatureOperation", "intersect": "IntersectFeatureOperation"}
COLLATERAL = ("undo", "keep")
UNION, DIFFERENCE, INTERSECTION = "UnionBooleanType", "DifferenceBooleanType", "IntersectionBooleanType"
_BOOLEANS = {"join": UNION, "cut": DIFFERENCE, "intersect": INTERSECTION}

_DIGITS = 6
# Kernel volumes drift on a large body, so a preview may also differ by this fraction of it.
PREVIEW_REL = 1e-6
STALE_ENTITY_REFS = "EntityRefs on the target may be stale after this combine; find them again"


def to_units(value_cm, units, power=1):
    return geometry.to_units(value_cm, units, power, _DIGITS)


# -- Argument validation --------------------------------------------------------


def _handle(value, where):
    """A BodyRef with an optional token, as a dict; raises ValueError."""
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a BodyRef object")
    token = value.get("token")
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise ValueError(f"{where} 'token' must be a non-empty string")
    path, name, index = parse_body_ref({k: v for k, v in value.items() if k != "token"}, where)
    return {"path": path, "name": name, "index": index, "token": token}


def _same_handle(a, b):
    return (a["path"], a["name"], a["index"]) == (b["path"], b["name"], b["index"])


@error_pair
def validate_args(arguments):
    """Return (options, error_text)."""
    require_object(arguments)
    reject_unknown(arguments, _KNOWN)
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    for key in ("target", "tools", "operation", "keep_tools"):
        if key not in arguments:
            raise ValueError(f"'{key}' is required" + (": Fusion's default silently consumes the tools"
                                                       if key == "keep_tools" else ""))
    if arguments["operation"] not in OPERATIONS:
        raise ValueError(f"'operation' must be one of {sorted(OPERATIONS)}, got {arguments['operation']!r}")
    on_collateral = arguments.get("on_collateral", "undo")
    if on_collateral not in COLLATERAL:
        raise ValueError(f"'on_collateral' must be one of {list(COLLATERAL)}, got {on_collateral!r}")
    if not isinstance(arguments["tools"], list) or not arguments["tools"]:
        raise ValueError("'tools' must be a non-empty array of BodyRefs")
    target = _handle(arguments["target"], "'target'")
    tools = [_handle(tool, "each 'tools' entry") for tool in arguments["tools"]]
    if any(_same_handle(target, tool) for tool in tools):
        raise ValueError("the target is among the tools")
    if any(_same_handle(tool, other) for i, tool in enumerate(tools) for other in tools[:i]):
        raise ValueError("a tool is listed twice")
    return {
        "target": target, "tools": tools, "operation": arguments["operation"],
        "keep_tools": bool_arg(arguments, "keep_tools"), "on_collateral": on_collateral, "units": units,
        "feature_name": text_arg(arguments, "feature_name", blank_ok=True) or None,
        "result_name": text_arg(arguments, "result_name", blank_ok=True) or None,
        "dry_run": bool_arg(arguments, "dry_run", False),
    }


# -- Finding the bodies ---------------------------------------------------------


def pick_body(entries, handle, by_token):
    """Find one body now. Returns (entry, resolved_by, warnings, problem).

    A token counts only if it names a valid body, and only on the occurrence the
    handle states: proxies are matched through their native body, because a
    token can come back native. Anything else falls to path, name and index.
    """
    if handle["token"]:
        alive = [body for body in by_token(handle["token"]) if safe(body, "isValid", True) is not False]
        hits = [e for e in entries if e["occurrence_path"] == handle["path"]
                and any(same_entity(native(body), native(e["_body"])) for body in alive)]
        if len(hits) == 1:
            renamed = hits[0]["body"] != handle["name"]
            return hits[0], "token", [f"{handle['name']!r} is called {hits[0]['body']!r} now"] if renamed else [], None
    targets, _, problem = resolve_targets(entries, [(handle["path"], handle["name"], handle["index"])])
    if problem:
        return None, None, [], problem["error"]
    stale = [f"the token for {label(targets[0])} no longer names it; found by path and name"] if handle["token"] else []
    return targets[0], "name", stale, None


def pick_all(entries, opts, by_token):
    """Returns (target, tools, resolved rows, warnings, refusals); a miss leaves target or tools None."""
    picked, resolved, warnings, refusals = [], [], [], []
    for role, handle in [("target", opts["target"])] + [("tool", tool) for tool in opts["tools"]]:
        entry, how, notes, problem = pick_body(entries, handle, by_token)
        warnings += notes
        if problem:
            refusals.append(problem)
        else:
            picked.append(entry)
            resolved.append({"role": role, "kind": "body", "resolved_by": how, "ref": body_ref(entry)})
    if refusals:
        return None, None, resolved, warnings, refusals
    return picked[0], picked[1:], resolved, warnings, []


def body_refusals(target, tools):
    """What no combine can do with these bodies. A token can reach what the handles did not say."""
    bodies = [target] + tools
    refusals = []
    if any(tool is target for tool in tools):
        refusals.append(f"the target is among the tools: {label(target)}")
    if any(tool is other for i, tool in enumerate(tools) for other in tools[:i]):
        refusals.append("two tool references name the same body")
    surfaces = [label(e) for e in bodies if not e["solid"]]
    if surfaces:
        refusals.append(f"not solid, so a boolean has no volume to work with: {surfaces}")
    occurrences = sorted({e["occurrence_path"] for e in bodies})
    if len(occurrences) > 1:
        refusals.append(f"the bodies are in different occurrences: {occurrences}. A combine here needs them in one")
    return refusals


# -- Preview --------------------------------------------------------------------


def _lumps(body):
    return len(strict_items(safe(body, "lumps")))


def preview(copies, target, tools, operation):
    """What the combine should give, on temporary copies: volume, lumps, and each tool's overlap.

    A boolean modifies its target, so every step starts from a fresh copy.
    Raises RuntimeError when one fails.
    """
    def apply(body, tool, kind):
        if not copies.combine(body, copies.fresh(tool), kind):
            raise RuntimeError(f"the {kind} with {label(tool)} failed")

    overlaps = []
    for tool in tools:
        volume = 0.0
        if bbox_overlap(target["bbox"], tool["bbox"]):
            body = copies.fresh(target)
            apply(body, tool, INTERSECTION)
            volume = volume_of(body)
        overlaps.append(volume)
    result = copies.fresh(target)
    for tool in tools:
        apply(result, tool, _BOOLEANS[operation])
    return {"volume": volume_of(result), "lumps": _lumps(result), "overlaps": overlaps}


def preview_refusals(shown, tools, operation):
    """A cut or intersect that cannot do what was asked. Each tool must overlap the target."""
    if operation == "join":
        return []
    misses = [label(tool) for tool, volume in zip(tools, shown["overlaps"]) if volume <= VOLUME_ABS_TOLERANCE_CM3]
    if misses:
        return [f"{operation}: these tools do not overlap the target: {misses}"]
    if shown["volume"] <= VOLUME_ABS_TOLERANCE_CM3:
        return [f"{operation} would leave nothing of the target"]
    return []


def preview_matches(expected, built, target_volume):
    return abs(expected - built) <= max(VOLUME_ABS_TOLERANCE_CM3, PREVIEW_REL * abs(target_volume or 0.0))


def disjoint_warning(operation, lumps_before, lumps_after):
    if operation == "join" and None not in (lumps_before, lumps_after) and lumps_after > lumps_before:
        return (f"disjoint_join: the join gained lumps ({lumps_before} to {lumps_after}), so the target and a "
                "tool do not touch")
    return None


def _plan(target, tools, opts, shown):
    units = opts["units"]
    plan = {"operation": opts["operation"], "target": body_ref(target), "tools": [body_ref(t) for t in tools],
            "keep_tools": opts["keep_tools"], "component": target["component"],
            "would_consume": [] if opts["keep_tools"] else [body_ref(t) for t in tools],
            "preview_volume": None, "lumps": None, "overlaps": None}
    if shown:
        plan.update(preview_volume=to_units(shown["volume"], units, 3), lumps=shown["lumps"],
                    overlaps=[{"ref": body_ref(tool), "volume": to_units(volume, units, 3)}
                              for tool, volume in zip(tools, shown["overlaps"])])
    return plan


# -- After the add --------------------------------------------------------------


def _entry_of(body, entries, path):
    return next((e for e in entries if e["occurrence_path"] == path
                 and same_entity(native(body), native(e["_body"]))), None)


def _name_results(bodies, name, warnings):
    """Largest first takes *name*, the rest ' 2', ' 3'. Fusion may adjust a name it already holds."""
    ordered = sorted(bodies, key=lambda body: -(safe(body, "volume") or 0.0))
    for i, body in enumerate(ordered):
        wanted = name if i == 0 else f"{name} {i + 1}"
        try:
            body.name = wanted
        except RuntimeError as exc:
            warnings.append(f"could not name a result body {wanted!r}: {exc}")
            continue
        if safe(body, "name") != wanted:
            warnings.append(f"a result body asked to be {wanted!r} is {safe(body, 'name')!r}")


def result_rows(bodies, entries, path, units):
    """A row per body the feature left, volume largest first."""
    rows = []
    for body in sorted(bodies, key=lambda b: -(safe(b, "volume") or 0.0)):
        entry = _entry_of(body, entries, path)
        ref = body_ref(entry) if entry else {"occurrence_path": path, "body_name": safe(body, "name")}
        source = entry["_body"] if entry else body
        rows.append({"ref": ref, "token": safe(source, "entityToken"),
                     "volume": to_units(safe(source, "volume"), units, 3),
                     "lumps": safe(safe(source, "lumps"), "count"), "faces": safe(safe(source, "faces"), "count")})
    return rows


def _tool_checks(tools, before, after, after_entries, keep):
    """Did the tools end up where keep_tools said? Returns (kept refs, problems, notes)."""
    keys = [tool["key"] for tool in tools]
    kept_keys = set(fo.unchanged(before, after, keys))
    gone = [key for key in keys if key not in after]
    problems, notes = [], []
    if keep:
        lost = [key for key in keys if key not in kept_keys]
        if lost:
            problems.append(f"keep_tools is true but these tools changed or went: {lost}")
    elif len(gone) != len(keys):
        problems.append(f"keep_tools is false but these tools remain: {[k for k in keys if k not in gone]}")
    now = {e["key"]: e for e in after_entries}
    for tool in tools:
        match = now.get(tool["key"])
        if match is not None and match["visible"] != tool["visible"]:
            notes.append(f"{tool['key']} went from visible={tool['visible']} to visible={match['visible']}")
    return [body_ref(tool) for tool in tools if tool["key"] in kept_keys], problems, notes


def _rename_in(changes, old, new):
    for row in changes["touched"]:
        if row["ref"] == old:
            row["ref"] = new


def finish(design, session, outcome, job, warnings):
    """Everything after a successful add: collateral, checks, names, the result.

    *job* holds what the add was given: ``target``, ``tools``, ``opts``, ``before``,
    ``token``, ``shown``, ``resolved`` and ``lumps_before``. Its wrappers predate
    the feature, so only their records are read here.
    """
    feature, health = outcome["feature"], outcome["health"]
    target, tools, opts, before, shown = job["target"], job["tools"], job["opts"], job["before"], job["shown"]
    token, resolved, units = job["token"], job["resolved"], opts["units"]
    keys = [target["key"]] + [tool["key"] for tool in tools]
    after = fo.snapshot(design)
    changes = fo.body_changes(before, after, keys, units)
    row = fo.feature_row(feature, health)

    lost = fo.lost_outside(before, after, keys)
    if lost and opts["on_collateral"] == "undo":
        undone = fo.undo(design, feature, before)
        return fo.envelope("undone" if undone["undone"] else "refused", units, feature=row, bodies=changes,
                           baseline_token=token, resolved=resolved, collateral=lost,
                           warnings=warnings + [f"bodies nobody named lost volume or went: {lost}"]
                           + undone["warnings"])

    bodies = strict_items(safe(feature, "bodies"))
    problems = [f"bodies nobody named lost volume or went: {lost}"] if lost else []
    if not bodies:
        problems.append("the feature reports no bodies")
    if target["key"] in fo.unchanged(before, after, [target["key"]]):
        problems.append(f"the target did not change: {target['key']}")

    entries = session.fresh_entries()
    tools_kept, tool_problems, notes = _tool_checks(tools, before, after, entries, opts["keep_tools"])
    problems += tool_problems
    warnings = warnings + notes

    # Names last: a rename would read as a consumed body in the diff above, and an undo must not need it.
    if opts["feature_name"]:
        try:
            feature.name = opts["feature_name"]
            row = fo.feature_row(feature, health)
        except RuntimeError as exc:
            warnings.append(f"could not name the feature: {exc}")
    stale = [item["ref"] for item in changes["consumed"]]
    path = target["occurrence_path"]
    if opts["result_name"] and bodies:
        old = body_ref(target)
        _name_results(bodies, opts["result_name"], warnings)
        entries = session.fresh_entries()
        fresh = _entry_of(max(bodies, key=lambda b: safe(b, "volume") or 0.0), entries, path)
        if fresh is not None and fresh["body"] != target["body"]:
            _rename_in(changes, old, body_ref(fresh))
            if old not in stale:
                stale.append(old)

    rows = result_rows(bodies, entries, path, units)
    built_volume = sum(safe(body, "volume") or 0.0 for body in bodies)
    matched = None
    if shown:
        matched = preview_matches(shown["volume"], built_volume, before[target["key"]]["volume"])
        if not matched:
            problems.append(f"built volume {to_units(built_volume, units, 3)} disagrees with the preview "
                            f"{to_units(shown['volume'], units, 3)}")
    lumps = sum(item["lumps"] or 0 for item in rows) if rows else None
    disjoint = disjoint_warning(opts["operation"], job["lumps_before"], lumps)
    if disjoint:
        warnings.append(disjoint)
    others = [item["ref"] for item in changes["touched"] if item["role"] == "other_instance"]
    if others:
        warnings.append(f"other instances of this component changed with it: {others}")

    return fo.envelope(
        "suspect" if problems else "built", units, feature=row, bodies=changes, baseline_token=token,
        resolved=resolved, warnings=problems + warnings + [STALE_ENTITY_REFS],
        result={"bodies": rows, "preview_volume": to_units(shown["volume"], units, 3) if shown else None,
                "preview_matched": matched},
        tools_kept=tools_kept, stale_refs=stale)


def run(design, document, opts, session):
    """One combine: find, refuse, preview, then add unless this is a dry run."""
    units, dry_run = opts["units"], opts["dry_run"]
    target, tools, resolved, warnings, refusals = pick_all(session.entries(), opts, session.by_token)
    if target is not None:
        refusals += body_refusals(target, tools)
    blocked, notes = fo.preflight(design, document, [target] + tools if target is not None else ())
    refusals += blocked
    warnings += notes
    if refusals:
        return fo.refused(units, refusals, dry_run=dry_run, resolved=resolved, warnings=warnings)

    warnings += [f"{label(e)} is hidden; hidden bodies are often stale" for e in [target] + tools if not e["visible"]]
    try:
        shown = preview(session, target, tools, opts["operation"])
    except RuntimeError as exc:
        shown = None
        warnings.append(f"no preview, so nothing to compare the result with: {exc}")
    if shown:
        refusals = preview_refusals(shown, tools, opts["operation"])
        if refusals:
            return fo.refused(units, refusals, dry_run=dry_run, resolved=resolved, warnings=warnings,
                              plan=_plan(target, tools, opts, shown))
    lumps_before = safe(safe(target["_body"], "lumps"), "count")
    if dry_run:
        disjoint = disjoint_warning(opts["operation"], lumps_before, shown["lumps"]) if shown else None
        return fo.envelope("planned", units, dry_run=True, resolved=resolved,
                           warnings=warnings + ([disjoint] if disjoint else []),
                           plan=_plan(target, tools, opts, shown))

    before, token = fo.baseline(design)
    outcome = fo.add_feature(lambda: session.add(target, tools, opts))
    warnings += outcome["warnings"]
    if outcome["status"] == "refused":
        return fo.refused(units, [outcome["refusal"]], resolved=resolved, warnings=warnings, baseline_token=token)
    job = {"target": target, "tools": tools, "opts": opts, "before": before, "token": token, "shown": shown,
           "resolved": resolved, "lumps_before": lumps_before}
    return finish(design, session, outcome, job, warnings)


# -- Fusion layer ---------------------------------------------------------------


def _feature_input(entry):
    """What the add takes: a sub-component's features work on its native bodies."""
    return entry["_body"] if entry["occurrence_path"] == ROOT_PATH else native(entry["_body"])


class FusionCombine(FusionScene):
    """``FusionScene`` plus the temporary copies and the add. Lives for one call."""

    def __init__(self, design):
        super().__init__(design)
        self._manager = adsk.fusion.TemporaryBRepManager.get()

    def fresh_entries(self):
        return FusionScene(self._design).entries()

    def fresh(self, entry):
        return temp_copy(self._manager, entry["_body"])

    def combine(self, target, tool, kind):
        return self._manager.booleanOperation(target, tool, getattr(adsk.fusion.BooleanTypes, kind))

    def add(self, target, tools, opts):
        collection = adsk.core.ObjectCollection.create()
        for tool in tools:
            collection.add(_feature_input(tool))
        combines = target["_body"].parentComponent.features.combineFeatures
        entity = combines.createInput(_feature_input(target), collection)
        entity.operation = getattr(adsk.fusion.FeatureOperations, OPERATIONS[opts["operation"]])
        entity.isKeepToolBodies = opts["keep_tools"]
        return combines.add(entity)


def combine_bodies(arguments):
    """Combine bodies of the active design."""
    opts, err = validate_args(arguments)
    if err:
        return error_result(err)
    try:
        app = get_app()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            refusals, _ = fo.preflight(None)
            return fo.write_result(fo.refused(opts["units"], refusals, dry_run=opts["dry_run"]))
        payload = run(design, safe(app, "activeDocument"), opts, FusionCombine(design))
        log(f"[MCP] combine_bodies {opts['operation']}: {payload['status']}")
        return fo.write_result(payload)
    except Exception as exc:
        log(f"[MCP] combine_bodies failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
