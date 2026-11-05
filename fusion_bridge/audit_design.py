"""A read-only health report for the active design: audit_design.

This module GATHERS: it reads Fusion into plain facts, section by section, and
reuses the reads ``document_status``, ``timeline_health``, ``update_references``,
``check_solids`` and ``parameters`` already make. ``audit_rules`` JUDGES those
facts. Nothing here writes to the design.

The bridge drops a main-thread call after ``settings.MCP_MAIN_THREAD_TIMEOUT``, so
gathering runs on a budget under that limit. Sections run cheapest first; one that
runs out reports how far it got, and what never ran is listed under ``skipped``.
"""

import functools
import traceback

import adsk.core
import adsk.fusion

from . import audit_rules as rules
from . import solid_check as sc
from . import timeline_health
from .common.api import items, safe, strict_items
from .common.args import bool_arg, int_arg, is_finite_number, reject_unknown, require_object, text_arg
from .common.bodies import iter_body_entries
from .common.documents import open_documents
from .common.geometry import UNIT_FACTORS
from .common.results import error_result, ok_result
from .common.timeline import health_states, state_name
from .dispatch import get_app, log
from .document_lifecycle import LOOKUP_BUDGET_S, CloudLookups, direct_references, indirect_count, read_reference
from .documents import attach_references, describe_document, referenced_ids

DEFAULT_TIME_BUDGET_S = 90.0
MIN_TIME_BUDGET_S = 1.0
MAX_TIME_BUDGET_S = 110.0
MAX_OPEN_CURVE_SCAN = 200
TIME_REASON = "the time budget ran out"

_KNOWN_ARGS = {"profile", "checks", "groups", "units", "max_findings", "baseline_token", "time_budget_s",
               "extra_findings", "detail"}

BASELINES = rules.BaselineStore()


class NotApplicable(Exception):
    """This section does not exist for this design, so nothing was missed."""


# -- Argument validation --------------------------------------------------------


def _name_list(arguments, key, allowed):
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(not isinstance(name, str) for name in value):
        raise ValueError(f"'{key}' must be a non-empty list of names")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"unknown {key}: {', '.join(unknown)}; choose from {list(allowed)}")
    return value


def validate_arguments(arguments):
    """Normalized options; raises ValueError with the text to report."""
    require_object(arguments)
    reject_unknown(arguments, _KNOWN_ARGS)
    profile = arguments.get("profile", "general")
    if profile not in rules.PROFILES:
        raise ValueError(f"'profile' must be one of {list(rules.PROFILES)}, got {profile!r}")
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    if arguments.get("checks") is not None and arguments.get("groups") is not None:
        raise ValueError("pass 'checks' or 'groups', not both")
    budget = arguments.get("time_budget_s", DEFAULT_TIME_BUDGET_S)
    if not is_finite_number(budget) or not MIN_TIME_BUDGET_S <= budget <= MAX_TIME_BUDGET_S:
        raise ValueError(f"'time_budget_s' must be a number from {MIN_TIME_BUDGET_S:g} to {MAX_TIME_BUDGET_S:g}")
    extras = arguments.get("extra_findings")
    return {
        "profile": profile,
        "units": units,
        "checks": _name_list(arguments, "checks", rules.CHECK_NAMES),
        "groups": _name_list(arguments, "groups", rules.GROUPS),
        "max_findings": int_arg(arguments, "max_findings", rules.DEFAULT_MAX_FINDINGS, 1, rules.MAX_FINDINGS_LIMIT),
        "detail": bool_arg(arguments, "detail", False),
        "baseline_token": text_arg(arguments, "baseline_token"),
        "time_budget_s": float(budget),
        "extra_findings": None if extras is None else rules.validate_extra_findings(extras),
    }


# -- Gathering ------------------------------------------------------------------


class Gathering:
    """What the gatherers share for one run."""

    def __init__(self, app, design, document, budget):
        self.app = app
        self.design = design
        self.root = design.rootComponent
        self.document = document
        self.budget = budget
        self.cloud = CloudLookups(app, min(LOOKUP_BUDGET_S, max(budget.remaining(), 0.0)))
        self.measured = []  # (body entry, metrics) pairs, for the face scan


