"""Judge a design's gathered facts: the Fusion-free half of audit_design.

``audit_design`` GATHERS plain facts from Fusion into named sections; everything
here JUDGES those facts, so it is testable with fakes. A check is a small
function ``(facts, context) -> [{"where", "message"}]`` naming what it found and
nothing else. Whether that matters is a separate question: ``SEVERITY_TABLE``
answers it per profile, and a check switched off there never runs.

The finding format is the public contract, and an outside tool contributes
through ``extra_findings`` in the same shape, without this repo knowing about it:

    check     short name of the rule, e.g. "timeline_errors"
    group     file, timeline, links, bodies, joints, sketches, parameters, or the caller's own
    severity  error, warning or info
    where     where to look: "<kind>: <name>", e.g. "body: Frame:1/Body6". Together with
              ``check`` it identifies the finding between runs, so keep it stable
    message   what was found
    why       why it matters (may be null)
    fix_tool  the tool to go to next (may be null)
    source    extra findings only: who supplied it (default "extra_findings")

Every finding is judged and compared one by one. Only the report folds repeats of
a check into a single row (``collapse``), so what a baseline stores and matches
is always the individual ``check`` and ``where``.

A run may cover less than was asked (time budget, a read that failed). Each
section records that, and a baseline compares only checks that ran in full on
both runs: a finding that was not looked for is not a finding that was resolved.
"""

import re
import time
import uuid
from collections import Counter, OrderedDict, namedtuple

from . import parameter_graph as graph
from . import solid_check as sc
from .common.api import plain_text
from .document_lifecycle import OUT_OF_DATE, STALE_NOT_FLAGGED, UNKNOWN
from .documents import flag_duplicates

ERROR, WARNING, INFO = "error", "warning", "info"
OFF = None
SEVERITIES = (ERROR, WARNING, INFO)
SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}

GROUPS = ("file", "timeline", "links", "bodies", "joints", "sketches", "parameters")
PROFILES = ("general", "assembly", "export-ready")

# Gathered in this order, so a spent budget costs the slowest reads first.
SECTIONS = ("file", "timeline", "links", "joints", "sketches", "parameters", "bodies", "body_faces")
# A section that reads what another one found.
SECTION_NEEDS = {"body_faces": "bodies"}

COMPLETE, PARTIAL, SKIPPED, FAILED, NOT_APPLICABLE = "complete", "partial", "skipped", "failed", "not_applicable"

DEFAULT_MAX_FINDINGS = 100
MAX_FINDINGS_LIMIT = 1000
MAX_EXTRA_FINDINGS = 1000
BASELINE_STORE_LIMIT = 20
MAX_MESSAGE_CHARS = 300
COLLAPSE_SAMPLE_SIZE = 3
FIRST_ISSUES_LIMIT = 20
EXTRA_SOURCE = "extra_findings"
EXTRA_KEYS = {"check", "group", "severity", "where", "message", "why", "fix_tool", "source"}

# Fusion's default names, with the " (1)" a copy gets. Imported parts carry them
# too, so a match says "unnamed", never "wrong".
GENERIC_BODY_NAME = re.compile(r"^(?:body|surface|solid)\s*\d+(?:\s*\(\d+\))?$", re.IGNORECASE)
JOINT_ENTITY_TYPES = ("adsk::fusion::Joint", "adsk::fusion::AssemblyConstraint")

