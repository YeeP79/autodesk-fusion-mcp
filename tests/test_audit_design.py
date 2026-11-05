"""audit_design: the rules over plain facts, then gathering and the tool over fakes."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)
import adsk.fusion

from fusion_bridge import audit_design as ad
from fusion_bridge import audit_rules as rules
from fusion_bridge import tool_surface


def run_check(name, facts, selected=None, sections=None, units="in"):
    check = next(c for c in rules.CHECKS if c.name == name)
    return check.run(facts, rules.Context(units, selected or [name], sections or {}))


def wheres(hits):
    return [h["where"] for h in hits]


def complete(*names):
    return {name: {"status": rules.COMPLETE} for name in names}


def finding(check="timeline_errors", where="timeline: Extrude1", severity="error", group="timeline", **extra):
    return {"check": check, "group": group, "severity": severity, "where": where, "message": "m",
            "why": None, "fix_tool": None, **extra}


def record(findings=(), fully_run=("timeline_errors",), extras=False):
    return rules.baseline_record("id", "Doc v1", "general", findings, fully_run, extras, now=100.0)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# -- The table ------------------------------------------------------------------


class TableTests(unittest.TestCase):
    def test_every_check_has_a_row_and_every_row_a_check(self):
        self.assertEqual(set(rules.SEVERITY_TABLE), set(rules.CHECK_NAMES))
        self.assertEqual(len(rules.CHECK_NAMES), len(set(rules.CHECK_NAMES)))

    def test_every_row_covers_every_profile_with_a_known_severity(self):
        for name, row in rules.SEVERITY_TABLE.items():
            with self.subTest(check=name):
                self.assertEqual(len(row), len(rules.PROFILES))
                self.assertTrue(set(row) <= {*rules.SEVERITIES, rules.OFF})

    def test_groups_and_sections_are_known(self):
        for check in rules.CHECKS:
            with self.subTest(check=check.name):
                self.assertIn(check.group, rules.GROUPS)
                self.assertIn(check.needs, rules.SECTIONS)
                self.assertTrue(check.why)

    def test_fix_tools_are_real_tools(self):
        names = {t["name"] for t in tool_surface.TOOL_DEFINITIONS}
        for check in rules.CHECKS:
            if check.fix_tool is not None:
                self.assertIn(check.fix_tool, names, check.name)

    def test_every_section_has_a_gatherer(self):
        self.assertEqual(set(ad.GATHERERS), set(rules.SECTIONS))

    def test_a_table_change_changes_the_severity_with_no_change_to_the_check(self):
        facts = {"timeline": {"report": {"rolled_back": True, "marker_position": 1, "count": 2}, "items": []}}
        selected, _ = rules.select_checks("general", ["timeline_rolled_back"])
        with mock.patch.dict(rules.SEVERITY_TABLE, {"timeline_rolled_back": (rules.INFO,) * 3}):
            findings, *_ = rules.judge(facts, complete("timeline"), selected, "general")
        self.assertEqual(findings[0]["severity"], rules.INFO)

    def test_generic_names_stay_info_in_every_profile(self):
        self.assertEqual(set(rules.SEVERITY_TABLE["generic_body_name"]), {rules.INFO})

    def test_unheld_occurrence_stays_info_in_every_profile(self):
        self.assertEqual(set(rules.SEVERITY_TABLE["unheld_occurrence"]), {rules.INFO})

    def test_assembly_needs_no_face_scan(self):
        selected, _ = rules.select_checks("assembly")
        self.assertNotIn("body_faces", rules.sections_for(selected))


class SelectionTests(unittest.TestCase):
    def test_a_profile_leaves_out_what_it_switches_off(self):
        selected, off = rules.select_checks("general")
        self.assertNotIn("face_count_outlier", [c.name for c in selected])
        self.assertIn("face_count_outlier", off)

    def test_groups_filter(self):
        selected, _ = rules.select_checks("general", groups=["file"])
        self.assertEqual({c.group for c in selected}, {"file"})

    def test_checks_filter(self):
        selected, off = rules.select_checks("general", checks=["joint_errors", "face_count_outlier"])
        self.assertEqual([c.name for c in selected], ["joint_errors"])
        self.assertEqual(off, ["face_count_outlier"])

    def test_sections_come_in_gathering_order_with_what_they_read_first(self):
        selected, _ = rules.select_checks("general", checks=["tiny_faces", "not_saved"])
        self.assertEqual(rules.sections_for(selected), ["file", "bodies", "body_faces"])

    def test_a_filtered_run_reads_only_what_it_needs(self):
        selected, _ = rules.select_checks("export-ready", groups=["parameters"])
        self.assertEqual(rules.sections_for(selected), ["parameters"])

    def test_general_reads_no_parameters(self):
        selected, off = rules.select_checks("general")
        self.assertNotIn("parameters", rules.sections_for(selected))
        self.assertIn("unused_user_parameter", off)


# -- file -----------------------------------------------------------------------


def file_facts(active=None, documents=(), related=(), latest=None):
    base = {"name": "Doc v3", "is_saved": True, "is_modified": False,
            "data_file": {"id": "a", "name": "Doc", "version_number": 3, "is_read_only": False}}
    return {"file": {"active": {**base, **(active or {})}, "documents": list(documents),
                     "related_ids": set(related), "latest_version": latest}}


def open_doc(file_id, version=1, name="Part", referenced_by=()):
    return {"name": f"{name} v{version}", "referenced_by": list(referenced_by),
            "data_file": {"id": file_id, "name": name, "version_number": version}}


class FileCheckTests(unittest.TestCase):
    def test_clean_file_finds_nothing(self):
        for name in ("not_saved", "unsaved_changes", "read_only", "old_version", "duplicate_instances"):
            self.assertEqual(run_check(name, file_facts()), [], name)

    def test_not_saved(self):
        self.assertEqual(wheres(run_check("not_saved", file_facts({"is_saved": False}))), ["file: Doc v3"])

    def test_an_unreadable_saved_flag_is_not_a_finding(self):
        self.assertEqual(run_check("not_saved", file_facts({"is_saved": None})), [])

    def test_unsaved_changes(self):
        self.assertEqual(len(run_check("unsaved_changes", file_facts({"is_modified": True}))), 1)

    def test_read_only(self):
        facts = file_facts({"data_file": {"id": "a", "is_read_only": True}})
        self.assertEqual(len(run_check("read_only", facts)), 1)

    def test_read_only_unknown_or_no_file_is_not_a_finding(self):
        self.assertEqual(run_check("read_only", file_facts({"data_file": {"is_read_only": "unknown"}})), [])
        self.assertEqual(run_check("read_only", file_facts({"data_file": None})), [])

    def test_old_version(self):
        hits = run_check("old_version", file_facts(latest=5))
        self.assertIn("version 3 is open; the latest is 5", hits[0]["message"])

    def test_old_version_needs_both_numbers(self):
        self.assertEqual(run_check("old_version", file_facts(latest=None)), [])
        self.assertEqual(run_check("old_version", file_facts(latest=3)), [])
        self.assertEqual(run_check("old_version", file_facts({"data_file": None}, latest=5)), [])

    def test_duplicates_of_a_related_file_use_document_status_wording(self):
        facts = file_facts(documents=[open_doc("k", 11, "Knuckle"), open_doc("k", 12, "Knuckle")], related={"k"})
        hits = run_check("duplicate_instances", facts)
        self.assertEqual(wheres(hits), ["file: Knuckle"])
        self.assertIn("2 open instances", hits[0]["message"])

    def test_duplicates_of_an_unrelated_file_are_not_this_designs_problem(self):
        facts = file_facts(documents=[open_doc("x"), open_doc("x")], related={"a"})
        self.assertEqual(run_check("duplicate_instances", facts), [])

    def test_documents_with_no_file_are_never_duplicates(self):
        docs = [{"name": "Untitled", "data_file": None}, {"name": "Untitled", "data_file": None}]
        self.assertEqual(run_check("duplicate_instances", file_facts(documents=docs, related={"a"})), [])


# -- timeline -------------------------------------------------------------------


def row(name="Extrude1", health="healthy", index=0, suppressed=False, entity_type="adsk::fusion::ExtrudeFeature",
        message=""):
    return {"name": name, "health": health, "index": index, "suppressed": suppressed,
            "entity_type": entity_type, "message": message}


def timeline_facts(*rows, rolled_back=False, is_root=True):
    return {"timeline": {"items": list(rows), "report": {
        "rolled_back": rolled_back, "marker_position": 3, "count": 9,
        "active_is_root": is_root, "active_component": "Sub"}}}


class TimelineCheckTests(unittest.TestCase):
    def test_errors_and_warnings_are_separate_checks(self):
        facts = timeline_facts(row("A", "error", 2, message="No target body"), row("B", "warning"), row("C"))
        hits = run_check("timeline_errors", facts)
        self.assertEqual(wheres(hits), ["timeline: A"])
        self.assertIn("step 2: No target body", hits[0]["message"])
        self.assertEqual(wheres(run_check("timeline_warnings", facts)), ["timeline: B"])

    def test_the_step_number_stays_out_of_where_so_an_insert_does_not_make_it_new(self):
        self.assertEqual(wheres(run_check("timeline_errors", timeline_facts(row("A", "error", 7)))),
                         wheres(run_check("timeline_errors", timeline_facts(row("A", "error", 8)))))

    def test_a_suppressed_item_is_not_an_error(self):
        self.assertEqual(run_check("timeline_errors", timeline_facts(row("A", "error", suppressed=True))), [])

    def test_rolled_back_items_read_as_unknown_and_are_not_errors(self):
        self.assertEqual(run_check("timeline_errors", timeline_facts(row("A", "unknown"))), [])

    def test_a_missing_message_is_said_plainly(self):
        self.assertIn("no message", run_check("timeline_errors", timeline_facts(row("A", "error")))[0]["message"])

    def test_a_broken_joint_is_reported_once_when_the_joint_check_ran(self):
        facts = timeline_facts(row("Joint1", "error", entity_type="adsk::fusion::Joint"), row("Cut1", "error"))
        hits = run_check("timeline_errors", facts, ["timeline_errors", "joint_errors"], complete("joints"))
        self.assertEqual(wheres(hits), ["timeline: Cut1"])

    def test_fusion_markup_is_stripped(self):
        message = "Body 1 missing<b>1 Reference Failures</b><br/>The target body is lost."
        hit = run_check("timeline_errors", timeline_facts(row("A", "error", 7, message=message)))[0]
        self.assertEqual(hit["message"], "step 7: Body 1 missing 1 Reference Failures The target body is lost.")

    def test_a_message_that_is_only_markup_counts_as_none(self):
        hit = run_check("timeline_errors", timeline_facts(row("A", "error", 7, message="<br/>")))[0]
        self.assertIn("no message", hit["message"])

    def test_a_broken_joint_stays_in_the_timeline_without_the_joint_check(self):
        facts = timeline_facts(row("Joint1", "error", entity_type="adsk::fusion::Joint"))
        self.assertEqual(len(run_check("timeline_errors", facts, sections=complete("joints"))), 1)

    def test_a_broken_joint_stays_in_the_timeline_when_joints_were_only_partly_read(self):
        facts = timeline_facts(row("Joint1", "error", entity_type="adsk::fusion::Joint"))
        hits = run_check("timeline_errors", facts, ["timeline_errors", "joint_errors"],
                         {"joints": {"status": rules.PARTIAL}})
        self.assertEqual(len(hits), 1)

    def test_rolled_back_marker(self):
        hits = run_check("timeline_rolled_back", timeline_facts(rolled_back=True))
        self.assertIn("3 of 9", hits[0]["message"])
        self.assertEqual(run_check("timeline_rolled_back", timeline_facts()), [])

    def test_suppressed_features(self):
        self.assertEqual(wheres(run_check("suppressed_features", timeline_facts(row("A", suppressed=True), row("B")))),
                         ["timeline: A"])

    def test_active_component(self):
        self.assertEqual(wheres(run_check("active_component", timeline_facts(is_root=False))), ["component: Sub"])
        self.assertEqual(run_check("active_component", timeline_facts()), [])


# -- links ----------------------------------------------------------------------


def link(status, name="Part"):
    return {"name": name, "status": status, "loaded_version": 2, "latest_version": 4, "data_file_id": "p"}


class LinkCheckTests(unittest.TestCase):
    def facts(self, *refs, indirect=0):
        return {"links": {"references": list(refs), "indirect": indirect}}

    def test_each_status_has_its_check(self):
        facts = self.facts(link("out_of_date", "A"), link("stale_not_flagged", "B"), link("unknown", "C"),
                           link("current", "D"))
        self.assertEqual(wheres(run_check("link_out_of_date", facts)), ["link: A"])
        self.assertEqual(wheres(run_check("link_stale_unflagged", facts)), ["link: B"])
        self.assertEqual(wheres(run_check("link_unreadable", facts)), ["link: C"])

    def test_a_nameless_link_still_has_a_location(self):
        facts = self.facts({"name": None, "data_file_id": None, "status": "unknown"})
        self.assertEqual(wheres(run_check("link_unreadable", facts)), ["link: unknown file"])

    def test_links_inside_sub_assemblies_are_declared_unchecked(self):
        self.assertIn("3 link(s)", run_check("indirect_links", self.facts(indirect=3))[0]["message"])
        self.assertEqual(run_check("indirect_links", self.facts()), [])


# -- bodies ---------------------------------------------------------------------


def metrics(key="root/Part", component="Root", **kw):
    base = {"key": key, "occurrence_path": "root", "component": component, "is_solid": True, "volume_cm3": 10.0,
            "lumps": 1, "shells": 1, "face_count": 6, "bbox": ((0, 0, 0), (1, 1, 1)), "instances": 1, "linked": False}
    base.update(kw)
    return base


def entry(body="Part", path="root", component="Root", visible=True, occurrence_visible=None, index=0, linked=False):
    return {"occurrence_path": path, "component": component, "linked": linked, "body": body, "index": index,
            "visible": visible, "occurrence_visible": occurrence_visible, "key": f"{path}/{body}"}


def body_facts(*measured, entries=()):
    return {"bodies": {"metrics": list(measured), "entries": list(entries)}}


class BodyCheckTests(unittest.TestCase):
    def test_a_sound_body_finds_nothing(self):
        facts = body_facts(metrics(faces=[{"index": 0, "area": 5.0, "perimeter": 9.0}], edge_lengths=[3.0]))
        for name in ("zero_volume", "non_solid", "tiny_faces", "short_edges", "sliver_faces", "multiple_lumps",
                     "extra_shells"):
            self.assertEqual(run_check(name, facts), [], name)

    def test_solid_check_problems_arrive_with_a_location_and_units(self):
        facts = body_facts(metrics("Frame:1/Body6", volume_cm3=0.0))
        hits = run_check("zero_volume", facts, units="mm")
        self.assertEqual(wheres(hits), ["body: Frame:1/Body6"])
        self.assertIn("mm^3", hits[0]["message"])

    def test_non_solid(self):
        self.assertEqual(len(run_check("non_solid", body_facts(metrics(is_solid=False)))), 1)

    def test_tiny_faces_and_short_edges(self):
        facts = body_facts(metrics(faces=[{"index": 0, "area": 1e-6, "perimeter": 0.5}], edge_lengths=[1e-5]))
        self.assertIn("1 tiny face(s)", run_check("tiny_faces", facts)[0]["message"])
        self.assertIn("1 short edge(s)", run_check("short_edges", facts)[0]["message"])

    def test_face_rules_say_nothing_about_a_body_not_scanned(self):
        facts = body_facts(metrics())
        self.assertEqual(run_check("tiny_faces", facts), [])
        self.assertEqual(run_check("sliver_faces", facts), [])

    def test_sliver_faces(self):
        facts = body_facts(metrics(faces=[{"index": 0, "area": 0.002, "perimeter": 10.0}]))
        self.assertIn("1 sliver face(s)", run_check("sliver_faces", facts)[0]["message"])

    def test_lumps_and_shells(self):
        facts = body_facts(metrics(lumps=2, shells=3))
        self.assertIn("2 disjoint", run_check("multiple_lumps", facts)[0]["message"])
        self.assertIn("3 shells", run_check("extra_shells", facts)[0]["message"])

    def test_instances_and_links_are_noted(self):
        facts = body_facts(metrics(lumps=2, instances=40, linked=True))
        self.assertIn("(40 instances, linked)", run_check("multiple_lumps", facts)[0]["message"])

    def test_casting_beside_bolts_is_no_outlier(self):
        """Live false alarm: a 92-face casting beside 3-face bolts, median 3."""
        bolts = [metrics(f"Bolt{i}:1/Body1", f"Bolt{i}", face_count=3) for i in range(6)]
        facts = body_facts(metrics("Knuckle:1/Casting", "Knuckle", face_count=92), *bolts)
        self.assertEqual(run_check("face_count_outlier", facts), [])
        selected, _ = rules.select_checks("general")
        self.assertNotIn("face_count_outlier", [c.name for c in selected])

    def test_outlier_among_siblings_of_one_component_is_found(self):
        peers = [metrics(f"root/B{i}", face_count=6) for i in range(4)]
        hits = run_check("face_count_outlier", body_facts(metrics("root/Twisted", face_count=200), *peers))
        self.assertEqual(wheres(hits), ["body: root/Twisted"])

    def test_outlier_needs_enough_siblings_to_compare(self):
        facts = body_facts(metrics("root/A", face_count=200), metrics("root/B", face_count=6))
        self.assertEqual(run_check("face_count_outlier", facts), [])

    def test_hidden_body(self):
        facts = body_facts(entries=[entry("Old", visible=False), entry("New")])
        self.assertEqual(wheres(run_check("hidden_bodies", facts)), ["body: root/Old"])

    def test_a_hidden_occurrence_is_one_finding_not_one_per_body(self):
        rows = [entry("A", "Sub:1", "Sub", False, False), entry("B", "Sub:1", "Sub", False, False, index=1),
                entry("C", "Sub:1+In:1", "In", False, False)]
        hits = run_check("hidden_bodies", body_facts(entries=rows))
        self.assertEqual(wheres(hits), ["occurrence: Sub:1"])
        self.assertIn("2 body(ies)", hits[0]["message"])

    def test_one_definition_hidden_in_many_instances_is_one_finding(self):
        rows = [entry("A", "Bolt:1", "Bolt", False, True), entry("A", "Bolt:2", "Bolt", False, True)]
        self.assertEqual(wheres(run_check("hidden_bodies", body_facts(entries=rows))), ["body: Bolt:1/A"])

    def test_generic_names(self):
        for name in ("Body6", "body 12", "Surface3", "Solid1", "Body1 (1)"):
            self.assertTrue(rules.GENERIC_BODY_NAME.match(name), name)
        for name in ("Body", "Lower Arm", "Bodywork1", "Body6 left", ""):
            self.assertFalse(rules.GENERIC_BODY_NAME.match(name), name)

    def test_generic_name_is_found_once_per_definition(self):
        rows = [entry("Body6", "Bolt:1", "Bolt", linked=True), entry("Body6", "Bolt:2", "Bolt", linked=True),
                entry("Arm")]
        hits = run_check("generic_body_name", body_facts(entries=rows))
        self.assertEqual(wheres(hits), ["body: Bolt:1/Body6"])
        self.assertIn("linked", hits[0]["message"])

    def test_a_nameless_body_is_not_generic(self):
        self.assertEqual(run_check("generic_body_name", body_facts(entries=[entry(None)])), [])

    def test_group_instances_keeps_same_named_bodies_apart(self):
        rows = [entry("A", index=0), entry("A", index=1), entry("A", "Sub:1", "Sub")]
        self.assertEqual([count for _, count in rules.group_instances(rows)], [1, 1, 1])


# -- joints ---------------------------------------------------------------------


def occurrence(path="Part:1", linked=False, grounded=False, ground_to_parent=True, joints=0, as_built=0, rigid=0):
    return {"path": path, "linked": linked, "grounded": grounded, "ground_to_parent": ground_to_parent,
            "joints": joints, "as_built_joints": as_built, "rigid_groups": rigid}


def joint_facts(*occurrences, joints=(), constrained=()):
    return {"joints": {"joints": list(joints), "occurrences": list(occurrences), "constrained_paths": set(constrained)}}


class JointCheckTests(unittest.TestCase):
    def test_joint_health(self):
        rows = [{"name": "J1", "component": "Root", "health": "error", "message": "Lost", "suppressed": False},
                {"name": "J2", "component": "Sub", "health": "warning", "message": "", "suppressed": False},
                {"name": "J3", "component": "Sub", "health": "error", "message": "", "suppressed": True},
                {"name": "J4", "component": "Sub", "health": "healthy", "message": "", "suppressed": False}]
        facts = joint_facts(joints=rows)
        hits = run_check("joint_errors", facts)
        self.assertEqual(wheres(hits), ["joint: Root/J1"])
        self.assertEqual(hits[0]["message"], "Lost")
        self.assertEqual(wheres(run_check("joint_warnings", facts)), ["joint: Sub/J2"])

    def test_a_top_level_occurrence_held_by_nothing(self):
        self.assertEqual(wheres(run_check("unheld_occurrence", joint_facts(occurrence()))), ["occurrence: Part:1"])

    def test_anything_that_holds_it_clears_it(self):
        for held in (occurrence(grounded=True), occurrence(joints=1), occurrence(as_built=1), occurrence(rigid=2)):
            self.assertEqual(run_check("unheld_occurrence", joint_facts(held)), [], held)

    def test_an_assembly_constraint_clears_it(self):
        self.assertEqual(run_check("unheld_occurrence", joint_facts(occurrence(), constrained=["Part:1"])), [])

    def test_what_could_not_be_read_is_not_evidence(self):
        self.assertEqual(run_check("unheld_occurrence", joint_facts(occurrence(joints=None))), [])
        self.assertEqual(run_check("unheld_occurrence", joint_facts(occurrence(grounded=None))), [])

    def test_nested_and_grounded_to_parent_moves_with_the_parent(self):
        self.assertEqual(run_check("unheld_occurrence", joint_facts(occurrence("A:1", grounded=True),
                                                                    occurrence("A:1+B:1"))), [])

    def test_nested_and_free_of_its_parent_is_unheld(self):
        facts = joint_facts(occurrence("A:1", grounded=True), occurrence("A:1+B:1", ground_to_parent=False))
        self.assertEqual(wheres(run_check("unheld_occurrence", facts)), ["occurrence: A:1+B:1"])

    def test_ground_to_parent_does_not_clear_a_top_level_occurrence(self):
        self.assertEqual(len(run_check("unheld_occurrence", joint_facts(occurrence(ground_to_parent=True)))), 1)

    def test_inside_a_link_is_the_linked_files_business(self):
        facts = joint_facts(occurrence("L:1", linked=True, grounded=True),
                            occurrence("L:1+B:1", linked=True, ground_to_parent=False))
        self.assertEqual(run_check("unheld_occurrence", facts), [])

    def test_is_under_needs_a_whole_path_segment(self):
        self.assertTrue(rules.is_under("A:1+B:1+C:1", {"A:1"}))
        self.assertFalse(rules.is_under("A:1", {"A:1"}))
        self.assertFalse(rules.is_under("A:11+B:1", {"A:1"}))


# -- sketches and parameters ----------------------------------------------------


def sketch(name="Sketch1", fully=True, derived=False, is_open=False):
    return {"name": name, "component": "Root", "fully_constrained": fully, "derived": derived,
            "profiles": 0 if is_open else 1, "open": is_open}


class SketchAndParameterTests(unittest.TestCase):
    def test_under_constrained(self):
        facts = {"sketches": {"sketches": [sketch("A", False), sketch("B"), sketch("C", None),
                                           sketch("D", False, derived=True)]}}
        self.assertEqual(wheres(run_check("under_constrained_sketch", facts)), ["sketch: Root/A"])

    def test_open_profile(self):
        facts = {"sketches": {"sketches": [sketch("A", is_open=True), sketch("B")]}}
        self.assertEqual(wheres(run_check("open_profile", facts)), ["sketch: Root/A"])

    def test_unused_user_parameter(self):
        facts = {"parameters": {"user": ["od", "wall", "spare"],
                                "expressions": {"od": "1.75 in", "wall": "0.12 in", "spare": "3 in",
                                                "idia": "od - 2 * wall", "d7": "idia"}}}
        hits = run_check("unused_user_parameter", facts)
        self.assertEqual(wheres(hits), ["parameter: spare"])

    def test_a_parameter_referencing_only_itself_is_unused(self):
        facts = {"parameters": {"user": ["a"], "expressions": {"a": "a * 1"}}}
        self.assertEqual(len(run_check("unused_user_parameter", facts)), 1)


# -- Judging --------------------------------------------------------------------


class JudgeTests(unittest.TestCase):
    def selected(self, *names):
        return rules.select_checks("export-ready", list(names))[0]

    def test_a_finding_has_the_documented_shape(self):
        facts = timeline_facts(row("A", "error"))
        findings, fully_run, ran, crashed = rules.judge(facts, complete("timeline"), self.selected("timeline_errors"),
                                                        "general")
        self.assertEqual(set(findings[0]), {"check", "group", "severity", "where", "message", "why", "fix_tool"})
        self.assertEqual(findings[0]["fix_tool"], "timeline_health")
        self.assertEqual((fully_run, ran, crashed), ({"timeline_errors"}, 1, []))

    def test_severity_follows_the_profile(self):
        facts = timeline_facts(rolled_back=True)
        selected = self.selected("timeline_rolled_back")
        self.assertEqual(rules.judge(facts, complete("timeline"), selected, "general")[0][0]["severity"], rules.WARNING)
        self.assertEqual(rules.judge(facts, complete("timeline"), selected, "export-ready")[0][0]["severity"], rules.ERROR)

    def test_a_section_never_read_runs_nothing(self):
        for status in (rules.SKIPPED, rules.FAILED, rules.NOT_APPLICABLE):
            findings, fully_run, ran, _ = rules.judge({}, {"timeline": {"status": status}},
                                                      self.selected("timeline_errors"), "general")
            self.assertEqual((findings, fully_run, ran), ([], set(), 0))

    def test_a_partial_section_keeps_findings_but_is_not_fully_run(self):
        facts = {"sketches": {"sketches": [sketch("A", False)]}}
        findings, fully_run, ran, _ = rules.judge(facts, {"sketches": {"status": rules.PARTIAL}},
                                                  self.selected("under_constrained_sketch"), "general")
        self.assertEqual((len(findings), fully_run, ran), (1, set(), 1))

    def test_unused_parameter_is_not_claimed_from_part_of_the_parameters(self):
        facts = {"parameters": {"user": ["od"], "expressions": {"od": "1 in"}}}
        findings, _, ran, _ = rules.judge(facts, {"parameters": {"status": rules.PARTIAL}},
                                          self.selected("unused_user_parameter"), "general")
        self.assertEqual((findings, ran), ([], 0))

    def test_a_crashing_check_costs_only_itself(self):
        facts = {"timeline": {"items": "not rows", "report": {"rolled_back": True, "marker_position": 1, "count": 2}}}
        findings, fully_run, _, crashed = rules.judge(
            facts, complete("timeline"), self.selected("timeline_errors", "timeline_rolled_back"), "general")
        self.assertEqual([c["check"] for c in crashed], ["timeline_errors"])
        self.assertEqual(fully_run, {"timeline_rolled_back"})
        self.assertEqual(len(findings), 1)

    def test_long_fusion_messages_are_cut(self):
        hits = run_check("timeline_errors", timeline_facts(row("A", "error", message="x\n" * 2000)))
        self.assertLessEqual(len(hits[0]["message"]), rules.MAX_MESSAGE_CHARS)


class CoverageTests(unittest.TestCase):
    def test_complete_only_when_everything_was_read(self):
        self.assertTrue(rules.is_complete(complete("file", "bodies"), []))
        self.assertTrue(rules.is_complete({"timeline": {"status": rules.NOT_APPLICABLE}}, []))
        self.assertFalse(rules.is_complete({"bodies": {"status": rules.PARTIAL}}, []))
        self.assertFalse(rules.is_complete({"bodies": {"status": rules.FAILED}}, []))
        self.assertFalse(rules.is_complete(complete("file"), [{"check": "x"}]))

    def test_skipped_names_the_section_and_its_checks(self):
        selected, _ = rules.select_checks("general", ["tiny_faces", "not_saved"])
        rows = rules.skipped_report({"file": {"status": rules.COMPLETE},
                                     "body_faces": {"status": rules.PARTIAL, "covered": 3, "total": 9, "reason": "r"}},
                                    selected, [])
        self.assertEqual(rows, [{"section": "body_faces", "status": rules.PARTIAL, "covered": 3, "total": 9,
                                 "reason": "r", "checks": ["tiny_faces"]}])

    def test_skipped_lists_crashes_and_switched_off_checks(self):
        rows = rules.skipped_report({}, [], [{"check": "a", "status": rules.FAILED, "reason": "boom"}], ["b"])
        self.assertEqual([r["check"] for r in rows], ["a", "b"])

    def test_budget(self):
        clock = Clock()
        budget = rules.Budget(10, clock)
        self.assertFalse(budget.expired())
        clock.now = 4.0
        self.assertEqual((budget.elapsed(), budget.remaining()), (4.0, 6.0))
        clock.now = 10.0
        self.assertTrue(budget.expired())

    def test_fits_budget(self):
        self.assertTrue(rules.fits_budget(0, 0.0, 10**6, 0.1))
        self.assertTrue(rules.fits_budget(100, 1.0, 100, 1.0))
        self.assertFalse(rules.fits_budget(100, 1.0, 1000, 5.0))


# -- Extra findings -------------------------------------------------------------


class ExtraFindingTests(unittest.TestCase):
    good = {"check": "custom_rule", "group": "custom", "severity": "warning", "where": "custom: A", "message": "short"}

    def test_a_minimal_finding_is_filled_in(self):
        self.assertEqual(rules.validate_extra_findings([self.good]),
                         [{**self.good, "why": None, "fix_tool": None, "source": rules.EXTRA_SOURCE}])

    def test_source_is_kept(self):
        self.assertEqual(rules.validate_extra_findings([{**self.good, "source": "x"}])[0]["source"], "x")

    def test_empty_list_is_valid(self):
        self.assertEqual(rules.validate_extra_findings([]), [])

    def test_refusals(self):
        bad = [
            ("not a list", "list"),
            ([1], "object"),
            ([{**self.good, "extra": 1}], "unknown key"),
            ([{**self.good, "where": " "}], "where"),
            ([{k: v for k, v in self.good.items() if k != "message"}], "message"),
            ([{**self.good, "severity": "fatal"}], "severity"),
            ([{**self.good, "why": 3}], "why"),
            ([{**self.good, "check": "timeline_errors"}], "built-in"),
            ([self.good] * (rules.MAX_EXTRA_FINDINGS + 1), "at most"),
        ]
        for value, text in bad:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, text):
                rules.validate_extra_findings(value)


# -- Baselines ------------------------------------------------------------------


class BaselineTests(unittest.TestCase):
    def test_new_resolved_unchanged(self):
        before = record([finding(where="timeline: A"), finding(where="timeline: B")])
        after = [finding(where="timeline: B"), finding(where="timeline: C")]
        labeled, resolved, not_compared = rules.compare_to_baseline(before, after, {"timeline_errors"}, False)
        self.assertEqual({f["where"]: f["change"] for f in labeled}, {"timeline: B": "unchanged", "timeline: C": "new"})
        self.assertEqual(wheres(resolved), ["timeline: A"])
        self.assertEqual(not_compared, [])

    def test_a_changed_message_is_the_same_finding(self):
        before = record([finding(message="version 2 of 4")])
        labeled, resolved, _ = rules.compare_to_baseline(before, [finding(message="version 2 of 5")],
                                                         {"timeline_errors"}, False)
        self.assertEqual((labeled[0]["change"], resolved), ("unchanged", []))

    def test_equal_identities_match_one_for_one(self):
        before = record([finding(), finding()])
        labeled, resolved, _ = rules.compare_to_baseline(before, [finding()], {"timeline_errors"}, False)
        self.assertEqual((labeled[0]["change"], len(resolved)), ("unchanged", 1))
        labeled, resolved, _ = rules.compare_to_baseline(record([finding()]), [finding(), finding()],
                                                         {"timeline_errors"}, False)
        self.assertEqual([f["change"] for f in labeled], ["unchanged", "new"])

    def test_a_check_that_did_not_run_resolves_nothing(self):
        """The trap: a budget skip or a filter must not read as a fix."""
        before = record([finding()])
        labeled, resolved, not_compared = rules.compare_to_baseline(before, [], set(), False)
        self.assertEqual(resolved, [])
        self.assertEqual(not_compared, ["timeline_errors"])

    def test_a_check_missing_from_the_baseline_finds_nothing_new(self):
        before = record([], fully_run=())
        labeled, _, not_compared = rules.compare_to_baseline(before, [finding()], {"timeline_errors"}, False)
        self.assertEqual(labeled[0]["change"], "not_compared")
        self.assertEqual(not_compared, ["timeline_errors"])

    def test_extras_compare_only_when_supplied_both_times(self):
        extra = finding("custom_rule", "custom: A", group="custom", source="x")
        labeled, resolved, _ = rules.compare_to_baseline(record([extra], extras=True), [], set(), True)
        self.assertEqual(wheres(resolved), ["custom: A"])
        _, resolved, not_compared = rules.compare_to_baseline(record([extra], extras=True), [], set(), False)
        self.assertEqual((resolved, not_compared), ([], ["custom_rule"]))
        labeled, _, _ = rules.compare_to_baseline(record([], extras=False), [extra], set(), True)
        self.assertEqual(labeled[0]["change"], "not_compared")

    def test_comparing_leaves_the_stored_findings_alone(self):
        stored = [finding()]
        rules.compare_to_baseline(record(stored), [stored[0]], {"timeline_errors"}, False)
        self.assertNotIn("change", stored[0])

    def test_store_keeps_the_most_recent(self):
        store = rules.BaselineStore(limit=2)
        tokens = [store.store({"n": i}) for i in range(3)]
        self.assertIsNone(store.get(tokens[0]))
        self.assertEqual(store.get(tokens[2]), {"n": 2})
        self.assertEqual(len(set(tokens)), 3)

    def test_changes_report_counts_all_but_lists_up_to_the_cap(self):
        labeled = [{**finding(where=f"timeline: {i}"), "change": "new"} for i in range(3)]
        labeled.append({**finding(where="timeline: u"), "change": "unchanged"})
        labeled.append({**finding(where="timeline: n"), "change": "not_compared"})
        report = rules.changes_report("t", record(), labeled, [finding()], ["x"], 2, now=160.0)
        self.assertEqual((report["new"], report["resolved"], report["unchanged"], report["not_compared"]), (3, 1, 1, 1))
        self.assertEqual(len(report["new_findings"]), 2)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["baseline_age_seconds"], 60.0)


# -- The report -----------------------------------------------------------------


def report_of(findings, complete_run=True, max_findings=100, changes=None):
    return rules.build_report(findings, document="Doc", profile="general", units="in", token="t",
                              complete=complete_run, checks_run=1, elapsed_s=1.26, skipped=[],
                              max_findings=max_findings, changes=changes)


class CollapseTests(unittest.TestCase):
    def test_repeats_become_one_row_with_a_count_and_a_sample(self):
        findings = [finding("a", str(i), "info", "bodies") for i in range(5)]
        rows = rules.collapse(findings)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["check"], rows[0]["count"], rows[0]["where"]), ("a", 5, "5 places"))
        self.assertEqual(rows[0]["sample"], [{"where": str(i), "message": "m"} for i in range(3)])
        self.assertEqual((rows[0]["group"], rows[0]["severity"], rows[0]["message"]), ("bodies", "info", "m"))

    def test_a_lone_finding_stays_as_it_is(self):
        only = finding("a", "1")
        self.assertEqual(rules.collapse([only]), [only])

    def test_nothing_in_is_nothing_out(self):
        self.assertEqual(rules.collapse([]), [])

    def test_two_is_already_a_repeat(self):
        rows = rules.collapse([finding("a", "1"), finding("a", "2")])
        self.assertEqual([row["count"] for row in rows], [2])
        self.assertEqual(len(rows[0]["sample"]), 2)

    def test_different_messages_are_not_reported_as_the_first_one(self):
        findings = [{**finding("a", "1"), "message": "x"}, {**finding("a", "2"), "message": "y"}]
        row = rules.collapse(findings)[0]
        self.assertNotIn(row["message"], ("x", "y"))
        self.assertEqual([s["message"] for s in row["sample"]], ["x", "y"])

    def test_a_row_keeps_the_position_of_its_first_member(self):
        findings = [finding("a", "1", "error"), finding("b", "2", "warning"), finding("a", "3", "error")]
        self.assertEqual([row["check"] for row in rules.collapse(findings)], ["a", "b"])

    def test_one_check_at_two_severities_stays_two_rows(self):
        findings = [finding("x", "1", "error", "custom", source="s"), finding("x", "2", "error", "custom", source="s"),
                    finding("x", "3", "info", "custom", source="s")]
        self.assertEqual([row.get("count") for row in rules.collapse(findings)], [2, None])

    def test_two_sources_of_one_check_stay_apart(self):
        findings = [finding("x", "1", "info", "custom", source="a"), finding("x", "2", "info", "custom", source="b")]
        self.assertEqual(len(rules.collapse(findings)), 2)

    def test_a_named_check_stays_individual_and_the_rest_fold(self):
        findings = [finding("a", "1"), finding("a", "2"), finding("b", "3"), finding("b", "4")]
        rows = rules.collapse(findings, expanded=["a"])
        self.assertEqual(wheres(rows), ["1", "2", "2 places"])

    def test_detail_keeps_everything(self):
        findings = [finding("a", "1"), finding("a", "2")]
        self.assertEqual(rules.collapse(findings, detail=True), findings)

    def test_folding_leaves_the_findings_themselves_alone(self):
        findings = [finding("a", "1"), finding("a", "2")]
        rules.collapse(findings)
        self.assertEqual(set(findings[0]), set(finding()))

    def test_changes_are_counted_and_the_row_reads_as_the_first(self):
        findings = [finding("a", "1", change="new"), finding("a", "2", change="unchanged"),
                    finding("a", "3", change="unchanged")]
        row = rules.collapse(findings)[0]
        self.assertEqual((row["change"], row["change_counts"]), ("new", {"new": 1, "unchanged": 2}))
        self.assertEqual(row["sample"][0], {"where": "1", "message": "m", "change": "new"})

    def test_the_cap_counts_rows_and_the_summary_counts_findings(self):
        findings = [finding("a", str(i)) for i in range(50)] + [finding("b", "z")]
        report = rules.build_report(findings, document="D", profile="general", units="in", token="t", complete=True,
                                    checks_run=2, elapsed_s=0, skipped=[], max_findings=2, show=rules.collapse)
        self.assertEqual(len(report["findings"]), 2)
        self.assertFalse(report["findings_truncated"])
        self.assertEqual(report["summary"]["findings"], 51)

    def test_changes_fold_with_the_same_rows(self):
        labeled = [finding("a", str(i), change="new") for i in range(4)]
        changes = rules.changes_report("t", record(), labeled, [], [], 100, now=100.0, show=rules.collapse)
        self.assertEqual(changes["new"], 4)
        self.assertEqual(len(changes["new_findings"]), 1)
        self.assertEqual(changes["new_findings"][0]["count"], 4)


class FirstIssuesTests(unittest.TestCase):
    def test_broken_items_come_in_step_order(self):
        facts = timeline_facts(row("Late", "error", 9), row("Early", "warning", 2), row("Fine", "healthy", 1))
        rows, total = rules.first_issues(facts, 20)
        self.assertEqual([(r["step"], r["name"], r["severity"]) for r in rows],
                         [(2, "Early", "warning"), (9, "Late", "error")])
        self.assertEqual(total, 2)

    def test_suppressed_items_are_left_out(self):
        self.assertEqual(rules.first_issues(timeline_facts(row("A", "error", 1, suppressed=True)), 20), ([], 0))

    def test_a_clean_timeline_is_an_empty_list_and_an_unread_one_is_none(self):
        self.assertEqual(rules.first_issues(timeline_facts(row("A", "healthy", 1)), 20), ([], 0))
        self.assertEqual(rules.first_issues({}, 20), (None, None))

    def test_a_broken_joint_stays_in_its_place(self):
        facts = timeline_facts(row("Joint1", "error", 3, entity_type="adsk::fusion::Joint"), row("Cut1", "error", 8))
        self.assertEqual([r["name"] for r in rules.first_issues(facts, 20)[0]], ["Joint1", "Cut1"])

    def test_the_limit_keeps_the_earliest_and_the_total_counts_all(self):
        facts = timeline_facts(*[row(f"F{i}", "error", 10 - i) for i in range(5)])
        rows, total = rules.first_issues(facts, 2)
        self.assertEqual([r["step"] for r in rows], [6, 7])
        self.assertEqual(total, 5)

    def test_an_item_with_no_slot_sits_at_the_step_before_it(self):
        facts = timeline_facts(row("A", "healthy", 4), row("Inside", "error", None), row("B", "error", 5))
        rows, _ = rules.first_issues(facts, 20)
        self.assertEqual([(r["step"], r["name"]) for r in rows], [(4, "Inside"), (5, "B")])

    def test_messages_are_plain_and_short(self):
        facts = timeline_facts(row("A", "error", 1, message="<b>x</b>" + "y" * 500))
        message = rules.first_issues(facts, 20)[0][0]["message"]
        self.assertNotIn("<", message)
        self.assertLessEqual(len(message), rules.MAX_MESSAGE_CHARS)


class ReportTests(unittest.TestCase):
    def test_key_order_puts_the_verdict_first_and_skipped_last(self):
        keys = list(report_of([], changes={"new": 0}))
        self.assertEqual(keys[:2], ["ok", "summary"])
        self.assertEqual(keys[-3:], ["findings", "findings_truncated", "skipped"])
        self.assertLess(keys.index("changes"), keys.index("findings"))

    def test_worst_first_then_by_group_order(self):
        findings = [finding("c", "1", "info", "file"), finding("b", "2", "warning", "parameters"),
                    finding("a", "3", "warning", "file"), finding("d", "4", "error", "joints"),
                    finding("e", "5", "warning", "custom")]
        self.assertEqual(wheres(report_of(findings)["findings"]), ["4", "3", "2", "5", "1"])

    def test_new_comes_first_within_a_severity(self):
        findings = [finding("a", "1", change="unchanged"), finding("b", "2", change="new")]
        self.assertEqual(wheres(report_of(findings)["findings"]), ["2", "1"])

    def test_summary_counts_everything_even_past_the_cap(self):
        findings = [finding(where=str(i)) for i in range(5)] + [finding("z", "a", "info", "custom")]
        report = report_of(findings, max_findings=2)
        self.assertEqual(len(report["findings"]), 2)
        self.assertTrue(report["findings_truncated"])
        self.assertEqual(report["summary"]["findings"], 6)
        self.assertEqual(report["summary"]["by_severity"], {"error": 5, "warning": 0, "info": 1})
        self.assertEqual(report["summary"]["by_group"], {"timeline": {"error": 5, "warning": 0, "info": 0},
                                                         "custom": {"error": 0, "warning": 0, "info": 1}})
        self.assertEqual(report["summary"]["by_check"], {"timeline_errors": 5, "z": 1})

    def test_ok(self):
        self.assertTrue(report_of([])["ok"])
        self.assertTrue(report_of([finding(severity="info")])["ok"])
        self.assertFalse(report_of([finding(severity="warning")])["ok"])
        self.assertFalse(report_of([finding(severity="error")])["ok"])

    def test_a_clean_report_on_part_of_a_design_is_not_ok(self):
        self.assertFalse(report_of([], complete_run=False)["ok"])


# -- Arguments ------------------------------------------------------------------


class ArgumentTests(unittest.TestCase):
    def test_defaults(self):
        opts = ad.validate_arguments({})
        self.assertEqual((opts["profile"], opts["units"], opts["time_budget_s"], opts["max_findings"]),
                         ("general", "in", 90.0, 100))
        self.assertIsNone(opts["extra_findings"])

    def test_extra_findings_empty_is_not_absent(self):
        self.assertEqual(ad.validate_arguments({"extra_findings": []})["extra_findings"], [])

    def test_refusals(self):
        bad = [
            ("nope", "object"),
            ({"bogus": 1}, "unknown argument"),
            ({"profile": "strict"}, "profile"),
            ({"units": "ft"}, "units"),
            ({"checks": ["joint_errors"], "groups": ["file"]}, "not both"),
            ({"checks": ["nope"]}, "unknown checks"),
            ({"checks": []}, "non-empty"),
            ({"groups": ["custom"]}, "unknown groups"),
            ({"groups": "file"}, "non-empty"),
            ({"time_budget_s": 0}, "time_budget_s"),
            ({"time_budget_s": 120}, "time_budget_s"),
            ({"time_budget_s": True}, "time_budget_s"),
            ({"time_budget_s": float("nan")}, "time_budget_s"),
            ({"max_findings": 0}, "max_findings"),
            ({"baseline_token": " "}, "baseline_token"),
            ({"extra_findings": {}}, "extra_findings"),
        ]
        for arguments, text in bad:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, text):
                ad.validate_arguments(arguments)

    def test_the_budget_stays_under_the_main_thread_limit(self):
        from settings import MCP_MAIN_THREAD_TIMEOUT
        self.assertLess(ad.MAX_TIME_BUDGET_S, MCP_MAIN_THREAD_TIMEOUT)

    def test_schema_matches_the_validator(self):
        tool = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "audit_design")
        props = tool["inputSchema"]["properties"]
        self.assertEqual(set(props), ad._KNOWN_ARGS)
        self.assertEqual(props["profile"]["enum"], list(rules.PROFILES))
        self.assertEqual(set(props["groups"]["items"]["enum"]), set(rules.GROUPS))
        self.assertEqual(props["time_budget_s"]["maximum"], ad.MAX_TIME_BUDGET_S)
        self.assertEqual(props["max_findings"]["maximum"], rules.MAX_FINDINGS_LIMIT)
        self.assertEqual(set(props["extra_findings"]["items"]["properties"]), rules.EXTRA_KEYS)


# -- Fakes ----------------------------------------------------------------------


class Fake:
    """A read-only Fusion object: the audit must never assign to one."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def __setattr__(self, name, value):
        raise AssertionError(f"audit_design wrote {name!r}: it is read-only")


