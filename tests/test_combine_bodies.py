"""combine_bodies: schema, validation, finding bodies, the preview, and whole runs
against fake bodies made of unit cells (no Fusion needed)."""

import json
import unittest

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import combine_bodies as cb
from fusion_bridge import tool_surface
from fusion_bridge.common import bodies, geometry

PLATE = {"occurrence_path": "root", "body_name": "Plate"}
TUBE = {"occurrence_path": "root", "body_name": "Tube"}
ROD = {"occurrence_path": "root", "body_name": "Rod"}
BYSTANDER = "root/Other"


def args(**overrides):
    return {"target": PLATE, "tools": [TUBE], "operation": "cut", "keep_tools": True, "units": "cm", **overrides}


def options(**overrides):
    opts, err = cb.validate_args(args(**overrides))
    assert err is None, err
    return opts


# -- Fakes -----------------------------------------------------------------------


class P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class Box:
    def __init__(self, cells):
        self.minPoint = P(*(min(c[i] for c in cells) for i in range(3)))
        self.maxPoint = P(*(max(c[i] for c in cells) + 1 for i in range(3)))


def groups(cells):
    """Connected groups of unit cells."""
    left, found = set(cells), []
    while left:
        stack, group = [left.pop()], set()
        while stack:
            x, y, z = cell = stack.pop()
            group.add(cell)
            near = {(x + 1, y, z), (x - 1, y, z), (x, y + 1, z), (x, y - 1, z), (x, y, z + 1), (x, y, z - 1)} & left
            left -= near
            stack.extend(near)
        found.append(group)
    return found


class Lump:
    pass


class Members(list):
    count = property(len)


class Body:
    """A solid of unit cells. ``generation`` marks which edit its wrapper belongs to."""

    isSolid = True
    isVisible = True
    nativeObject = None
    assemblyContext = None
    isValid = True
    generation = 0

    def __init__(self, name, cells, token=None):
        self.name, self.cells, self.entityToken = name, set(cells), token or f"tok-{name}"
        self.faces = Members()

    volume = property(lambda self: float(len(self.cells)))
    boundingBox = property(lambda self: Box(self.cells) if self.cells else None)
    lumps = property(lambda self: Members(Lump() for _ in groups(self.cells)))


class Root:
    allOccurrences = ()

    def __init__(self, members):
        self.bRepBodies = members


class Design:
    designType = 1
    activeEditObject = None
    parentDocument = None
    timeline = type("T", (), {"count": 0, "markerPosition": 0, "__iter__": lambda self: iter(())})()

    def __init__(self, members):
        self.rootComponent = Root(members)


class Feature:
    healthState = 0
    errorOrWarningMessage = ""
    parentComponent = None
    timelineObject = None
    objectType = "adsk::fusion::CombineFeature"
    name = "Combine1"

    def __init__(self, session, made):
        self.session, self.bodies = session, made

    def deleteMe(self):
        self.session.undo()
        return True


class Session:
    """The collaborator ``run`` drives, over a list of cell bodies."""

    def __init__(self, members, stray=None, surprise=None, refuse=None, preview_breaks=False):
        self.members, self.design = members, Design(members)
        self.stray, self.surprise, self.refuse, self.preview_breaks = stray, surprise, refuse, preview_breaks
        self.tokens = {}
        self.added = []
        self.generation = 0

    def entries(self):
        return bodies.assign_keys(list(bodies.body_entries(self.design.rootComponent)))

    fresh_entries = entries

    def by_token(self, token):
        return self.tokens.get(token, [b for b in self.members if b.entityToken == token])

    def fresh(self, entry):
        return Body("copy", entry["_body"].cells)

    def combine(self, target, tool, kind):
        if self.preview_breaks:
            return False
        if kind == cb.UNION:
            target.cells |= tool.cells
        elif kind == cb.DIFFERENCE:
            target.cells -= tool.cells
        else:
            target.cells &= tool.cells
        return True

    def finish_edit(self):
        """A base feature's finishEdit: every wrapper handed out so far dies."""
        self.generation += 1
        replaced = []
        for old in self.members:
            old.isValid = False
            new = Body(old.name, old.cells, f"{old.entityToken}+")
            new.generation = self.generation
            replaced.append(new)
        self.members[:] = replaced

    def add(self, target, tools, opts):
        self.added.append(opts["operation"])
        if self.refuse:
            raise RuntimeError(self.refuse)
        for entry in [target] + tools:
            if entry["_body"].generation != self.generation:
                raise RuntimeError("combine failed: a reference from before finishEdit")
        self.saved = [(b, set(b.cells)) for b in self.members]
        goal, cutters = target["_body"], [t["_body"] for t in tools]
        for tool in cutters:
            if opts["operation"] == "join":
                goal.cells |= tool.cells
            elif opts["operation"] == "cut":
                goal.cells -= tool.cells
            else:
                goal.cells &= tool.cells
        if self.surprise:
            goal.cells |= self.surprise
        if not opts["keep_tools"]:
            self.members[:] = [b for b in self.members if b not in cutters]
        made = [goal]
        pieces = groups(goal.cells)
        if opts["operation"] == "cut" and len(pieces) > 1:
            goal.cells = max(pieces, key=len)
            for i, piece in enumerate(p for p in pieces if p is not max(pieces, key=len)):
                extra = Body(f"Plate piece {i + 1}", piece)
                self.members.append(extra)
                made.append(extra)
        if self.stray:
            next(b for b in self.members if b.name == self.stray).cells.pop()
        return Feature(self, made)

    def undo(self):
        self.members[:] = [b for b, _ in self.saved]
        for body, cells in self.saved:
            body.cells = cells