# Which rules each profile runs, and how much each one matters there. Checks
# never set a severity: to change one, change this table.
SEVERITY_TABLE = {
    # check                     general  assembly  export-ready
    "not_saved":                (WARNING, WARNING, ERROR),
    "unsaved_changes":          (INFO, INFO, ERROR),
    "old_version":              (WARNING, WARNING, ERROR),
    "duplicate_instances":      (WARNING, WARNING, WARNING),
    "read_only":                (INFO, INFO, INFO),
    "timeline_errors":          (ERROR, ERROR, ERROR),
    "timeline_warnings":        (WARNING, WARNING, WARNING),
    "timeline_rolled_back":     (WARNING, WARNING, ERROR),
    "suppressed_features":      (INFO, INFO, WARNING),
    "active_component":         (INFO, INFO, OFF),
    "link_out_of_date":         (WARNING, WARNING, ERROR),
    "link_stale_unflagged":     (WARNING, WARNING, ERROR),
    "link_unreadable":          (WARNING, WARNING, ERROR),
    "indirect_links":           (INFO, INFO, INFO),
    "zero_volume":              (ERROR, ERROR, ERROR),
    "non_solid":                (WARNING, WARNING, ERROR),
    "multiple_lumps":           (WARNING, WARNING, ERROR),
    "extra_shells":             (INFO, INFO, WARNING),
    "sliver_faces":             (WARNING, OFF, WARNING),
    "tiny_faces":               (INFO, OFF, WARNING),
    "short_edges":              (INFO, OFF, WARNING),
    # Off where parts of unlike kinds sit together: a casting beside bolts is no fault.
    "face_count_outlier":       (OFF, OFF, INFO),
    "hidden_bodies":            (INFO, INFO, WARNING),
    "generic_body_name":        (INFO, INFO, INFO),
    "joint_errors":             (ERROR, ERROR, ERROR),
    "joint_warnings":           (WARNING, WARNING, WARNING),
    "unheld_occurrence":        (INFO, INFO, INFO),
    "under_constrained_sketch": (INFO, OFF, INFO),
    "open_profile":             (INFO, OFF, INFO),
    # Off by default: parameters are often kept as measured facts that scripts read.
    "unused_user_parameter":    (OFF, OFF, INFO),
}