class Coll(Fake):
    def __init__(self, members=()):
        self.__dict__["_members"] = list(members)
        self.__dict__["count"] = len(self._members)

    def item(self, index):
        return self._members[index]


def fake_body(name, faces=6, visible=True, area=5.0, volume=10.0):
    box = Fake(minPoint=Fake(x=0, y=0, z=0), maxPoint=Fake(x=1, y=1, z=1))
    edge = Fake(length=2.0)
    face_list = [Fake(area=area, edges=Coll([edge] * 4)) for _ in range(faces)]
    return Fake(name=name, isVisible=visible, isSolid=True, volume=volume, boundingBox=box, lumps=Coll([1]),
                shells=Coll([1]), faces=Coll(face_list), edges=Coll([edge] * 4))


def fake_occurrence(path, component, bodies=(), visible=True, grounded=False, joints=0, linked=False):
    return Fake(fullPathName=path, name=path, component=component, bRepBodies=Coll(bodies), isVisible=visible,
                isReferencedComponent=linked, isGrounded=grounded, isGroundToParent=True, joints=Coll([1] * joints),
                asBuiltJoints=Coll(), rigidGroups=Coll())


def fake_sketch(name, fully=True, profiles=1, curves=()):
    return Fake(name=name, isFullyConstrained=fully, isDerived=False, profiles=Coll([1] * profiles),
                sketchCurves=Coll(curves))