def bar(x_from, x_to, y=0):
    return {(x, y, 0) for x in range(x_from, x_to)}


def workshop(**kwargs):
    return Session([Body("Plate", bar(0, 6)), Body("Tube", bar(5, 8)), Body("Rod", bar(2, 3)),
                    Body("Other", bar(0, 4, y=9))], **kwargs)


def go(session, **overrides):
    return cb.run(session.design, None, options(**overrides), session)


# -- Schema and arguments --------------------------------------------------------


class SchemaTests(unittest.TestCase):
    def schema(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "combine_bodies")["inputSchema"]

    def test_keep_tools_is_required(self):
        self.assertEqual(self.schema()["required"], ["target", "tools", "operation", "keep_tools"])

    def test_strict(self):
        self.assertFalse(self.schema()["additionalProperties"])
        self.assertFalse(self.schema()["properties"]["target"]["additionalProperties"])
        self.assertIn("token", self.schema()["properties"]["target"]["properties"])


class ValidationTests(unittest.TestCase):
    def error(self, *drop, **overrides):
        values = args(**overrides)
        for key in drop:
            del values[key]
        return cb.validate_args(values)[1]

    def test_defaults(self):
        opts = options()
        self.assertFalse(opts["dry_run"])
        self.assertEqual(opts["on_collateral"], "undo")

    def test_keep_tools_missing_says_why(self):
        self.assertIn("silently consumes", self.error("keep_tools"))

    def test_keep_tools_must_be_boolean(self):
        self.assertIn("boolean", self.error(keep_tools="yes"))

    def test_each_required_key(self):
        for key in ("target", "tools", "operation"):
            with self.subTest(key=key):
                self.assertIn(f"'{key}' is required", self.error(key))

    def test_target_in_tools(self):
        self.assertIn("among the tools", self.error(tools=[TUBE, PLATE]))

    def test_tool_twice(self):
        self.assertIn("twice", self.error(tools=[TUBE, TUBE]))

    def test_the_same_name_at_two_indexes_is_two_bodies(self):
        self.assertIsNone(self.error(tools=[{**TUBE, "body_index": 0}, {**TUBE, "body_index": 1}]))

    def test_bad_values(self):
        for overrides in ({"tools": []}, {"tools": "Tube"}, {"operation": "subtract"}, {"on_collateral": "ask"},
                          {"units": "ft"}, {"dry_run": "no"}, {"nope": 1}, {"target": {**PLATE, "token": " "}},
                          {"target": "Plate"}, {"tools": [{"body_name": "Tube"}]}):
            with self.subTest(overrides=overrides):
                self.assertIsNotNone(self.error(**overrides))

    def test_blank_names_count_as_absent(self):
        opts = options(feature_name="", result_name="")
        self.assertIsNone(opts["feature_name"])
        self.assertIsNone(opts["result_name"])

    def test_not_an_object(self):
        self.assertIsNotNone(cb.validate_args([])[1])


# -- Finding bodies --------------------------------------------------------------


