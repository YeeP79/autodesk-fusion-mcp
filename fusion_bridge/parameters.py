"""List the active design's parameters, show what they drive, and set user parameters.

``Parameter.value`` is in internal units (cm, radians, kg), so values are
formatted through the design's units manager in each parameter's own unit.
Direct dependents and dependents trees come from parsed expressions
(``parameter_graph``). ``Parameter.dependentParameters`` is transitive (live:
``od`` listed ``d7``, which references ``idia``, not ``od``), so it supplies
``all_dependents`` and is checked against the parsed closure, like with like.
Only user parameters can be set: model parameters belong to features and
sketches and are edited there.
"""

import traceback

import adsk.core
import adsk.fusion

from . import parameter_graph as graph
from .common.results import error_result, tool_result
from .dispatch import get_app, log

ACTIONS = ("list", "set")
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
DEFAULT_DEPTH = 5
MAX_DEPTH = 20
MAX_UPDATES = 50
MAX_ROW_DEPENDENTS = 25
MAX_DRIVES = 25
MAX_TREE_NODES = 500
MAX_CLOSURE_NODES = 5000
MAX_MISMATCHES = 20
MAX_TIMELINE_ERRORS = 25

_LIST_ARGS = {"action", "include_model", "name_contains", "comment_contains", "limit", "name", "depth"}
_SET_ARGS = {"action", "updates", "dry_run", "depth"}

COMPUTE_NOTE = (
    "Geometry may not have regenerated yet: run the compute tool (Compute All). "
    "The API recompute does not always match the UI command."
)


# -- Argument validation (no Fusion) -----------------------------------------


def _int_arg(arguments, key, default, low, high):
    value = arguments.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"'{key}' must be an integer from {low} to {high}")
    return value


def _str_arg(arguments, key):
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string")
    return value


def validate_arguments(arguments):
    """Return normalized arguments or raise ValueError with a user-facing message."""
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    action = arguments.get("action", "list")
    if action not in ACTIONS:
        raise ValueError(f"'action' must be one of {list(ACTIONS)}, got {action!r}")
    allowed = _LIST_ARGS if action == "list" else _SET_ARGS
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise ValueError(f"Unexpected argument(s) for action '{action}': {extra}")

    depth = _int_arg(arguments, "depth", DEFAULT_DEPTH, 1, MAX_DEPTH)
    if action == "list":
        include_model = arguments.get("include_model", False)
        if not isinstance(include_model, bool):
            raise ValueError("'include_model' must be a boolean")
        return {
            "action": action,
            "include_model": include_model,
            "name_contains": _str_arg(arguments, "name_contains"),
            "comment_contains": _str_arg(arguments, "comment_contains"),
            "limit": _int_arg(arguments, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT),
            "name": _str_arg(arguments, "name"),
            "depth": depth,
        }

    dry_run = arguments.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ValueError("'dry_run' must be a boolean")
    updates = arguments.get("updates")
    if not isinstance(updates, list) or not updates:
        raise ValueError("'updates' must be a non-empty list of {name, expression}")
    if len(updates) > MAX_UPDATES:
        raise ValueError(f"'updates' may hold at most {MAX_UPDATES} entries")
    normalized = []
    seen = set()
    for index, update in enumerate(updates):
        if not isinstance(update, dict) or set(update) != {"name", "expression"}:
            raise ValueError(f"updates[{index}] must be an object with exactly 'name' and 'expression'")
        name, expression = update["name"], update["expression"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"updates[{index}].name must be a non-empty string")
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError(f"updates[{index}].expression must be a non-empty string")
        if name in seen:
            raise ValueError(f"Parameter {name!r} appears more than once in 'updates'")
        seen.add(name)
        normalized.append({"name": name, "expression": expression})
    return {"action": action, "updates": normalized, "dry_run": dry_run, "depth": depth}


# -- Reading parameters ------------------------------------------------------


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _short_type(obj):
    object_type = _safe(lambda: obj.objectType) or type(obj).__name__
    return object_type.rsplit(":", 1)[-1]


def describe_owner(created_by):
    """``{"type", "name", "sketch"}`` for a model parameter's ``createdBy`` object."""
    if created_by is None:
        return None
    owner = {"type": _short_type(created_by), "name": _safe(lambda: created_by.name)}
    sketch = _safe(lambda: created_by.parentSketch)
    if sketch is not None:
        owner["sketch"] = _safe(lambda: sketch.name)
    return owner


def owner_label(owner):
    """Short label for a feature/sketch that a parameter belongs to."""
    if not owner:
        return None
    if owner.get("sketch"):
        return owner["sketch"]
    return owner.get("name") or owner.get("type")