def fake_param(name, expression):
    return Fake(name=name, expression=expression)


def fake_design(bodies=(), occurrences=(), sketches=(), joints=(), user=(), model=(), timeline=()):
    root = Fake(name="Root", bRepBodies=Coll(bodies), allOccurrences=Coll(occurrences), allJoints=Coll(joints),
                allAssemblyConstraints=Coll(), sketches=Coll(sketches))
    line = Fake(count=len(timeline), markerPosition=len(timeline), item=lambda i: timeline[i])
    return Fake(rootComponent=root, activeComponent=root, timeline=line, allComponents=Coll([root]),
                userParameters=Coll(user), allParameters=Coll([*user, *model]))


def fake_document(name="Chassis v54", file_id="urn:chassis", references=()):
    data_file = Fake(id=file_id, name="Chassis", versionNumber=54, latestVersionNumber=54, isReadOnly=True,
                     parentProject=None, parentFolder=None)
    return Fake(name=name, isActive=True, isModified=False, isSaved=True, dataFile=data_file, products=Coll(),
                documentReferences=Coll(references), allDocumentReferences=Coll(references), objectType="Doc")


def fake_app(document):
    return Fake(activeDocument=document, documents=Coll([document]), activeProduct=object(),
                data=Fake(findFileById=lambda file_id: document.dataFile))