class Budget:
    """A deadline for gathering. *clock* is injectable so tests need not wait."""

    def __init__(self, seconds, clock=time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._started = clock()

    def elapsed(self):
        return self._clock() - self._started

    def remaining(self):
        return self.seconds - self.elapsed()

    def expired(self):
        return self.remaining() <= 0


def fits_budget(done, elapsed, upcoming, remaining):
    """Would *upcoming* more units of work fit in *remaining*, at the rate seen so far?

    One large body cannot be interrupted once started, so it has to be judged
    before it starts. With no rate to go on yet, the answer is yes.
    """
    return done <= 0 or elapsed / done * upcoming <= remaining


class Context:
    """What a check may ask besides the facts."""

    def __init__(self, units="in", selected=(), sections=None):
        self.units = units
        self.selected = set(selected)
        self.sections = sections or {}
        self.thresholds = sc.validate_args({"units": units})[0]["thresholds"]

    def complete(self, section):
        return self.sections.get(section, {}).get("status") == COMPLETE


def hit(where, message):
    return {"where": where, "message": _short(message)}


def _short(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS - 3] + "..."


def is_under(path, paths):
    """True when occurrence *path* sits below one of *paths* ('A:1' holds 'A:1+B:1')."""
    parts = path.split("+")
    return any("+".join(parts[:depth]) in paths for depth in range(1, len(parts)))


def group_instances(entries):
    """``[(first entry, instance count)]``, one per body definition.

    Forty instances of one bolt are one body to judge, and measuring it forty
    times is what would spend the budget.
    """
    groups = OrderedDict()
    for entry in entries:
        key = (entry["component"], entry["body"], entry["index"])
        first, count = groups.get(key, (entry, 0))
        groups[key] = (first, count + 1)
    return list(groups.values())


# -- file -----------------------------------------------------------------------


def _document(facts):
    active = facts["file"]["active"]
    return f"file: {active.get('name')}", active


def not_saved(facts, context):
    where, active = _document(facts)
    return [hit(where, "the document has never been saved")] if active.get("is_saved") is False else []


def unsaved_changes(facts, context):
    where, active = _document(facts)
    return [hit(where, "the document has unsaved changes")] if active.get("is_modified") is True else []


def read_only(facts, context):
    where, active = _document(facts)
    data_file = active.get("data_file") or {}
    return [hit(where, "the file is read-only")] if data_file.get("is_read_only") is True else []


def old_version(facts, context):
    where, active = _document(facts)
    loaded = (active.get("data_file") or {}).get("version_number")
    latest = facts["file"].get("latest_version")
    if loaded is None or latest is None or latest <= loaded:
        return []
    return [hit(where, f"version {loaded} is open; the latest is {latest}")]


def duplicate_instances(facts, context):
    """Several open copies of this file, or of a file it links."""
    groups = OrderedDict()
    for info in facts["file"]["documents"]:
        file_id = (info.get("data_file") or {}).get("id")
        if file_id in facts["file"]["related_ids"]:
            groups.setdefault(file_id, []).append(info)
    hits = []
    for group in groups.values():
        if len(group) > 1:
            name = group[0]["data_file"].get("name") or group[0].get("name")
            hits.append(hit(f"file: {name}", flag_duplicates(group)[0]))
    return hits


# -- timeline -------------------------------------------------------------------


def _timeline_rows(facts, context, health, joint_check):
    """Unsuppressed timeline items in *health*, minus joints the joint check reports."""
    owned = joint_check in context.selected and context.complete("joints")
    return [row for row in facts["timeline"]["items"]
            if row["health"] == health and not row["suppressed"]
            and not (owned and row.get("entity_type") in JOINT_ENTITY_TYPES)]


def _timeline_message(row):
    return plain_text(row["message"]) or "no message from Fusion"


def _timeline_hit(row):
    return hit(f"timeline: {row['name']}", f"step {row['index']}: {_timeline_message(row)}")


def timeline_errors(facts, context):
    return [_timeline_hit(row) for row in _timeline_rows(facts, context, "error", "joint_errors")]


def timeline_warnings(facts, context):
    return [_timeline_hit(row) for row in _timeline_rows(facts, context, "warning", "joint_warnings")]


def timeline_rolled_back(facts, context):
    report = facts["timeline"]["report"]
    if not report["rolled_back"]:
        return []
    return [hit("timeline: marker", f"the marker is at {report['marker_position']} of {report['count']}; "
                                    "later steps are not computed or checked")]


def suppressed_features(facts, context):
    return [hit(f"timeline: {row['name']}", f"step {row['index']} is suppressed")
            for row in facts["timeline"]["items"] if row["suppressed"]]


def active_component(facts, context):
    report = facts["timeline"]["report"]
    if report["active_is_root"]:
        return []
    return [hit(f"component: {report['active_component']}", "the active component is not the root")]


# -- links ----------------------------------------------------------------------


def _links_with(facts, status):
    return [ref for ref in facts["links"]["references"] if ref.get("status") == status]


def _link_where(ref):
    return f"link: {ref.get('name') or ref.get('data_file_id') or 'unknown file'}"


def _versions(ref):
    return f"version {ref.get('loaded_version')} is loaded; the latest is {ref.get('latest_version')}"


def link_out_of_date(facts, context):
    return [hit(_link_where(ref), _versions(ref)) for ref in _links_with(facts, OUT_OF_DATE)]


def link_stale_unflagged(facts, context):
    return [hit(_link_where(ref), f"{_versions(ref)}, but Fusion does not flag it")
            for ref in _links_with(facts, STALE_NOT_FLAGGED)]


def link_unreadable(facts, context):
    return [hit(_link_where(ref), "its loaded or latest version could not be read")
            for ref in _links_with(facts, UNKNOWN)]


def indirect_links(facts, context):
    count = facts["links"]["indirect"]
    if not count:
        return []
    return [hit("links: sub-assemblies", f"{count} link(s) inside sub-assemblies were not checked; "
                                         "audit those documents to check them")]


# -- bodies ---------------------------------------------------------------------


def _validity(metrics, context):
    return sc.validity_problems(metrics, context.thresholds, context.units)


def _slivers(metrics, context):
    return sc.sliver_problems(metrics, context.thresholds, context.units)


def _lumps(metrics, context):
    return sc.lump_problems(metrics)


def _body_note(metrics):
    notes = []
    if (metrics.get("instances") or 1) > 1:
        notes.append(f"{metrics['instances']} instances")
    if metrics.get("linked"):
        notes.append("linked")
    return f" ({', '.join(notes)})" if notes else ""


def _body_rule(kind, read, describe):
    """A check over one kind of solid_check problem: its rules, this table's severity."""
    def check(facts, context):
        return [hit(f"body: {row['body']}", describe(row, context) + _body_note(metrics))
                for metrics in facts["bodies"]["metrics"]
                for row in read(metrics, context) if row["type"] == kind]

    check.__name__ = kind
    return check


zero_volume = _body_rule("zero_volume", _validity, lambda row, c: f"volume is {row['volume']} {c.units}^3")
non_solid = _body_rule("non_solid", _validity, lambda row, c: "a surface or open body, not a solid")
tiny_faces = _body_rule("tiny_faces", _validity, lambda row, c: (
    f"{row['count']} tiny face(s), the smallest {row['smallest_area']} {c.units}^2"))
short_edges = _body_rule("short_edges", _validity, lambda row, c: (
    f"{row['count']} short edge(s), the shortest {row['shortest']} {c.units}"))
sliver_faces = _body_rule("sliver_faces", _slivers, lambda row, c: (
    f"{row['count']} sliver face(s), the thinnest {row['examples'][0]['width']} {c.units} wide"))
multiple_lumps = _body_rule("multiple_lumps", _lumps, lambda row, c: f"one body made of {row['lumps']} disjoint pieces")
extra_shells = _body_rule("extra_shells", _lumps, lambda row, c: (
    f"{row['shells']} shells in {row['lumps']} lump(s): an enclosed void"))


def face_count_outlier(facts, context):
    """Bodies with far more faces than the other bodies of their own component.

    Like with like: across a whole assembly the median is set by whatever is
    most numerous, so a casting beside bolts looks like a twisted loft.
    """
    by_component = OrderedDict()
    for metrics in facts["bodies"]["metrics"]:
        by_component.setdefault(metrics.get("component"), []).append(metrics)
    factor = context.thresholds["face_count_factor"]
    return [hit(f"body: {row['body']}", f"{row['face_count']} faces; the others in its component have "
                                        f"a median of {row['median_face_count']}")
            for bodies in by_component.values() for row in sc.face_count_outliers(bodies, factor)]


def hidden_bodies(facts, context):
    """Hidden bodies, one finding per hidden occurrence rather than per body inside it."""
    occurrences, bodies = OrderedDict(), []
    for entry in facts["bodies"]["entries"]:
        if entry["occurrence_visible"] is False:
            occurrences.setdefault(entry["occurrence_path"], []).append(entry["body"])
        elif entry["visible"] is False:
            bodies.append(entry)
    hits = [hit(f"occurrence: {path}", f"hidden, with {len(names)} body(ies)")
            for path, names in occurrences.items() if not is_under(path, occurrences)]
    hits.extend(hit(f"body: {entry['key']}", "hidden") for entry, _ in group_instances(bodies))
    return hits


def generic_body_name(facts, context):
    return [hit(f"body: {entry['key']}", "still has Fusion's default name" + (" (linked)" if entry["linked"] else ""))
            for entry, _ in group_instances(facts["bodies"]["entries"])
            if GENERIC_BODY_NAME.match(entry["body"] or "")]


# -- joints ---------------------------------------------------------------------


def _joint_rows(facts, health):
    return [hit(f"joint: {row['component']}/{row['name']}", plain_text(row["message"]) or f"Fusion reports a {health}")
            for row in facts["joints"]["joints"] if row["health"] == health and not row["suppressed"]]


def joint_errors(facts, context):
    return _joint_rows(facts, "error")


def joint_warnings(facts, context):
    return _joint_rows(facts, "warning")


def unheld_occurrence(facts, context):
    """Occurrences with no joint, rigid group or constraint, and not grounded.

    An unread count or flag is not evidence, so it never makes a finding. A
    nested occurrence grounded to its parent moves with it, and the parent gets
    its own row. Inside a link is the linked file's business.
    """
    rows = facts["joints"]["occurrences"]
    linked = {row["path"] for row in rows if row["linked"]}
    constrained = set(facts["joints"]["constrained_paths"])
    hits = []
    for row in rows:
        path = row["path"]
        held = (row["joints"], row["as_built_joints"], row["rigid_groups"])
        if (is_under(path, linked) or path in constrained or row["grounded"] is not False
                or any(count is None or count > 0 for count in held)
                or ("+" in path and row["ground_to_parent"] is not False)):
            continue
        hits.append(hit(f"occurrence: {path}", "no joint, rigid group or constraint holds it, and it is not grounded"))
    return hits


# -- sketches -------------------------------------------------------------------


def _sketch_where(row):
    return f"sketch: {row['component']}/{row['name']}"


def under_constrained_sketch(facts, context):
    return [hit(_sketch_where(row), "not fully constrained")
            for row in facts["sketches"]["sketches"]
            if row["fully_constrained"] is False and not row["derived"]]


def open_profile(facts, context):
    return [hit(_sketch_where(row), "has curves but no closed profile")
            for row in facts["sketches"]["sketches"] if row["open"]]


# -- parameters -----------------------------------------------------------------


def unused_user_parameter(facts, context):
    dependents = graph.build_dependents(facts["parameters"]["expressions"])
    return [hit(f"parameter: {name}", "no other parameter in this design references it")
            for name in facts["parameters"]["user"] if not dependents.get(name)]


# -- The rules ------------------------------------------------------------------

# *needs* names the section the check reads. *whole*: the check cannot answer from
# part of the section, because something unread could change the answer.
Check = namedtuple("Check", "name group needs run why fix_tool whole", defaults=(False,))

CHECKS = (
    Check("not_saved", "file", "file", not_saved,
          "Nothing protects this work and links to it cannot resolve.", "save_document"),
    Check("unsaved_changes", "file", "file", unsaved_changes,
          "What is exported or linked may not be what the cloud holds.", "save_document"),
    Check("old_version", "file", "file", old_version,
          "Edits here would start from an older design.", "open_document"),
    Check("duplicate_instances", "file", "file", duplicate_instances,
          "Saving a non-primary instance can silently do nothing.", "close_document"),
    Check("read_only", "file", "file", read_only,
          "Saves will not stick until the file is editable.", "set_editable_state"),
    Check("timeline_errors", "timeline", "timeline", timeline_errors,
          "While the timeline has broken features, Fusion rolls back API edits.", "timeline_health"),
    Check("timeline_warnings", "timeline", "timeline", timeline_warnings,
          "The feature computed, but possibly not as designed.", "timeline_health"),
    Check("timeline_rolled_back", "timeline", "timeline", timeline_rolled_back,
          "The model shown is not the finished design.", "timeline_health"),
    Check("suppressed_features", "timeline", "timeline", suppressed_features,
          "The model is missing this step until it is unsuppressed or deleted.", "delete_features"),
    Check("active_component", "timeline", "timeline", active_component,
          "New features land in the active component.", None),
    Check("link_out_of_date", "links", "links", link_out_of_date,
          "This design is built on an older version of the linked file.", "update_references"),
    Check("link_stale_unflagged", "links", "links", link_stale_unflagged,
          "Get Latest may do nothing until the parent is closed and reopened.", "update_references"),
    Check("link_unreadable", "links", "links", link_unreadable,
          "The link's state is unknown, so it cannot be called current.", "update_references"),
    Check("indirect_links", "links", "links", indirect_links,
          "A stale link further down would not show up here.", "update_references"),
    Check("zero_volume", "bodies", "bodies", zero_volume,
          "A solid with no volume is a failed feature that still looks like a body.", "check_solids"),
    Check("non_solid", "bodies", "bodies", non_solid,
          "Volume, mass and interference checks skip it, and it will not export as a solid.", "check_solids"),
    Check("multiple_lumps", "bodies", "bodies", multiple_lumps,
          "Usually a union or base-feature artifact, or a cut that split a part in two.", "check_solids"),
    Check("extra_shells", "bodies", "bodies", extra_shells,
          "A void inside a solid is invisible and changes its mass.", "check_solids"),
    Check("sliver_faces", "bodies", "body_faces", sliver_faces,
          "Long thin faces are the mark of a twisted loft, and they break later features.", "check_solids"),
    Check("tiny_faces", "bodies", "body_faces", tiny_faces,
          "Usually a harmless leftover, but they can break fillets and exports.", "check_solids"),
    Check("short_edges", "bodies", "body_faces", short_edges,
          "Usually a harmless leftover, but they can break fillets and exports.", "check_solids"),
    Check("face_count_outlier", "bodies", "bodies", face_count_outlier,
          "A body far busier than its neighbors may be a twisted loft.", "describe_body", True),
    Check("hidden_bodies", "bodies", "bodies", hidden_bodies,
          "Hidden bodies are often stale reference geometry, and they still export.", "list_bodies"),
    Check("generic_body_name", "bodies", "bodies", generic_body_name,
          "Fine for an imported part; a body of your own is easier to find with a real name.", None),
    Check("joint_errors", "joints", "joints", joint_errors,
          "The assembly is not positioned the way the joint says.", None),
    Check("joint_warnings", "joints", "joints", joint_warnings,
          "The joint solved, but possibly not as designed.", None),
    Check("unheld_occurrence", "joints", "joints", unheld_occurrence,
          "It can be dragged out of place. Fine if it was modeled in position.", None),
    Check("under_constrained_sketch", "sketches", "sketches", under_constrained_sketch,
          "The sketch can shift when something it depends on changes.", None),
    Check("open_profile", "sketches", "sketches", open_profile,
          "It cannot be extruded. Fine for a path or a reference sketch.", "sketch_profile"),
    Check("unused_user_parameter", "parameters", "parameters", unused_user_parameter,
          "It drives nothing here, so changing it changes nothing. It may be a note, or left over.",
          "parameters", True),
)
CHECK_NAMES = tuple(check.name for check in CHECKS)


def severity_of(check_name, profile):
    return SEVERITY_TABLE[check_name][PROFILES.index(profile)]


def select_checks(profile, checks=None, groups=None):
    """``(checks to run, names the profile switched off)`` after the caller's filter."""
    chosen = [check for check in CHECKS
              if (checks is None or check.name in checks) and (groups is None or check.group in groups)]
    return ([check for check in chosen if severity_of(check.name, profile) is not OFF],
            [check.name for check in chosen if severity_of(check.name, profile) is OFF])


def sections_for(selected):
    """The sections *selected* need, in gathering order, with what those read first."""
    wanted = {check.needs for check in selected}
    wanted.update(SECTION_NEEDS[section] for section in list(wanted) if section in SECTION_NEEDS)
    return [section for section in SECTIONS if section in wanted]


def judge(facts, sections, selected, profile, units="in"):
    """Run *selected* over the facts: ``(findings, fully run names, number run, crashed)``.

    A check whose section is missing does not run. One that ran on a partial
    section keeps its findings but is not fully run, so a baseline will not
    compare it. A check that raises costs only itself.
    """
    context = Context(units, [check.name for check in selected], sections)
    findings, fully_run, ran, crashed = [], set(), 0, []
    for check in selected:
        status = sections.get(check.needs, {}).get("status")
        if status not in (COMPLETE, PARTIAL) or (status == PARTIAL and check.whole):
            continue
        try:
            hits = check.run(facts, context)
        except Exception as exc:
            crashed.append({"check": check.name, "status": FAILED, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        ran += 1
        findings.extend({"check": check.name, "group": check.group,
                         "severity": severity_of(check.name, profile), "where": found["where"],
                         "message": found["message"], "why": check.why, "fix_tool": check.fix_tool}
                        for found in hits)
        if status == COMPLETE:
            fully_run.add(check.name)
    return findings, fully_run, ran, crashed


def skipped_report(sections, selected, crashed, switched_off=()):
    """What this run did not fully cover, section by section."""
    rows = []
    for section, state in sections.items():
        if state["status"] == COMPLETE:
            continue
        row = {"section": section, **state, "checks": [check.name for check in selected if check.needs == section]}
        rows.append(row)
    rows.extend(crashed)
    rows.extend({"check": name, "status": "off", "reason": "this profile switches it off"} for name in switched_off)
    return rows


def is_complete(sections, crashed):
    """False when something that should have been looked at was not."""
    return not crashed and all(state["status"] in (COMPLETE, NOT_APPLICABLE) for state in sections.values())


# -- Extra findings -------------------------------------------------------------


def validate_extra_findings(value):
    """Normalized copies of caller-supplied findings; raises ValueError."""
    if not isinstance(value, list) or len(value) > MAX_EXTRA_FINDINGS:
        raise ValueError(f"'extra_findings' must be a list of at most {MAX_EXTRA_FINDINGS} findings")
    extras = []
    for index, item in enumerate(value):
        label = f"extra_findings[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be an object")
        unknown = sorted(set(item) - EXTRA_KEYS)
        if unknown:
            raise ValueError(f"{label} has unknown key(s): {', '.join(unknown)}")
        for key in ("check", "group", "where", "message"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"{label}.{key} must be a non-empty string")
        if item.get("severity") not in SEVERITIES:
            raise ValueError(f"{label}.severity must be one of {list(SEVERITIES)}")
        for key in ("why", "fix_tool", "source"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ValueError(f"{label}.{key} must be a string or null")
        if item["check"] in SEVERITY_TABLE:
            raise ValueError(f"{label}.check {item['check']!r} is a built-in check; use a name of your own")
        extras.append({"check": item["check"], "group": item["group"], "severity": item["severity"],
                       "where": item["where"], "message": item["message"], "why": item.get("why"),
                       "fix_tool": item.get("fix_tool"), "source": item.get("source") or EXTRA_SOURCE})
    return extras


# -- Baselines ------------------------------------------------------------------


class BaselineStore:
    """Past runs by token. Session-only, like check_solids': a reload forgets them."""

    def __init__(self, limit=BASELINE_STORE_LIMIT):
        self._limit = limit
        self._runs = OrderedDict()

    def store(self, record):
        token = uuid.uuid4().hex[:12]
        self._runs[token] = record
        while len(self._runs) > self._limit:
            self._runs.popitem(last=False)
        return token

    def get(self, token):
        return self._runs.get(token)


def baseline_record(document_key, document, profile, findings, fully_run, extras_supplied, now=None):
    return {"created": time.time() if now is None else now, "document_key": document_key, "document": document,
            "profile": profile, "findings": list(findings), "fully_run": set(fully_run),
            "extras_supplied": extras_supplied}


def _identity(finding):
    return finding["check"], finding["where"]


def compare_to_baseline(baseline, findings, fully_run, extras_supplied):
    """Label *findings* new, unchanged or not_compared, and list what was resolved.

    Returns ``(labeled copies, resolved, not_compared_checks)``. Only checks that
    ran in full both times are compared. Extra findings count as compared when
    the caller supplied the argument both times, so ``[]`` means "looked, found
    nothing". Equal identities match one for one.
    """
    shared = baseline["fully_run"] & set(fully_run)
    extras_shared = baseline["extras_supplied"] and extras_supplied

    def compared(finding):
        return extras_shared if "source" in finding else finding["check"] in shared

    remaining = Counter(_identity(f) for f in baseline["findings"] if compared(f))
    labeled = []
    for finding in findings:
        if not compared(finding):
            change = "not_compared"
        elif remaining[_identity(finding)] > 0:
            remaining[_identity(finding)] -= 1
            change = "unchanged"
        else:
            change = "new"
        labeled.append({**finding, "change": change})

    resolved = []
    for finding in baseline["findings"]:
        if compared(finding) and remaining[_identity(finding)] > 0:
            remaining[_identity(finding)] -= 1
            resolved.append(finding)
    not_compared = {f["check"] for f in [*baseline["findings"], *findings] if not compared(f)}
    return labeled, resolved, sorted(not_compared)


# -- The report -----------------------------------------------------------------


def _group_rank(group):
    return GROUPS.index(group) if group in GROUPS else len(GROUPS)


def sort_findings(findings):
    """Worst first; among equals what is new since the baseline comes first."""
    return sorted(findings, key=lambda f: (SEVERITY_RANK[f["severity"]], f.get("change") != "new",
                                           _group_rank(f["group"]), f["group"], f["check"], f["where"]))


def _collapsed_row(group):
    """One row for the findings of *group*, already in report order."""
    first, count = group[0], len(group)
    messages = {finding["message"] for finding in group}
    row = {**first, "count": count, "where": f"{count} places",
           "message": first["message"] if len(messages) == 1 else f"{count} findings; the sample shows some"}
    row["sample"] = [{key: finding[key] for key in ("where", "message", "change") if key in finding}
                     for finding in group[:COLLAPSE_SAMPLE_SIZE]]
    changes = Counter(finding["change"] for finding in group if "change" in finding)
    if changes:
        row["change_counts"] = dict(sorted(changes.items()))
    return row


def collapse(findings, expanded=(), detail=False):
    """Fold repeats of a check into one row with a count and a short sample.

    *findings* are in report order, so a row sits where its worst member would
    and its sample leads with what is new. Rows for a check named in *expanded*,
    or all of them under *detail*, stay individual, and so does a lone finding.
    """
    if detail:
        return list(findings)
    groups = OrderedDict()
    for position, finding in enumerate(findings):
        key = (position,) if finding["check"] in expanded else (
            finding["check"], finding["group"], finding["severity"], finding.get("source"))
        groups.setdefault(key, []).append(finding)
    return [group[0] if len(group) == 1 else _collapsed_row(group) for group in groups.values()]


def first_issues(facts, limit):
    """Broken timeline items in step order, since a repair starts at the earliest.

    Straight from the timeline, so a broken joint the joint checks report is still
    in its place. Returns ``(rows, total)``; both None when the timeline was not
    read, which is not the same as a timeline with nothing wrong.
    """
    timeline = facts.get("timeline")
    if timeline is None:
        return None, None
    rows, step = [], -1
    for row in timeline["items"]:
        step = step if row.get("index") is None else row["index"]  # inside a collapsed group: no slot of its own
        if row["health"] in (ERROR, WARNING) and not row["suppressed"]:
            rows.append({"step": step, "name": row["name"], "severity": row["health"],
                         "component": row.get("component"), "message": _short(_timeline_message(row))})
    rows.sort(key=lambda row: row["step"])
    return rows[:limit], len(rows)


def summarize(findings):
    by_severity = {severity: 0 for severity in SEVERITIES}
    by_group, by_check = OrderedDict(), Counter()
    for finding in sorted(findings, key=lambda f: (_group_rank(f["group"]), f["group"])):
        by_severity[finding["severity"]] += 1
        by_group.setdefault(finding["group"], {severity: 0 for severity in SEVERITIES})[finding["severity"]] += 1
        by_check[finding["check"]] += 1
    return {"findings": len(findings), "by_severity": by_severity, "by_group": by_group,
            "by_check": dict(sorted(by_check.items()))}


def changes_report(token, baseline, labeled, resolved, not_compared, max_findings, now=None, show=list):
    """What changed since *baseline*. *show* turns ordered findings into report rows."""
    new = sort_findings([f for f in labeled if f["change"] == "new"])
    resolved = sort_findings(resolved)
    new_rows, resolved_rows = show(new), show(resolved)
    return {
        "baseline_token": token,
        "baseline_profile": baseline["profile"],
        "baseline_age_seconds": round((time.time() if now is None else now) - baseline["created"], 1),
        "new": len(new),
        "resolved": len(resolved),
        "unchanged": sum(1 for f in labeled if f["change"] == "unchanged"),
        "not_compared": sum(1 for f in labeled if f["change"] == "not_compared"),
        "not_compared_checks": not_compared,
        "new_findings": new_rows[:max_findings],
        "resolved_findings": resolved_rows[:max_findings],
        "truncated": len(new_rows) > max_findings or len(resolved_rows) > max_findings,
    }


def build_report(findings, *, document, profile, units, token, complete, checks_run, elapsed_s,
                 skipped, max_findings, changes=None, issues=(None, None), show=list):
    """The result: verdict and summary first, then findings, then what was skipped.

    ``ok`` needs nothing worse than info AND full coverage: a clean report on half
    a design is not a healthy design. The summary counts every finding; *show*
    turns the ordered findings into rows, and ``max_findings`` caps the rows.
    *issues* is what ``first_issues`` returned.
    """
    summary = summarize(findings)
    rows = show(sort_findings(findings))
    report = {
        "ok": complete and not summary["by_severity"][ERROR] and not summary["by_severity"][WARNING],
        "summary": summary,
        "complete": complete,
        "document": document,
        "profile": profile,
        "units": units,
        "checks_run": checks_run,
        "elapsed_s": round(elapsed_s, 1),
        "baseline_token": token,
    }
    if changes is not None:
        report["changes"] = changes
    report["first_issues"], report["first_issues_total"] = issues
    report["findings"] = rows[:max_findings]
    report["findings_truncated"] = len(rows) > max_findings
    report["skipped"] = skipped
    return report