def format_value(units_manager, param):
    """The parameter's value as Fusion would display it, in its own unit."""
    is_text = _safe(lambda: param.valueType) == getattr(
        getattr(adsk.fusion, "ParameterValueTypes", None), "TextParameterValueType", 1
    )
    if is_text:
        return _safe(lambda: param.textValue)
    value = _safe(lambda: param.value)
    if value is None:
        return None
    unit = _safe(lambda: param.unit) or ""
    if not unit:
        return f"{value:.6g}"
    text = _safe(lambda: units_manager.formatValue(value, unit))
    if text:
        return text
    converted = _safe(lambda: units_manager.convert(value, units_manager.internalUnits, unit))
    if isinstance(converted, (int, float)):
        return f"{converted:.6g} {unit}"
    return None


def _read_param(param, kind, units_manager):
    info = {
        "name": _safe(lambda: param.name),
        "kind": kind,
        "expression": _safe(lambda: param.expression),
        "value": format_value(units_manager, param),
        "unit": _safe(lambda: param.unit) or "",
        "comment": _safe(lambda: param.comment) or "",
        "favorite": bool(_safe(lambda: param.isFavorite, False)),
    }
    if kind == "model":
        info["owner"] = describe_owner(_safe(lambda: param.createdBy))
        info["role"] = _safe(lambda: param.role)
        component = _safe(lambda: param.component)
        info["component"] = _safe(lambda: component.name) if component is not None else None
    return info


def snapshot(design):
    """Read every parameter once: ``(infos, objects, user_order, model_order)``.

    ``infos`` and ``objects`` are keyed by parameter name. ``design.allParameters``
    includes user parameters, which are classified by ``design.userParameters``.
    """
    units_manager = _safe(lambda: design.unitsManager)
    infos, objects, user_order, model_order = {}, {}, [], []
    for param in design.userParameters:
        info = _read_param(param, "user", units_manager)
        if info["name"] is None or info["name"] in infos:
            continue
        infos[info["name"]] = info
        objects[info["name"]] = param
        user_order.append(info["name"])
    for param in design.allParameters:
        name = _safe(lambda: param.name)
        if name is None or name in infos:
            continue
        info = _read_param(param, "model", units_manager)
        infos[name] = info
        objects[name] = param
        model_order.append(name)
    return infos, objects, user_order, model_order


class DependencyIndex:
    """Direct dependents parsed from expressions; all dependents from the API, else parsed."""

    def __init__(self, infos, objects):
        self._infos = infos
        self._objects = objects
        self._parsed = graph.build_dependents({n: i["expression"] or "" for n, i in infos.items()})
        self._cache = {}
        self.api_names = 0
        self.parsed_names = 0

    def dependents_of(self, name):
        """Parameters whose expressions reference ``name`` directly."""
        return self._parsed.get(name, [])

    def closure(self, name):
        """Everything downstream of ``name`` by parsing. Returns ``(names, truncated)``."""
        nodes, truncated = graph.transitive_dependents(name, self.dependents_of, max_depth=MAX_CLOSURE_NODES,
                                                       max_nodes=MAX_CLOSURE_NODES)
        return [node["name"] for node in nodes], truncated

    def api(self, name):
        """Transitive dependents reported by ``dependentParameters``, or None if unavailable."""
        param = self._objects.get(name)
        if param is None:
            return None
        names = _safe(lambda: [p.name for p in param.dependentParameters])
        if names is None:
            return None
        return [n for n in names if n != name]

    def all_dependents(self, name):
        """Everything downstream of ``name``: the API's list, else the parsed closure."""
        if name not in self._cache:
            names = self.api(name)
            if names is None:
                names = self.closure(name)[0]
                self.parsed_names += 1
            else:
                self.api_names += 1
            self._cache[name] = names
        return self._cache[name]

    def dependencies_of(self, name):
        """Parameters ``name`` references: API ``dependencyParameters`` or parsed."""
        param = self._objects.get(name)
        names = _safe(lambda: [p.name for p in param.dependencyParameters]) if param is not None else None
        if names is not None:
            return names, "api"
        expression = self._infos.get(name, {}).get("expression") or ""
        return graph.parameter_references(expression, self._infos, self_name=name), "parsed"

    @property
    def source(self):
        if self.api_names and self.parsed_names:
            return "mixed"
        if self.parsed_names:
            return "parsed"
        return "api"

    def mismatch(self, name):
        """``{"api_only", "parsed_only"}`` when the API's list and the parsed closure differ."""
        api_names = self.api(name)
        closure, truncated = self.closure(name)
        if api_names is None or truncated:
            return None
        api_set, parsed_set = set(api_names), set(closure)
        if api_set == parsed_set:
            return None
        return {"name": name, "api_only": sorted(api_set - parsed_set), "parsed_only": sorted(parsed_set - api_set)}