def state_for(design, document=None, budget=None):
    document = document or fake_document()
    return ad.Gathering(fake_app(document), design, document, budget or rules.Budget(90))


class SpentBudget:
    """Expired after *reads* checks of expired()."""

    def __init__(self, reads=0):
        self.reads = reads

    def expired(self):
        self.reads -= 1
        return self.reads < 0

    def remaining(self):
        return 50.0

    def elapsed(self):
        return 1.0


# -- Gathering ------------------------------------------------------------------


class GatherFlowTests(unittest.TestCase):
    def state(self, budget=None):
        return Fake(budget=budget or rules.Budget(90))

    def test_one_failed_section_costs_only_itself(self):
        def broken(state):
            raise RuntimeError("boom")

        facts, status = ad.gather(self.state(), ["timeline", "file"],
                                  {"timeline": broken, "file": lambda state: ({"a": 1}, None)})
        self.assertEqual(status["timeline"]["status"], rules.FAILED)
        self.assertIn("boom", status["timeline"]["reason"])
        self.assertEqual(status["file"], {"status": rules.COMPLETE})
        self.assertEqual(facts, {"file": {"a": 1}})

    def test_a_spent_budget_skips_the_rest(self):
        called = []
        gatherers = {"file": lambda state: (called.append("file") or {}, None),
                     "bodies": lambda state: (called.append("bodies") or {}, None)}
        _, status = ad.gather(self.state(SpentBudget(1)), ["file", "bodies"], gatherers)
        self.assertEqual(called, ["file"])
        self.assertEqual(status["bodies"], {"status": rules.SKIPPED, "reason": ad.TIME_REASON})

    def test_coverage_makes_a_section_partial(self):
        _, status = ad.gather(self.state(), ["file"], {"file": lambda state: ({}, {"covered": 1, "total": 2, "reason": "r"})})
        self.assertEqual(status["file"], {"status": rules.PARTIAL, "covered": 1, "total": 2, "reason": "r"})

    def test_not_applicable_is_not_a_failure(self):
        def direct(state):
            raise ad.NotApplicable("a direct design has no timeline")

        _, status = ad.gather(self.state(), ["timeline"], {"timeline": direct})
        self.assertEqual(status["timeline"]["status"], rules.NOT_APPLICABLE)

    def test_the_face_scan_is_skipped_when_bodies_were_not_read(self):
        def broken(state):
            raise RuntimeError("boom")

        _, status = ad.gather(self.state(), ["bodies", "body_faces"],
                              {"bodies": broken, "body_faces": lambda state: self.fail("must not run")})
        self.assertEqual(status["body_faces"]["status"], rules.SKIPPED)

    def test_the_face_scan_is_no_more_complete_than_the_bodies(self):
        gatherers = {"bodies": lambda state: ({}, {"covered": 1, "total": 2, "reason": "r"}),
                     "body_faces": lambda state: ({}, None)}
        _, status = ad.gather(self.state(), ["bodies", "body_faces"], gatherers)
        self.assertEqual(status["body_faces"]["status"], rules.PARTIAL)