class PickBodyTests(unittest.TestCase):
    def setUp(self):
        self.session = workshop()
        self.entries = self.session.entries()

    def pick(self, **handle):
        full = {"path": "root", "name": "Plate", "index": None, "token": None, **handle}
        return cb.pick_body(self.entries, full, self.session.by_token)

    def test_by_name(self):
        entry, how, notes, problem = self.pick()
        self.assertEqual((entry["body"], how, notes, problem), ("Plate", "name", [], None))

    def test_a_live_token_wins(self):
        entry, how, notes, _ = self.pick(token="tok-Plate")
        self.assertEqual((entry["body"], how, notes), ("Plate", "token", []))

    def test_a_token_follows_a_rename(self):
        entry, how, notes, _ = self.pick(token="tok-Plate", name="Old name")
        self.assertEqual((entry["body"], how), ("Plate", "token"))
        self.assertIn("called 'Plate' now", notes[0])

    def test_an_invalid_token_result_falls_to_the_name(self):
        self.session.tokens["dead"] = [Body("Plate", bar(0, 1))]
        self.session.tokens["dead"][0].isValid = False
        entry, how, notes, _ = self.pick(token="dead")
        self.assertIs(entry["_body"], self.session.members[0])
        self.assertEqual(how, "name")
        self.assertIn("no longer names it", notes[0])

    def test_a_token_naming_nothing_falls_to_the_name(self):
        self.assertEqual(self.pick(token="gone")[1], "name")

    def test_a_token_on_another_occurrence_does_not_count(self):
        self.assertEqual(self.pick(token="tok-Plate", path="Sub:1", name="Plate")[3], "no body matches Sub:1/Plate")

    def test_a_native_token_result_matches_its_proxy(self):
        proxy, core = self.session.members[0], Body("Plate", bar(0, 6))
        proxy.nativeObject = core
        self.session.tokens["n"] = [core]
        entry, how, _, _ = self.pick(token="n")
        self.assertIs(entry["_body"], proxy)
        self.assertEqual(how, "token")

    def test_miss(self):
        self.assertIn("no body matches", self.pick(name="Nothing")[3])

    def test_repeated_name_needs_an_index(self):
        self.session.members.append(Body("Plate", bar(20, 21)))
        self.entries = self.session.entries()
        self.assertIn("ambiguous", self.pick()[3])
        self.assertEqual(self.pick(index=4)[0]["index"], 4)


class BodyRefusalTests(unittest.TestCase):
    def entry(self, name="A", path="root", solid=True):
        return {"body": name, "occurrence_path": path, "solid": solid}

    def test_clean(self):
        self.assertEqual(cb.body_refusals(self.entry(), [self.entry("B")]), [])

    def test_a_token_reaching_the_target(self):
        target = self.entry()
        self.assertIn("among the tools", cb.body_refusals(target, [target])[0])

    def test_two_refs_to_one_tool(self):
        tool = self.entry("B")
        self.assertIn("same body", cb.body_refusals(self.entry(), [tool, tool])[0])

    def test_surface(self):
        self.assertIn("not solid", cb.body_refusals(self.entry(), [self.entry("B", solid=False)])[0])

    def test_different_occurrences(self):
        self.assertIn("different occurrences", cb.body_refusals(self.entry(), [self.entry("B", "Sub:1")])[0])


# -- Preview ---------------------------------------------------------------------