def _coverage(covered, total, reason=TIME_REASON):
    """A gatherer's ``coverage``: None when it read everything."""
    return None if covered == total else {"covered": covered, "total": total, "reason": reason}


def gather_file(state):
    docs = open_documents(state.app)
    infos = [describe_document(doc) for doc in docs]
    attach_references(infos, [referenced_ids(doc) for doc in docs])
    active = describe_document(state.document)
    file_id = (active.get("data_file") or {}).get("id")
    return {
        "active": active,
        "documents": infos,
        "related_ids": ({file_id} if file_id else set()) | referenced_ids(state.document),
        "latest_version": state.cloud.lookup(file_id)[0] if file_id else None,
    }, None


def _is_direct_design(design):
    direct = getattr(getattr(adsk.fusion, "DesignTypes", None), "DirectDesignType", None)
    return direct is not None and safe(design, "designType") == direct


def gather_timeline(state):
    if _is_direct_design(state.design):
        raise NotApplicable("a direct design has no timeline")
    report, rows = timeline_health.read_report(state.design, health_states())
    return {"report": report, "items": [row for row, _ in rows]}, None


def gather_links(state):
    held = direct_references(state.document)
    references = [read_reference(ref, state.cloud) for ref in held]
    data = {"references": references, "indirect": indirect_count(state.document, len(references))}
    return data, _coverage(len(references) - len(state.cloud.skipped), len(references),
                           "the cloud lookup budget ran out; these used the version the open document knows")


def _read_joint(joint, states):
    return {
        "name": safe(joint, "name"),
        "component": safe(safe(joint, "parentComponent"), "name"),
        "health": state_name(safe(joint, "healthState"), states),
        "message": safe(joint, "errorOrWarningMessage") or "",
        "suppressed": safe(joint, "isSuppressed") is True,
    }


def _count(owner, name):
    """How many members a collection has; None, never 0, when it cannot be read."""
    return safe(safe(owner, name), "count")


def _read_occurrence(occurrence):
    return {
        "path": safe(occurrence, "fullPathName") or safe(occurrence, "name") or "?",
        "linked": safe(occurrence, "isReferencedComponent") is True,
        "grounded": safe(occurrence, "isGrounded"),
        "ground_to_parent": safe(occurrence, "isGroundToParent"),
        "joints": _count(occurrence, "joints"),
        "as_built_joints": _count(occurrence, "asBuiltJoints"),
        "rigid_groups": _count(occurrence, "rigidGroups"),
    }


def _constrained_paths(constraint):
    """Occurrences an assembly constraint holds. Occurrence has no list of its own."""
    paths = set()
    for relationship in items(safe(constraint, "geometricRelationships")):
        for name in ("entityOne", "entityTwo"):
            occurrence = safe(safe(relationship, name), "assemblyContext")
            path = safe(occurrence, "fullPathName")
            if path:
                paths.add(path)
    return paths


def gather_joints(state):
    states = health_states()
    joints = strict_items(safe(state.root, "allJoints"))
    constraints = items(safe(state.root, "allAssemblyConstraints"))
    occurrences = strict_items(safe(state.root, "allOccurrences"))
    data = {"joints": [], "occurrences": [], "constrained_paths": set()}
    done, total = 0, len(joints) + len(constraints) + len(occurrences)
    for joint in [*joints, *constraints]:
        if state.budget.expired():
            return data, _coverage(done, total)
        data["joints"].append(_read_joint(joint, states))
        done += 1
    for constraint in constraints:
        data["constrained_paths"] |= _constrained_paths(constraint)
    for occurrence in occurrences:
        if state.budget.expired():
            return data, _coverage(done, total)
        data["occurrences"].append(_read_occurrence(occurrence))
        done += 1
    return data, None


def _has_loose_curves(sketch):
    """True when a curve of the sketch's own is real geometry, not construction or projected."""
    return any(safe(curve, "isConstruction") is False and safe(curve, "isReference") is not True
               for curve in items(safe(sketch, "sketchCurves"), MAX_OPEN_CURVE_SCAN))