class GatherSectionTests(unittest.TestCase):
    def test_file(self):
        data, coverage = ad.gather_file(state_for(fake_design()))
        self.assertIsNone(coverage)
        self.assertEqual(data["active"]["name"], "Chassis v54")
        self.assertEqual(data["related_ids"], {"urn:chassis"})
        self.assertEqual(data["latest_version"], 54)

    def test_timeline_reads_through_timeline_health(self):
        item = Fake(name="Extrude1", isGroup=False, entity=None, healthState=2, index=0,
                    errorOrWarningMessage="No target", isSuppressed=False)
        data, _ = ad.gather_timeline(state_for(fake_design(timeline=[item])))
        self.assertEqual(data["items"][0]["health"], "error")
        self.assertFalse(data["report"]["rolled_back"])

    def test_a_direct_design_has_no_timeline(self):
        types = Fake(DirectDesignType=0)
        design = fake_design()
        design.__dict__["designType"] = 0
        with mock.patch.object(adsk.fusion, "DesignTypes", types, create=True), self.assertRaises(ad.NotApplicable):
            ad.gather_timeline(state_for(design))

    def test_links_use_the_update_references_reader(self):
        part = Fake(id="urn:part", name="Part", versionNumber=2, latestVersionNumber=4)
        ref = Fake(dataFile=part, version=2, isOutOfDate=True)
        data, coverage = ad.gather_links(state_for(fake_design(), fake_document(references=[ref])))
        self.assertEqual(data["references"][0]["status"], "out_of_date")
        self.assertIsNone(coverage)

    def test_links_past_the_cloud_budget_are_partial(self):
        part = Fake(id="urn:part", name="Part", versionNumber=2, latestVersionNumber=4)
        document = fake_document(references=[Fake(dataFile=part, version=2, isOutOfDate=False)])
        state = state_for(fake_design(), document)
        state.__dict__["cloud"] = ad.CloudLookups(state.app, -1)
        _, coverage = ad.gather_links(state)
        self.assertEqual((coverage["covered"], coverage["total"]), (0, 1))

    def test_joints_and_occurrences(self):
        joint = Fake(name="J1", parentComponent=Fake(name="Root"), healthState=2, errorOrWarningMessage="Lost",
                     isSuppressed=False)
        design = fake_design(occurrences=[fake_occurrence("A:1", Fake(name="A"), joints=1)], joints=[joint])
        data, coverage = ad.gather_joints(state_for(design))
        self.assertIsNone(coverage)
        self.assertEqual(data["joints"][0]["health"], "error")
        self.assertEqual(data["occurrences"][0]["joints"], 1)

    def test_an_unreadable_joint_list_on_an_occurrence_is_none_not_zero(self):
        occurrence = Fake(fullPathName="A:1", isGrounded=False)
        self.assertIsNone(ad._read_occurrence(occurrence)["joints"])

    def test_assembly_constraints_count_as_holding(self):
        held = Fake(fullPathName="A:1")
        relationship = Fake(entityOne=Fake(assemblyContext=held), entityTwo=Fake(assemblyContext=None))
        self.assertEqual(ad._constrained_paths(Fake(geometricRelationships=Coll([relationship]))), {"A:1"})

    def test_joints_stop_at_the_budget(self):
        occurrences = [fake_occurrence(f"A:{i}", Fake(name="A")) for i in range(3)]
        design = fake_design(occurrences=occurrences)
        data, coverage = ad.gather_joints(state_for(design, budget=SpentBudget(1)))
        self.assertEqual(len(data["occurrences"]), 1)
        self.assertEqual((coverage["covered"], coverage["total"]), (1, 3))

    def test_sketches(self):
        real = Fake(isConstruction=False, isReference=False)
        design = fake_design(sketches=[fake_sketch("Path", False, 0, [real]), fake_sketch("Plate")])
        data, coverage = ad.gather_sketches(state_for(design))
        self.assertIsNone(coverage)
        self.assertEqual([(s["name"], s["open"]) for s in data["sketches"]], [("Path", True), ("Plate", False)])

    def test_construction_or_projected_curves_alone_are_not_open(self):
        curves = [Fake(isConstruction=True, isReference=False), Fake(isConstruction=False, isReference=True)]
        self.assertFalse(ad._read_sketch(fake_sketch("S", profiles=0, curves=curves), "Root")["open"])

    def test_an_empty_sketch_is_not_open(self):
        self.assertFalse(ad._read_sketch(fake_sketch("S", profiles=0), "Root")["open"])

    def test_sketches_of_linked_components_are_left_out(self):
        linked = Fake(name="Stock", sketches=Coll([fake_sketch("Theirs", False)]))
        design = fake_design(sketches=[fake_sketch("Ours")], occurrences=[fake_occurrence("S:1", linked, linked=True)])
        design.__dict__["allComponents"] = Coll([design.rootComponent, linked])
        data, _ = ad.gather_sketches(state_for(design))
        self.assertEqual([s["name"] for s in data["sketches"]], ["Ours"])

    def test_sketches_stop_at_the_budget(self):
        design = fake_design(sketches=[fake_sketch("A"), fake_sketch("B")])
        data, coverage = ad.gather_sketches(state_for(design, budget=SpentBudget(1)))
        self.assertEqual(len(data["sketches"]), 1)
        self.assertEqual((coverage["covered"], coverage["total"]), (1, 2))

    def test_parameters(self):
        design = fake_design(user=[fake_param("od", "1 in")], model=[fake_param("d1", "od")])
        data, coverage = ad.gather_parameters(state_for(design))
        self.assertIsNone(coverage)
        self.assertEqual(data, {"user": ["od"], "expressions": {"od": "1 in", "d1": "od"}})

    def test_parameters_stop_at_the_budget(self):
        design = fake_design(user=[fake_param("od", "1 in")], model=[fake_param("d1", "od")])
        _, coverage = ad.gather_parameters(state_for(design, budget=SpentBudget(1)))
        self.assertEqual((coverage["covered"], coverage["total"]), (1, 2))

    def test_a_definition_is_measured_once_and_hidden_bodies_not_at_all(self):
        bolt = Fake(name="Bolt")
        body = fake_body("Body1")
        occurrences = [fake_occurrence(f"Bolt:{i}", bolt, [body]) for i in range(1, 4)]
        occurrences.append(fake_occurrence("Old:1", Fake(name="Old"), [fake_body("Stale", visible=False)], visible=False))
        state = state_for(fake_design(occurrences=occurrences))
        data, coverage = ad.gather_bodies(state)
        self.assertIsNone(coverage)
        self.assertEqual(len(data["entries"]), 4)
        self.assertEqual([(m["key"], m["instances"]) for m in data["metrics"]], [("Bolt:1/Body1", 3)])

    def test_gathered_bodies_hold_no_fusion_objects(self):
        data, _ = ad.gather_bodies(state_for(fake_design(bodies=[fake_body("A")])))
        self.assertFalse([key for row in data["entries"] for key in row if key.startswith("_")])
        json.dumps(data)

    def test_bodies_past_the_budget_are_partial(self):
        data, coverage = ad.gather_bodies(state_for(fake_design(bodies=[fake_body("A"), fake_body("B")]),
                                                    budget=SpentBudget(1)))
        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual((coverage["covered"], coverage["total"]), (1, None))

    def test_the_face_scan_runs_smallest_first_and_stops_at_the_budget(self):
        state = state_for(fake_design(bodies=[fake_body("Big", 30), fake_body("Small", 2), fake_body("Mid", 10)]))
        ad.gather_bodies(state)
        state.__dict__["budget"] = SpentBudget(2)
        _, coverage = ad.gather_body_faces(state)
        scanned = sorted(m["key"] for _, m in state.measured if "faces" in m)
        self.assertEqual(scanned, ["root/Mid", "root/Small"])
        self.assertEqual((coverage["covered"], coverage["total"]), (2, 3))

    def test_the_face_scan_stops_before_a_body_that_would_not_fit(self):
        clock = Clock()
        state = state_for(fake_design(bodies=[fake_body("Big", 3000), fake_body("Small", 2)]))
        ad.gather_bodies(state)
        state.__dict__["budget"] = rules.Budget(20, clock)
        real = ad.sc.measure_details

        def slow(entry, metrics):
            clock.now += 1.0
            return real(entry, metrics)

        with mock.patch.object(ad.sc, "measure_details", slow):
            _, coverage = ad.gather_body_faces(state)
        self.assertEqual(coverage["covered"], 1)

    def test_a_body_too_big_to_scan_keeps_the_section_partial(self):
        state = state_for(fake_design(bodies=[fake_body("A", 2)]))
        ad.gather_bodies(state)
        state.measured[0][1]["face_count"] = ad.sc.MAX_FACE_SCAN + 1
        _, coverage = ad.gather_body_faces(state)
        self.assertEqual((coverage["covered"], coverage["total"]), (0, 1))