class PreviewTests(unittest.TestCase):
    def shown(self, session, tools, operation):
        by_name = {e["body"]: e for e in session.entries()}
        return cb.preview(session, by_name["Plate"], [by_name[t] for t in tools], operation)

    def test_cut(self):
        shown = self.shown(workshop(), ["Tube", "Rod"], "cut")
        self.assertEqual((shown["volume"], shown["lumps"], shown["overlaps"]), (4.0, 2, [1.0, 1.0]))

    def test_join(self):
        self.assertEqual(self.shown(workshop(), ["Tube"], "join")["volume"], 8.0)

    def test_intersect(self):
        self.assertEqual(self.shown(workshop(), ["Tube"], "intersect")["volume"], 1.0)

    def test_boxes_that_miss_skip_the_boolean(self):
        session = workshop()
        session.combine = lambda target, tool, kind: kind != cb.INTERSECTION
        self.assertEqual(self.shown(session, ["Other"], "cut")["overlaps"], [0.0])

    def test_a_copy_is_never_reused(self):
        session = workshop()
        self.assertEqual(self.shown(session, ["Tube"], "cut")["volume"], 5.0)
        self.assertEqual(session.members[0].volume, 6.0)

    def test_a_failed_boolean_raises(self):
        with self.assertRaises(RuntimeError):
            self.shown(workshop(preview_breaks=True), ["Tube"], "cut")

    def test_refusal_only_for_cut_and_intersect(self):
        tool = {"body": "T", "occurrence_path": "root"}
        shown = {"volume": 4.0, "overlaps": [0.0]}
        self.assertEqual(cb.preview_refusals(shown, [tool], "join"), [])
        self.assertIn("do not overlap", cb.preview_refusals(shown, [tool], "cut")[0])
        self.assertIn("do not overlap", cb.preview_refusals(shown, [tool], "intersect")[0])

    def test_refusal_when_nothing_would_remain(self):
        tool = {"body": "T", "occurrence_path": "root"}
        self.assertIn("nothing", cb.preview_refusals({"volume": 0.0, "overlaps": [2.0]}, [tool], "cut")[0])

    def test_match_tolerance_scales_with_the_body(self):
        self.assertTrue(cb.preview_matches(8.0, 8.0 + 5e-7, 8.0))
        self.assertFalse(cb.preview_matches(8.0, 8.001, 8.0))
        self.assertTrue(cb.preview_matches(1e6, 1e6 + 0.5, 1e6))

    def test_disjoint_warning(self):
        self.assertIn("disjoint_join", cb.disjoint_warning("join", 1, 2))
        self.assertIsNone(cb.disjoint_warning("join", 1, 1))
        self.assertIsNone(cb.disjoint_warning("cut", 1, 2))
        self.assertIsNone(cb.disjoint_warning("join", None, 2))


# -- Whole runs ------------------------------------------------------------------