def _read_sketch(sketch, component_name):
    profiles = _count(sketch, "profiles")
    return {
        "name": safe(sketch, "name"),
        "component": component_name,
        "fully_constrained": safe(sketch, "isFullyConstrained"),
        "derived": safe(sketch, "isDerived") is True,
        "profiles": profiles,
        "open": profiles == 0 and _has_loose_curves(sketch),
    }


def gather_sketches(state):
    """Sketches of this design's own components: a linked one is edited in its own file."""
    linked = {safe(safe(occurrence, "component"), "name")
              for occurrence in strict_items(safe(state.root, "allOccurrences"))
              if safe(occurrence, "isReferencedComponent") is True}
    pairs = []
    for component in strict_items(safe(state.design, "allComponents")):
        name = safe(component, "name")
        if name not in linked:
            pairs.extend((sketch, name) for sketch in strict_items(safe(component, "sketches")))
    rows = []
    for sketch, name in pairs:
        if state.budget.expired():
            break
        rows.append(_read_sketch(sketch, name))
    return {"sketches": rows}, _coverage(len(rows), len(pairs))


def gather_parameters(state):
    """Names and expressions only: ``parameters.snapshot`` formats every value, which
    a design with thousands of model parameters cannot afford inside the budget."""
    user = [name for name in (safe(param, "name") for param in strict_items(state.design.userParameters)) if name]
    params = strict_items(state.design.allParameters)
    expressions, read = {}, 0
    for param in params:
        if state.budget.expired():
            break
        read += 1
        name = safe(param, "name")
        if name:
            expressions[name] = safe(param, "expression") or ""
    return {"user": user, "expressions": expressions}, _coverage(read, len(params))


def plain_entry(entry):
    """A body entry without its Fusion object."""
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def gather_bodies(state):
    """Every body's name and visibility; solid metrics once per visible definition."""
    entries = []
    finished = True
    for entry in iter_body_entries(state.root):
        if state.budget.expired():
            finished = False
            break
        entries.append(entry)
    sc.assign_keys(entries)
    visible, _ = sc.select_entries(entries, None)
    definitions = rules.group_instances(visible)
    for entry, instances in definitions:
        if state.budget.expired():
            break
        metrics = sc.measure_summary(entry)
        metrics.update(instances=instances, linked=entry["linked"])
        state.measured.append((entry, metrics))
    data = {"entries": [plain_entry(entry) for entry in entries],
            "metrics": [metrics for _, metrics in state.measured]}
    if not finished:
        return data, {"covered": len(entries), "total": None, "reason": TIME_REASON}
    return data, _coverage(len(state.measured), len(definitions))


def gather_body_faces(state):
    """Per-face metrics, smallest bodies first, so running out costs the fewest bodies."""
    pending = sorted(state.measured, key=lambda pair: pair[1].get("face_count") or 0)
    started, faces_done, scanned = state.budget.elapsed(), 0, 0
    for entry, metrics in pending:
        faces = metrics.get("face_count") or 0
        spent = state.budget.elapsed() - started
        if state.budget.expired() or not rules.fits_budget(faces_done, spent, faces, state.budget.remaining()):
            break
        sc.measure_details(entry, metrics)
        faces_done += faces
        scanned += 0 if metrics.get("face_scan_skipped") else 1
    return {}, _coverage(scanned, len(pending))


GATHERERS = {
    "file": gather_file,
    "timeline": gather_timeline,
    "links": gather_links,
    "joints": gather_joints,
    "sketches": gather_sketches,
    "parameters": gather_parameters,
    "bodies": gather_bodies,
    "body_faces": gather_body_faces,
}