def _label(infos, name):
    info = infos.get(name)
    owner = owner_label(info.get("owner")) if info else None
    return f"{name} ({owner})" if owner else name


def drives(infos, nodes):
    """Unique owning features/sketches of the model parameters among ``nodes``."""
    labels = []
    for node in nodes:
        info = infos.get(node["name"])
        label = owner_label(info.get("owner")) if info else None
        if label and label not in labels:
            labels.append(label)
    return labels


def _capped(items, cap):
    return {"items": items[:cap], "total": len(items), "truncated": len(items) > cap}


def _matches(info, name_contains, comment_contains):
    if name_contains and name_contains.lower() not in (info["name"] or "").lower():
        return False
    if comment_contains and comment_contains.lower() not in (info["comment"] or "").lower():
        return False
    return True


def build_list(infos, user_order, model_order, index, args):
    """The ``list`` payload (minus document metadata). Pure over its inputs."""
    def section(order, with_dependents):
        matched = [n for n in order if _matches(infos[n], args["name_contains"], args["comment_contains"])]
        rows = []
        for name in matched[: args["limit"]]:
            row = dict(infos[name])
            if with_dependents:
                direct = index.dependents_of(name)
                row["dependents"] = _capped([_label(infos, n) for n in direct], MAX_ROW_DEPENDENTS)
                row["all_dependents"] = _capped([_label(infos, n) for n in index.all_dependents(name)],
                                                MAX_ROW_DEPENDENTS)
                nodes, truncated = graph.transitive_dependents(
                    name, index.dependents_of, max_depth=args["depth"], max_nodes=MAX_TREE_NODES
                )
                drv = drives(infos, nodes)
                row["drives"] = {"items": drv[:MAX_DRIVES], "total": len(drv),
                                 "truncated": truncated or len(drv) > MAX_DRIVES}
            rows.append(row)
        return {"total": len(order), "matched": len(matched), "returned": len(rows),
                "truncated": len(matched) > len(rows), "rows": rows}

    payload = {"user_parameters": section(user_order, True)}
    if args["include_model"]:
        payload["model_parameters"] = section(model_order, False)
    mismatches = [m for m in (index.mismatch(r["name"]) for r in payload["user_parameters"]["rows"]) if m]
    payload["dependency_source"] = index.source
    payload["dependency_check"] = {
        "compared": payload["user_parameters"]["returned"],
        "mismatches": mismatches[:MAX_MISMATCHES],
        "mismatch_count": len(mismatches),
    }
    return payload


def build_detail(infos, index, name, depth):
    """One parameter with what it references and everything downstream of it."""
    info = dict(infos[name])
    dependencies, dep_source = index.dependencies_of(name)
    nodes, truncated = graph.transitive_dependents(name, index.dependents_of, max_depth=depth,
                                                   max_nodes=MAX_TREE_NODES)
    for node in nodes:
        node_info = infos.get(node["name"], {})
        node["kind"] = node_info.get("kind")
        node["expression"] = node_info.get("expression")
        node["owner"] = owner_label(node_info.get("owner"))
    info["dependencies"] = dependencies
    info["dependencies_source"] = dep_source
    info["direct_dependents"] = [_label(infos, n) for n in index.dependents_of(name)]
    info["all_dependents"] = [_label(infos, n) for n in index.all_dependents(name)]
    info["dependents_tree"] = {"depth_cap": depth, "nodes": nodes, "truncated": truncated}
    info["drives"] = drives(infos, nodes)
    info["dependency_source"] = index.source
    mismatch = index.mismatch(name)
    if mismatch:
        info["dependency_check_mismatch"] = mismatch
    return info


# -- Setting user parameters -------------------------------------------------


def check_updates(infos, updates):
    """Refuse the whole batch if any name is unknown or not a user parameter."""
    problems = []
    for update in updates:
        info = infos.get(update["name"])
        if info is None:
            problems.append(f"{update['name']!r}: no such parameter")
        elif info["kind"] != "user":
            owner = owner_label(info.get("owner"))
            where = f" (owned by {owner})" if owner else ""
            problems.append(f"{update['name']!r}: model parameter{where}; only user parameters can be set")
    return problems