class RunTests(unittest.TestCase):
    def test_cut_keeping_tools(self):
        session = workshop()
        payload = go(session)
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tools_kept"], [TUBE])
        self.assertEqual(payload["stale_refs"], [])
        self.assertEqual(payload["bodies"]["consumed"], [])
        self.assertEqual(payload["result"]["preview_volume"], 5.0)
        self.assertTrue(payload["result"]["preview_matched"])
        self.assertEqual(session.members[1].volume, 3.0)
        json.dumps(payload)

    def test_join_consuming_tools(self):
        session = workshop()
        payload = go(session, operation="join", keep_tools=False)
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertEqual(payload["tools_kept"], [])
        self.assertEqual(payload["stale_refs"], [TUBE])
        self.assertEqual([r["ref"] for r in payload["bodies"]["consumed"]], [TUBE])
        self.assertEqual(payload["result"]["bodies"][0]["ref"], PLATE)
        self.assertEqual(payload["result"]["bodies"][0]["token"], "tok-Plate")
        self.assertEqual(payload["result"]["bodies"][0]["volume"], 8.0)

    def test_result_comes_from_the_feature_and_lists_every_piece(self):
        session = workshop()
        payload = go(session, tools=[ROD])
        rows = payload["result"]["bodies"]
        self.assertEqual([r["volume"] for r in rows], [3.0, 2.0])
        self.assertEqual([r["ref"]["body_name"] for r in rows], ["Plate", "Plate piece 1"])
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertTrue(payload["result"]["preview_matched"])

    def test_after_finish_edit_old_tokens_work_by_name(self):
        session = workshop()
        session.finish_edit()
        payload = go(session, target={**PLATE, "token": "tok-Plate"}, tools=[{**TUBE, "token": "tok-Tube"}])
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertEqual([r["resolved_by"] for r in payload["resolved"]], ["name", "name"])

    def test_a_held_wrapper_would_have_failed(self):
        session = workshop()
        old = session.entries()
        session.finish_edit()
        with self.assertRaisesRegex(RuntimeError, "before finishEdit"):
            session.add(old[0], old[1:2], options())

    def test_resolved_by_token(self):
        payload = go(workshop(), target={**PLATE, "token": "tok-Plate"})
        self.assertEqual([r["resolved_by"] for r in payload["resolved"]], ["token", "name"])
        self.assertEqual(payload["resolved"][0]["role"], "target")

    def test_preview_mismatch_is_suspect(self):
        payload = go(workshop(surprise=bar(30, 32)))
        self.assertEqual(payload["status"], "suspect")
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["result"]["preview_matched"])
        self.assertTrue(any("disagrees" in w for w in payload["warnings"]))

    def test_no_preview_still_builds_and_says_so(self):
        payload = go(workshop(preview_breaks=True))
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertIsNone(payload["result"]["preview_matched"])
        self.assertTrue(any("no preview" in w for w in payload["warnings"]))

    def test_cut_with_a_tool_that_misses_is_refused(self):
        session = workshop()
        payload = go(session, tools=[{"occurrence_path": "root", "body_name": "Other"}])
        self.assertEqual(payload["status"], "refused")
        self.assertIn("do not overlap", payload["refusals"][0])
        self.assertEqual(session.added, [])

    def test_one_tool_of_two_missing_is_refused(self):
        payload = go(workshop(), tools=[TUBE, {"occurrence_path": "root", "body_name": "Other"}], keep_tools=False)
        self.assertEqual(payload["status"], "refused")
        self.assertIn("root/Other", payload["refusals"][0])

    def test_disjoint_join_warns(self):
        payload = go(workshop(), operation="join", tools=[{"occurrence_path": "root", "body_name": "Other"}])
        self.assertTrue(any(w.startswith("disjoint_join") for w in payload["warnings"]))

    def test_collateral_undo(self):
        session = workshop(stray="Other")
        payload = go(session)
        self.assertEqual(payload["status"], "undone")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["collateral"], [BYSTANDER])
        self.assertEqual(session.members[0].volume, 6.0)

    def test_collateral_keep_is_suspect(self):
        session = workshop(stray="Other")
        payload = go(session, on_collateral="keep")
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(session.members[0].volume, 5.0)

    def test_failed_undo_stays_refused(self):
        session = workshop(stray="Other")
        session.undo = lambda: None
        payload = go(session)
        self.assertEqual(payload["status"], "refused")
        self.assertTrue(any("did not return" in w for w in payload["warnings"]))

    def test_fusion_refusal_is_kept_word_for_word(self):
        payload = go(workshop(refuse="3 : no intersection"))
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusals"], ["3 : no intersection"])

    def test_a_kept_tool_that_changed_is_suspect(self):
        session = workshop()
        add = session.add

        def eat_tool(target, tools, opts):
            feature = add(target, tools, opts)
            tools[0]["_body"].cells.pop()
            return feature

        session.add = eat_tool
        payload = go(session)
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(payload["tools_kept"], [])

    def test_a_consumed_tool_that_stayed_is_suspect(self):
        session = workshop()
        add = session.add
        session.add = lambda target, tools, opts: add(target, tools, {**opts, "keep_tools": True})
        payload = go(session, keep_tools=False)
        self.assertEqual(payload["status"], "suspect")
        self.assertTrue(any("remain" in w for w in payload["warnings"]))

    def test_a_tool_that_changed_visibility_is_reported(self):
        session = workshop()
        add = session.add

        def hide_tool(target, tools, opts):
            feature = add(target, tools, opts)
            tools[0]["_body"].isVisible = False
            return feature

        session.add = hide_tool
        payload = go(session)
        self.assertEqual(payload["status"], "built")
        self.assertTrue(any("visible=False" in w for w in payload["warnings"]))

    def test_target_unchanged_is_suspect(self):
        session = workshop()
        session.add = lambda target, tools, opts: Feature(session, [target["_body"]])
        payload = go(session, keep_tools=True)
        self.assertEqual(payload["status"], "suspect")
        self.assertTrue(any("did not change" in w for w in payload["warnings"]))

    def test_no_bodies_on_the_feature_is_suspect(self):
        session = workshop()
        add = session.add

        def empty(target, tools, opts):
            feature = add(target, tools, opts)
            feature.bodies = []
            return feature

        session.add = empty
        payload = go(session)
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(payload["result"]["bodies"], [])

    def test_names(self):
        session = workshop()
        payload = go(session, tools=[ROD], feature_name="Cope", result_name="Gusset")
        self.assertEqual(payload["feature"]["name"], "Cope")
        names = [r["ref"]["body_name"] for r in payload["result"]["bodies"]]
        self.assertEqual(names, ["Gusset", "Gusset 2"])
        self.assertIn(PLATE, payload["stale_refs"])
        self.assertEqual(payload["bodies"]["touched"][0]["ref"]["body_name"], "Gusset")

    def test_every_built_result_warns_about_entity_refs(self):
        self.assertIn(cb.STALE_ENTITY_REFS, go(workshop())["warnings"])

    def test_a_baseline_is_stored(self):
        token = go(workshop())["baseline_token"]
        self.assertIsNotNone(bodies.get_baseline(token))

    def test_hidden_body_warns(self):
        session = workshop()
        session.members[1].isVisible = False
        self.assertTrue(any("hidden" in w for w in go(session)["warnings"]))

    def test_refusals_are_listed_together(self):
        session = workshop()
        session.design.activeEditObject = type("E", (), {"objectType": "adsk::fusion::BaseFeature", "name": "Base1"})()
        payload = go(session, tools=[{"occurrence_path": "root", "body_name": "Nothing"}])
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(len(payload["refusals"]), 2)
        self.assertEqual(session.added, [])

    def test_surface_body_refused(self):
        session = workshop()
        session.members[1].isSolid = False
        self.assertIn("not solid", go(session)["refusals"][0])