def gather(state, sections, gatherers=None):
    """Read *sections* in order. Returns ``(facts, status per section)``.

    A section that raises costs only itself. One built on another (the face scan
    on the bodies) is no more complete than what it read.
    """
    gatherers = gatherers or GATHERERS
    facts, status = {}, {}
    for section in sections:
        upstream = status.get(rules.SECTION_NEEDS.get(section), {}).get("status")
        if upstream in (rules.SKIPPED, rules.FAILED, rules.NOT_APPLICABLE):
            status[section] = {"status": rules.SKIPPED, "reason": f"{rules.SECTION_NEEDS[section]} was not read"}
            continue
        if state.budget.expired():
            status[section] = {"status": rules.SKIPPED, "reason": TIME_REASON}
            continue
        try:
            facts[section], coverage = gatherers[section](state)
        except NotApplicable as exc:
            status[section] = {"status": rules.NOT_APPLICABLE, "reason": str(exc)}
            continue
        except Exception as exc:
            log(f"[MCP] audit_design: reading {section} failed: {exc}")
            status[section] = {"status": rules.FAILED, "reason": f"{type(exc).__name__}: {exc}"}
            continue
        if coverage is not None:
            status[section] = {"status": rules.PARTIAL, **coverage}
        elif upstream == rules.PARTIAL:
            status[section] = {"status": rules.PARTIAL, "reason": f"{rules.SECTION_NEEDS[section]} was only partly read"}
        else:
            status[section] = {"status": rules.COMPLETE}
    return facts, status


# -- The tool -------------------------------------------------------------------


def document_key(document):
    """What identifies a design between runs. The name changes with every saved version."""
    return safe(safe(document, "dataFile"), "id") or safe(document, "name")


def run_audit(state, opts, baseline, document_id, document_name):
    """Gather, judge, compare and assemble the report."""
    selected, switched_off = rules.select_checks(opts["profile"], opts["checks"], opts["groups"])
    facts, sections = gather(state, rules.sections_for(selected))
    findings, fully_run, ran, crashed = rules.judge(facts, sections, selected, opts["profile"], opts["units"])
    extras = opts["extra_findings"]
    extras_supplied = extras is not None
    findings.extend(extras or [])

    # Stored and compared one by one: only the rows of the report are folded.
    show = functools.partial(rules.collapse, expanded=opts["checks"] or (), detail=opts["detail"])
    token = BASELINES.store(rules.baseline_record(document_id, document_name, opts["profile"], findings,
                                                  fully_run, extras_supplied))
    changes = None
    if baseline is not None:
        findings, resolved, not_compared = rules.compare_to_baseline(baseline, findings, fully_run, extras_supplied)
        changes = rules.changes_report(opts["baseline_token"], baseline, findings, resolved, not_compared,
                                       opts["max_findings"], show=show)
    return rules.build_report(
        findings, document=document_name, profile=opts["profile"], units=opts["units"], token=token,
        complete=rules.is_complete(sections, crashed), checks_run=ran,
        elapsed_s=state.budget.elapsed(), max_findings=opts["max_findings"], changes=changes,
        issues=rules.first_issues(facts, opts["max_findings"] if opts["detail"]
                                  else min(rules.FIRST_ISSUES_LIMIT, opts["max_findings"])),
        show=show,
        skipped=rules.skipped_report(sections, selected, crashed,
                                     switched_off if opts["checks"] or opts["groups"] else ()),
    )


def audit_design(arguments):
    """Report the health of the active design: read-only, worst findings first."""
    try:
        opts = validate_arguments(arguments)
    except ValueError as exc:
        return error_result(f"Error: {exc}")

    baseline = None
    if opts["baseline_token"] is not None:
        baseline = BASELINES.get(opts["baseline_token"])
        if baseline is None:
            return error_result(f"Error: unknown baseline_token {opts['baseline_token']!r} (baselines are "
                                f"session-only and the last {rules.BASELINE_STORE_LIMIT} are kept)")
    try:
        app = get_app()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return error_result("Error: the active product is not a Fusion design")
        document = app.activeDocument
        name, key = safe(document, "name"), document_key(document)
        if baseline is not None and baseline["document_key"] != key:
            return error_result(f"Error: that baseline was taken in {baseline['document']!r}, not {name!r}; "
                                "findings from different designs cannot be compared")
        state = Gathering(app, design, document, rules.Budget(opts["time_budget_s"]))
        report = run_audit(state, opts, baseline, key, name)
        log(f"[MCP] audit_design: {report['summary']['findings']} findings in {report['elapsed_s']} s")
        return ok_result(report)
    except Exception as exc:
        log(f"[MCP] audit_design failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