def plan_update(infos, index, update, depth, units_manager=None):
    """Dry-run description of one update: old -> new and what it would change."""
    name, expression = update["name"], update["expression"]
    info = infos[name]
    references = graph.parameter_references(expression, infos, self_name=name)
    unknown = [i for i in graph.expression_identifiers(expression) if i not in infos and i != name]
    nodes, truncated = graph.transitive_dependents(name, index.dependents_of, max_depth=depth,
                                                   max_nodes=MAX_TREE_NODES)
    plan = {
        "name": name,
        "old_expression": info["expression"],
        "new_expression": expression,
        "unit": info["unit"],
        "current_value": info["value"],
        "references": references,
        "dependents_affected": {
            "items": [_label(infos, n["name"]) for n in nodes],
            "truncated": truncated,
        },
        "drives": drives(infos, nodes),
    }
    if graph.normalize_expression(expression) == graph.normalize_expression(info["expression"]):
        plan["unchanged"] = True
    if unknown:
        plan["unknown_identifiers"] = unknown
    cycle = graph.would_create_cycle(name, references, index.dependents_of)
    if cycle:
        plan["would_create_cycle"] = cycle
    mismatch = index.mismatch(name)
    if mismatch:
        plan["dependency_check_mismatch"] = mismatch
    if units_manager is not None:
        valid = _safe(lambda: units_manager.isValidExpression(expression, info["unit"]))
        if valid is not None:
            plan["expression_valid"] = bool(valid)
    return plan


def apply_updates(objects, infos, updates, units_manager):
    """Set expressions in order, stopping at the first failure.

    Returns ``(results, failed)``. A set that raises, or one that reads back
    the old expression (Fusion rolls API edits back while the timeline has
    broken features), counts as a failure.
    """
    results = []
    failed = False
    for update in updates:
        name, expression = update["name"], update["expression"]
        old = infos[name]["expression"]
        entry = {"name": name, "old_expression": old, "new_expression": expression}
        if failed:
            entry["status"] = "not_attempted"
            results.append(entry)
            continue
        param = objects[name]
        try:
            param.expression = expression
        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            failed = True
            results.append(entry)
            continue
        after = _safe(lambda: param.expression)
        entry["expression_after"] = after
        entry["value_after"] = format_value(units_manager, param)
        normalized_after = graph.normalize_expression(after)
        if (normalized_after != graph.normalize_expression(expression)
                and normalized_after == graph.normalize_expression(old)):
            entry["status"] = "failed"
            entry["error"] = "Fusion kept the old expression (edits roll back while the timeline has errors)"
            failed = True
        else:
            entry["status"] = "applied"
        results.append(entry)
    return results, failed


def timeline_health(design):
    """Count timeline items by health state; name the ones in error."""
    states = adsk.fusion.FeatureHealthStates
    counts = {"healthy": 0, "warning": 0, "error": 0}
    errors = []
    for item in design.timeline:
        state = item.healthState
        if state == states.ErrorFeatureHealthState:
            counts["error"] += 1
            errors.append(item.name)
        elif state == states.HealthyFeatureHealthState:
            counts["healthy"] += 1
        else:
            counts["warning"] += 1
    return counts, errors


# -- Tool handler ------------------------------------------------------------


def parameters(arguments):
    """List parameters and their dependents, or set user parameter expressions."""
    try:
        args = validate_arguments(arguments)
    except ValueError as exc:
        return error_result(f"Error: {exc}")

    try:
        app = get_app()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return error_result("Error: the active product is not a Fusion design")
        units_manager = _safe(lambda: design.unitsManager)
        infos, objects, user_order, model_order = snapshot(design)
        index = DependencyIndex(infos, objects)
        meta = {
            "document": app.activeDocument.name if app.activeDocument else None,
            "default_length_units": _safe(lambda: units_manager.defaultLengthUnits),
        }

        if args["action"] == "list":
            if args["name"] is not None:
                if args["name"] not in infos:
                    return error_result(f"Error: no parameter named {args['name']!r}")
                return tool_result({**meta, "parameter": build_detail(infos, index, args["name"], args["depth"])})
            return tool_result({**meta, **build_list(infos, user_order, model_order, index, args)})

        problems = check_updates(infos, args["updates"])
        if problems:
            return error_result("Error: nothing was changed.\n" + "\n".join(problems))
        plans = [plan_update(infos, index, u, args["depth"], units_manager) for u in args["updates"]]
        if args["dry_run"]:
            return tool_result({**meta, "dry_run": True, "updates": plans,
                            "note": "Nothing was changed. Pass dry_run: false to apply, in this order."})

        results, failed = apply_updates(objects, infos, args["updates"], units_manager)
        counts, errors = timeline_health(design)
        applied = [r["name"] for r in results if r["status"] == "applied"]
        payload = {
            **meta,
            "dry_run": False,
            "applied": applied,
            "results": results,
            "timeline_health": counts,
            "timeline_errors": _capped(errors, MAX_TIMELINE_ERRORS),
            "note": COMPUTE_NOTE,
        }
        log(f"[MCP] parameters set: {len(applied)}/{len(results)} applied")
        return tool_result(payload, is_error=failed)
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] parameters failed: {exc}")
        return error_result(f"Error: {exc}\n{tb}")