class DryRunTests(unittest.TestCase):
    def test_plan_adds_nothing(self):
        session = workshop()
        payload = go(session, dry_run=True, keep_tools=False)
        self.assertEqual(payload["status"], "planned")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(session.added, [])
        plan = payload["plan"]
        self.assertEqual(plan["preview_volume"], 5.0)
        self.assertEqual(plan["lumps"], 1)
        self.assertEqual(plan["overlaps"], [{"ref": TUBE, "volume": 1.0}])
        self.assertEqual(plan["would_consume"], [TUBE])
        self.assertIsNone(payload["baseline_token"])

    def test_refusal_applies(self):
        payload = go(workshop(), dry_run=True, tools=[{"occurrence_path": "root", "body_name": "Other"}])
        self.assertEqual(payload["status"], "refused")
        self.assertIn("plan", payload)

    def test_disjoint_join_warns_before_building(self):
        payload = go(workshop(), dry_run=True, operation="join",
                     tools=[{"occurrence_path": "root", "body_name": "Other"}])
        self.assertTrue(any(w.startswith("disjoint_join") for w in payload["warnings"]))

    def test_no_preview_is_still_a_plan(self):
        payload = go(workshop(preview_breaks=True), dry_run=True)
        self.assertEqual(payload["status"], "planned")
        self.assertIsNone(payload["plan"]["preview_volume"])


class UnitTests(unittest.TestCase):
    def test_inches_out(self):
        payload = go(workshop(), units="in")
        self.assertEqual(payload["units"]["volume"], "in^3")
        self.assertEqual(payload["result"]["preview_volume"], geometry.to_units(5.0, "in", 3, 6))


class FeatureInputTests(unittest.TestCase):
    def test_root_takes_the_body_and_a_sub_component_its_native(self):
        core = Body("Plate", bar(0, 1))
        proxy = Body("Plate", bar(0, 1))
        proxy.nativeObject = core
        self.assertIs(cb._feature_input({"occurrence_path": "root", "_body": core}), core)
        self.assertIs(cb._feature_input({"occurrence_path": "Sub:1", "_body": proxy}), core)


class HandlerTests(unittest.TestCase):
    def test_validation_error_touches_nothing(self):
        result = cb.combine_bodies({"target": PLATE})
        self.assertTrue(result["isError"])
        self.assertIn("'tools' is required", result["content"][0]["text"])


class KeptToolResultTests(unittest.TestCase):
    """Fusion lists a kept tool among the feature's bodies; a sum of them read as a wrong result (seen live)."""

    class _Body:
        def __init__(self, name):
            self.name = name

    def test_kept_tool_is_not_a_result(self):
        plate, tube = self._Body("Plate"), self._Body("Tube")
        self.assertEqual(cb._without_kept_tools([plate, tube], [{"body": "Tube", "_body": tube}], True), [plate])

    def test_a_result_sharing_a_tools_name_stays(self):
        result, tool = self._Body("Tube"), self._Body("Tube")
        self.assertEqual(cb._without_kept_tools([result, tool], [{"body": "Tube", "_body": tool}], True), [result])

    def test_nothing_is_dropped_when_tools_are_consumed(self):
        plate = self._Body("Plate")
        self.assertEqual(cb._without_kept_tools([plate], [{"body": "Tube", "_body": self._Body("Tube")}], False), [plate])


if __name__ == "__main__":
    unittest.main()