# -- The tool end to end --------------------------------------------------------


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.document = fake_document()
        self.design = fake_design(
            bodies=[fake_body("Body6"), fake_body("Shell", area=1e-7)],
            sketches=[fake_sketch("Sketch1", False)],
            occurrences=[fake_occurrence("Part:1", Fake(name="Part"), [fake_body("Lug")])],
            user=[fake_param("spare", "1 in")],
        )
        cast = Fake(cast=lambda product: self.design)
        patches = [mock.patch.object(ad, "get_app", lambda: fake_app(self.document)),
                   mock.patch.object(adsk.fusion, "Design", cast, create=True),
                   mock.patch.object(ad, "BASELINES", rules.BaselineStore())]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_tool(self, **arguments):
        result = ad.audit_design(arguments)
        text = result["content"][0]["text"]
        return json.loads(text) if not result["isError"] else text

    def test_a_full_run_on_fakes(self):
        report = self.run_tool()
        self.assertTrue(report["complete"], report["skipped"])
        checks = {f["check"] for f in report["findings"]}
        self.assertTrue({"generic_body_name", "tiny_faces", "under_constrained_sketch",
                         "unheld_occurrence", "read_only"} <= checks, checks)
        self.assertNotIn("unused_user_parameter", checks)
        self.assertTrue(report["baseline_token"])
        self.assertNotIn("changes", report)

    def test_export_ready_raises_what_general_keeps_as_info(self):
        report = self.run_tool(profile="export-ready")
        self.assertFalse(report["ok"])
        self.assertEqual(next(f for f in report["findings"] if f["check"] == "tiny_faces")["severity"], "warning")

    def test_a_group_filter_gathers_only_that_group(self):
        report = self.run_tool(profile="export-ready", groups=["parameters"])
        self.assertEqual({f["group"] for f in report["findings"]}, {"parameters"})
        self.assertEqual(report["checks_run"], 1)

    def test_a_group_the_profile_switches_off_says_so(self):
        report = self.run_tool(groups=["parameters"])
        self.assertEqual((report["findings"], report["checks_run"]), ([], 0))
        self.assertEqual([(row["check"], row["status"]) for row in report["skipped"]],
                         [("unused_user_parameter", "off")])

    def test_a_named_check_the_profile_switches_off_is_listed_not_silent(self):
        report = self.run_tool(checks=["face_count_outlier"])
        self.assertEqual(report["skipped"][0]["check"], "face_count_outlier")
        self.assertEqual(report["skipped"][0]["status"], "off")

    def test_baseline_round_trip(self):
        first = self.run_tool(profile="export-ready")
        self.design.userParameters._members.clear()
        self.design.userParameters.__dict__["count"] = 0
        self.design.allParameters._members.clear()
        self.design.allParameters.__dict__["count"] = 0
        self.design.rootComponent.sketches._members.append(fake_sketch("Sketch2", False))
        self.design.rootComponent.sketches.__dict__["count"] = 2
        second = self.run_tool(profile="export-ready", baseline_token=first["baseline_token"])
        changes = second["changes"]
        self.assertEqual([f["where"] for f in changes["new_findings"]], ["sketch: Root/Sketch2"])
        self.assertEqual([f["where"] for f in changes["resolved_findings"]], ["parameter: spare"])
        self.assertEqual(changes["unchanged"], first["summary"]["findings"] - 1)
        self.assertNotEqual(second["baseline_token"], first["baseline_token"])

    def test_a_narrower_run_does_not_call_the_rest_resolved(self):
        first = self.run_tool(profile="export-ready")
        second = self.run_tool(profile="export-ready", groups=["parameters"], baseline_token=first["baseline_token"])
        self.assertEqual(second["changes"]["resolved"], 0)
        self.assertEqual(second["changes"]["unchanged"], 1)
        self.assertIn("generic_body_name", second["changes"]["not_compared_checks"])

    def many_generic_names(self):
        self.design = fake_design(bodies=[fake_body(f"Body{i}") for i in range(1, 7)])

    def test_repeats_fold_by_default_and_the_summary_still_counts_them(self):
        self.many_generic_names()
        report = self.run_tool(groups=["bodies"])
        rows = [f for f in report["findings"] if f["check"] == "generic_body_name"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["count"], rows[0]["where"]), (6, "6 places"))
        self.assertEqual(len(rows[0]["sample"]), rules.COLLAPSE_SAMPLE_SIZE)
        self.assertEqual(report["summary"]["by_check"]["generic_body_name"], 6)

    def test_detail_lists_every_finding(self):
        self.many_generic_names()
        report = self.run_tool(groups=["bodies"], detail=True)
        rows = [f for f in report["findings"] if f["check"] == "generic_body_name"]
        self.assertEqual(len(rows), 6)
        self.assertNotIn("count", rows[0])

    def test_naming_a_check_lists_it(self):
        self.many_generic_names()
        report = self.run_tool(checks=["generic_body_name"])
        self.assertEqual(len(report["findings"]), 6)

    def test_the_baseline_matches_items_under_folded_rows(self):
        self.many_generic_names()
        first = self.run_tool(groups=["bodies"])
        self.design = fake_design(bodies=[fake_body(f"Body{i}") for i in range(2, 8)])
        changes = self.run_tool(groups=["bodies"], baseline_token=first["baseline_token"])["changes"]
        self.assertEqual((changes["new"], changes["resolved"], changes["unchanged"]), (1, 1, 5))
        self.assertEqual(wheres(changes["new_findings"]), ["body: root/Body7"])
        self.assertEqual(wheres(changes["resolved_findings"]), ["body: root/Body1"])

    def test_a_folded_row_leads_with_what_is_new(self):
        self.many_generic_names()
        first = self.run_tool(groups=["bodies"])
        self.design = fake_design(bodies=[fake_body(f"Body{i}") for i in range(2, 8)])
        report = self.run_tool(groups=["bodies"], baseline_token=first["baseline_token"])
        row = next(f for f in report["findings"] if f["check"] == "generic_body_name")
        self.assertEqual(row["change"], "new")
        self.assertEqual(row["change_counts"], {"new": 1, "unchanged": 5})
        self.assertEqual(row["sample"][0]["where"], "body: root/Body7")

    def test_detail_must_be_a_boolean(self):
        self.assertIn("detail", self.run_tool(detail="yes"))

    def test_first_issues_is_null_when_the_timeline_was_not_read(self):
        report = self.run_tool(groups=["bodies"])
        self.assertIsNone(report["first_issues"])
        self.assertIsNone(report["first_issues_total"])

    def test_first_issues_lists_the_timeline_before_the_findings(self):
        keys = list(self.run_tool())
        self.assertLess(keys.index("first_issues"), keys.index("findings"))

    def test_the_cap_does_not_cut_what_the_baseline_stores(self):
        first = self.run_tool(max_findings=1)
        second = self.run_tool(baseline_token=first["baseline_token"])
        self.assertEqual((second["changes"]["new"], second["changes"]["resolved"]), (0, 0))

    def test_a_new_version_of_the_same_file_compares(self):
        first = self.run_tool()
        self.document = fake_document("Chassis v55")
        self.assertIn("changes", self.run_tool(baseline_token=first["baseline_token"]))

    def test_another_file_is_refused(self):
        first = self.run_tool()
        self.document = fake_document("Other v1", "urn:other")
        text = self.run_tool(baseline_token=first["baseline_token"])
        self.assertIn("cannot be compared", text)

    def test_an_unknown_token_is_refused_before_touching_fusion(self):
        with mock.patch.object(ad, "get_app", side_effect=AssertionError("touched Fusion")):
            self.assertIn("unknown baseline_token", self.run_tool(baseline_token="nope"))

    def test_bad_arguments_never_touch_fusion(self):
        with mock.patch.object(ad, "get_app", side_effect=AssertionError("touched Fusion")):
            self.assertIn("profile", self.run_tool(profile="nope"))

    def test_not_a_design(self):
        self.design = None
        self.assertIn("not a Fusion design", self.run_tool())

    def test_extra_findings_merge_and_sort(self):
        extra = {"check": "custom_rule", "group": "custom", "severity": "error", "where": "custom: A", "message": "short"}
        report = self.run_tool(extra_findings=[extra], groups=["parameters"])
        self.assertEqual(report["findings"][0]["check"], "custom_rule")
        self.assertEqual(report["findings"][0]["source"], rules.EXTRA_SOURCE)
        self.assertEqual(report["summary"]["by_group"]["custom"]["error"], 1)
        self.assertFalse(report["ok"])

    def test_extra_findings_resolve_only_against_an_explicit_empty_list(self):
        extra = {"check": "custom_rule", "group": "custom", "severity": "error", "where": "custom: A", "message": "short"}
        first = self.run_tool(extra_findings=[extra])
        without = self.run_tool(baseline_token=first["baseline_token"])
        self.assertEqual(without["changes"]["resolved"], 0)
        emptied = self.run_tool(baseline_token=first["baseline_token"], extra_findings=[])
        self.assertEqual([f["where"] for f in emptied["changes"]["resolved_findings"]], ["custom: A"])

    def test_a_section_that_failed_is_reported_and_is_not_ok(self):
        with mock.patch.object(ad, "gather_bodies", side_effect=RuntimeError("boom")), \
                mock.patch.dict(ad.GATHERERS, {"bodies": ad.gather_bodies}):
            report = self.run_tool()
        self.assertFalse(report["complete"])
        self.assertFalse(report["ok"])
        sections = {row["section"]: row["status"] for row in report["skipped"] if "section" in row}
        self.assertEqual(sections, {"bodies": rules.FAILED, "body_faces": rules.SKIPPED})


if __name__ == "__main__":
    unittest.main()
