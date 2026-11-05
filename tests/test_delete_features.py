"""delete_features: schema, argument validation, and the planning/resolution
logic exercised against fake timelines (no Fusion needed)."""

import json
import types
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

import adsk.fusion

from fusion_bridge import delete_features as df
from fusion_bridge import tool_surface


# -- Fakes -------------------------------------------------------------------------


class Fake:
    """Duck-typed Fusion object: objectType plus arbitrary attributes."""

    def __init__(self, kind, **attrs):
        self.objectType = f"adsk::fusion::{kind}"
        for key, value in attrs.items():
            setattr(self, key, value)


class Coll:
    def __init__(self, items):
        self.objectType = "adsk::core::ObjectCollection"
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]


class FakeTimelineObject:
    def __init__(self, timeline, name, entity, health="healthy", deletable=True, group=False, children=()):
        self._timeline = timeline
        self.name = name
        self.entity = entity
        self.health = health
        self.isDeletable = deletable
        self.isGroup = group
        self._children = list(children)
        self.errorOrWarningMessage = ""

    @property
    def index(self):
        return self._timeline._items.index(self) if self in self._timeline._items else -1

    @property
    def healthState(self):
        return self.health

    @property
    def count(self):
        return len(self._children) if self.isGroup else None

    def item(self, i):
        return self._children[i]

    def deleteObject(self):
        self._timeline.deleted.append(self.name)
        self._timeline._items.remove(self)
        if self.entity is not None:
            self.entity.isValid = False
        return True


class FakeTimeline(Coll):
    def __init__(self):
        super().__init__([])
        self.deleted = []

    def add(self, name, entity, **kwargs):
        obj = FakeTimelineObject(self, name, entity, **kwargs)
        self._items.append(obj)
        return obj

    @property
    def markerPosition(self):
        return len(self._items)


class FakeDesign:
    def __init__(self, timeline, root, occurrences=()):
        self.timeline = timeline
        self.rootComponent = root
        root.allOccurrences = Coll(occurrences)
        self.designType = "parametric"

    def findEntityByToken(self, token):
        out = []
        for obj in self.timeline._items:
            if obj.entity is not None and obj.entity.entityToken == token:
                out.append(obj.entity)
        return out


def component(name, cid=None):
    return Fake("Component", name=name, id=cid or name, allOccurrences=Coll([]))


def sketch(name, comp, token=None):
    return Fake("Sketch", name=name, parentComponent=comp, entityToken=token or f"tok-{name}", isValid=True)


def body(name, comp):
    return Fake("BRepBody", name=name, parentComponent=comp, nativeObject=None)


def feature(kind, name, comp, bodies=(), token=None, **inputs):
    return Fake(kind, name=name, parentComponent=comp, bodies=Coll(bodies),
                entityToken=token or f"tok-{name}", isValid=True, **inputs)


HEALTH = types.SimpleNamespace(HealthyFeatureHealthState="healthy", WarningFeatureHealthState="warning",
                               ErrorFeatureHealthState="error")


def entry(position, name, produces=(), consumes=(), component="Root", deletable=True,
          referenced=False, collapsed=False, entity_type="ExtrudeFeature", health="healthy"):
    return {"position": position, "index": position, "name": name, "entity_type": entity_type,
            "component": component, "referenced": referenced, "in_collapsed_group": collapsed,
            "deletable": deletable, "health": health, "message": "", "token": f"tok-{name}",
            "produces": list(produces), "consumes": list(consumes), "inputs_unreadable": False}


def text(result):
    return json.loads(result["content"][0]["text"])


class RaisingEdgeSet(Fake):
    """Edge set inputs only read with the marker right before the feature."""

    def __init__(self):
        super().__init__("ConstantRadiusFilletEdgeSet")

    @property
    def edges(self):
        raise RuntimeError("3 : position the timeline marker before this feature")


class RaisingCut(Fake):
    def __init__(self, name, comp, bodies=(), **inputs):
        super().__init__("ExtrudeFeature", name=name, parentComponent=comp, bodies=Coll(bodies),
                         entityToken=f"tok-{name}", isValid=True, **inputs)

    @property
    def participantBodies(self):
        raise RuntimeError("3 : position the timeline marker before this feature")


# -- Schema and argument validation -------------------------------------------------


class SchemaTests(unittest.TestCase):
    def _schema(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "delete_features")["inputSchema"]

    def test_names_required_and_strict(self):
        schema = self._schema()
        self.assertEqual(schema["required"], ["names"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), {"names", "component", "include_dependents", "dry_run"})


class ValidationTests(unittest.TestCase):
    def test_defaults(self):
        params, err = df.validate_arguments({"names": ["Extrude1"]})
        self.assertIsNone(err)
        self.assertEqual(params, {"names": ["Extrude1"], "component": None,
                                  "include_dependents": False, "dry_run": True})

    def test_rejects_bad_arguments(self):
        cases = [
            ({}, "names"), ({"names": []}, "names"), ({"names": "Extrude1"}, "names"),
            ({"names": ["ok", ""]}, "names"), ({"names": ["ok", 3]}, "names"),
            ({"names": ["a", "a"]}, "duplicates"), ({"names": ["a"], "component": ""}, "component"),
            ({"names": ["a"], "dry_run": "false"}, "dry_run"),
            ({"names": ["a"], "include_dependents": 1}, "include_dependents"),
        ]
        for args, word in cases:
            with self.subTest(args=args):
                params, err = df.validate_arguments(args)
                self.assertIsNone(params)
                self.assertIn(word, err)

    def test_handler_rejects_without_touching_fusion(self):
        with mock.patch.object(df, "get_app", side_effect=AssertionError("Fusion touched")):
            result = df.delete_features({"names": []})
        self.assertTrue(result["isError"])


# -- Resolution -----------------------------------------------------------------------


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry(0, "Sketch1", component="Root", entity_type="Sketch"),
            entry(1, "Extrude1", component="Root"),
            entry(2, "Extrude1", component="Bracket"),
            entry(3, "Fillet1", component="Bracket"),
            entry(4, "Linked Extrude", component="Vendor", referenced=True),
        ]

    def test_unique_name(self):
        targets, problems = df.resolve_targets(self.entries, ["Fillet1"])
        self.assertEqual(problems, {})
        self.assertEqual([t["position"] for t in targets], [3])

    def test_ambiguous_across_components_lists_candidates(self):
        _, problems = df.resolve_targets(self.entries, ["Extrude1"])
        self.assertEqual(problems["Extrude1"]["reason"], "ambiguous")
        self.assertEqual({c["component"] for c in problems["Extrude1"]["candidates"]}, {"Root", "Bracket"})
        self.assertEqual(set(problems["Extrude1"]["candidates"][0]), {"index", "name", "entity_type", "component"})

    def test_component_scope_disambiguates(self):
        targets, problems = df.resolve_targets(self.entries, ["Extrude1"], component="Bracket")
        self.assertEqual(problems, {})
        self.assertEqual(targets[0]["position"], 2)

    def test_wrong_component_points_elsewhere(self):
        _, problems = df.resolve_targets(self.entries, ["Sketch1"], component="Bracket")
        self.assertIn("not in component", problems["Sketch1"]["reason"])
        self.assertEqual(problems["Sketch1"]["candidates"][0]["component"], "Root")

    def test_linked_items_never_resolve(self):
        _, problems = df.resolve_targets(self.entries, ["Linked Extrude"])
        self.assertIn("linked", problems["Linked Extrude"]["reason"])

    def test_missing_name_suggests_close_matches(self):
        _, problems = df.resolve_targets(self.entries, ["fillet1"])
        self.assertEqual(problems["fillet1"]["reason"], "not found")
        self.assertEqual([c["name"] for c in problems["fillet1"]["candidates"]], ["Fillet1"])

    def test_one_bad_name_reports_only_that_name(self):
        _, problems = df.resolve_targets(self.entries, ["Fillet1", "Nope"])
        self.assertEqual(list(problems), ["Nope"])

    def test_names_are_case_sensitive_and_exact(self):
        _, problems = df.resolve_targets(self.entries, ["Sketch"])
        self.assertIn("Sketch", problems)


# -- Dependents and planning ---------------------------------------------------------


class DependentTests(unittest.TestCase):
    def setUp(self):
        # Sketch1 -> Extrude1 (creates body:B) -> Fillet1 (modifies B) -> Sketch2 on B -> Extrude2 (cuts B)
        # Sketch3 is unrelated and sits after everything.
        self.entries = [
            entry(0, "Sketch1", produces=["sketch:1"], entity_type="Sketch"),
            entry(1, "Extrude1", produces=["body:B"], consumes=["sketch:1"]),
            entry(2, "Fillet1", produces=["body:B"], consumes=[]),
            entry(3, "Sketch2", produces=["sketch:2"], consumes=["body:B"], entity_type="Sketch"),
            entry(4, "Extrude2", produces=["body:B"], consumes=["sketch:2"]),
            entry(5, "Sketch3", produces=["sketch:3"], entity_type="Sketch"),
        ]

    def test_transitive_strong_dependents(self):
        strong, weak = df.find_dependents(self.entries, [0])
        self.assertEqual(sorted(strong), [1, 2, 3, 4])
        self.assertEqual(strong[1], [{"on": 0, "via": "sketch:1"}])
        # Fillet1 has no readable inputs: its output body alone ties it to Extrude1.
        self.assertEqual(strong[2], [{"on": 1, "via": "body:B"}])
        self.assertNotIn(5, strong)
        self.assertEqual(weak, {})

    def test_modifier_only_has_weak_dependents(self):
        strong, weak = df.find_dependents(self.entries, [2])
        self.assertEqual(strong, {})
        self.assertEqual(sorted(weak), [3, 4])

    def test_earlier_items_are_never_dependents(self):
        strong, weak = df.find_dependents(self.entries, [4])
        self.assertEqual((strong, weak), ({}, {}))

    def test_refuses_when_dependents_exist(self):
        targets = [self.entries[0]]
        plan, refusal = df.build_plan(self.entries, targets, include_dependents=False,
                                      labels={"sketch:1": "Sketch 'Sketch1'"})
        self.assertIsNotNone(refusal)
        self.assertIn("include_dependents", refusal["error"])
        self.assertEqual([d["name"] for d in refusal["dependents"]], ["Extrude1", "Fillet1", "Sketch2", "Extrude2"])
        self.assertEqual(refusal["dependents"][0]["consumes"], [{"on": "Sketch1", "via": "Sketch 'Sketch1'"}])

    def test_include_dependents_orders_latest_first(self):
        plan, refusal = df.build_plan(self.entries, [self.entries[0]], include_dependents=True)
        self.assertIsNone(refusal)
        self.assertEqual([(i["name"], i["role"]) for i in plan["delete_order"]],
                         [("Extrude2", "dependent"), ("Sketch2", "dependent"), ("Fillet1", "dependent"),
                          ("Extrude1", "dependent"), ("Sketch1", "requested")])

    def test_multiple_targets_latest_first(self):
        plan, refusal = df.build_plan(self.entries, [self.entries[5], self.entries[4]], include_dependents=False)
        self.assertIsNone(refusal)
        self.assertEqual([i["name"] for i in plan["delete_order"]], ["Sketch3", "Extrude2"])

    def test_target_that_is_also_a_dependent_is_listed_once(self):
        plan, refusal = df.build_plan(self.entries, [self.entries[0], self.entries[1]], include_dependents=True)
        self.assertIsNone(refusal)
        names = [i["name"] for i in plan["delete_order"]]
        self.assertEqual(names, ["Extrude2", "Sketch2", "Fillet1", "Extrude1", "Sketch1"])
        self.assertEqual(plan["delete_order"][3]["role"], "requested")

    def test_weak_dependents_reported_not_deleted(self):
        plan, refusal = df.build_plan(self.entries, [self.entries[2]], include_dependents=True)
        self.assertIsNone(refusal)
        self.assertEqual([i["name"] for i in plan["delete_order"]], ["Fillet1"])
        self.assertEqual([i["name"] for i in plan["possibly_affected"]], ["Sketch2", "Extrude2"])

    def test_undetected_dependents_refused_via_is_deletable(self):
        entries = [entry(0, "Sketch9", produces=["sketch:9"], deletable=False, entity_type="Sketch")]
        _, refusal = df.build_plan(entries, entries, include_dependents=True)
        self.assertIn("not deletable", refusal["error"])
        self.assertEqual(refusal["not_deletable"][0]["name"], "Sketch9")

    def test_not_deletable_because_of_planned_dependents_is_fine(self):
        entries = [dict(e) for e in self.entries]
        entries[0]["deletable"] = False  # Fusion says Sketch1 has dependents; they are in the plan
        _, refusal = df.build_plan(entries, [entries[0]], include_dependents=True)
        self.assertIsNone(refusal)

    def test_collapsed_group_items_refused(self):
        entries = [entry(0, "Sketch1", collapsed=True, entity_type="Sketch")]
        _, refusal = df.build_plan(entries, entries, include_dependents=False)
        self.assertIn("collapsed", refusal["error"])


class UnresolvedTests(unittest.TestCase):
    """Later features the plan cannot rule out are listed, never silently dropped."""

    def _plan(self, entries, target=0, include_dependents=True):
        plan, refusal = df.build_plan(entries, [entries[target]], include_dependents)
        self.assertIsNone(refusal)
        return plan

    def test_unreadable_inputs_listed_with_a_reason_and_kept(self):
        entries = [entry(0, "Sketch1", produces=["sketch:1"], entity_type="Sketch"),
                   dict(entry(1, "Cut1", produces=["body:Z"]), inputs_unreadable=True)]
        plan = self._plan(entries)
        self.assertEqual([i["name"] for i in plan["delete_order"]], ["Sketch1"])
        self.assertEqual(plan["possibly_affected"][0]["name"], "Cut1")
        self.assertIn("could not be read", plan["possibly_affected"][0]["reason"])

    def test_unreadable_before_the_target_is_irrelevant(self):
        entries = [dict(entry(0, "Cut1"), inputs_unreadable=True),
                   entry(1, "Sketch1", produces=["sketch:1"], entity_type="Sketch")]
        self.assertEqual(self._plan(entries, target=1)["possibly_affected"], [])

    def test_unreadable_dependent_is_a_dependent_not_possibly_affected(self):
        entries = [entry(0, "Box1", produces=["body:B"]),
                   dict(entry(1, "Cut1", produces=["body:B"]), inputs_unreadable=True)]
        plan = self._plan(entries)
        self.assertEqual([d["name"] for d in plan["dependents"]], ["Cut1"])
        self.assertEqual(plan["possibly_affected"], [])

    def test_body_with_no_traceable_creator(self):
        # Box1's bodies do not compare equal to the fillet's: the fillet looks like a creator.
        entries = [entry(0, "Box1", produces=["body:old"]),
                   dict(entry(1, "Fillet1", produces=["body:new"]), modifies_only=True),
                   dict(entry(2, "Cut1", produces=["body:new"]), modifies_only=True),
                   entry(3, "Other", produces=["body:other"], consumes=["sketch:x"])]
        plan = self._plan(entries)
        self.assertEqual([i["name"] for i in plan["possibly_affected"]], ["Fillet1", "Cut1"])
        self.assertIn("could not be traced", plan["possibly_affected"][1]["reason"])

    def test_orphan_older_than_the_target_is_not_its_problem(self):
        entries = [dict(entry(0, "Fillet1", produces=["body:new"]), modifies_only=True),
                   entry(1, "Sketch1", produces=["sketch:1"], entity_type="Sketch"),
                   dict(entry(2, "Cut1", produces=["body:new"]), modifies_only=True)]
        self.assertEqual(self._plan(entries, target=1)["possibly_affected"], [])

    def test_target_reporting_no_bodies_flags_later_features(self):
        entries = [dict(entry(0, "Box1"), is_feature=True, bodies_unknown=True),
                   dict(entry(1, "Fillet1", produces=["body:C"]), is_feature=True),
                   entry(2, "Sketch9", produces=["sketch:9"], entity_type="Sketch")]
        plan = self._plan(entries)
        self.assertEqual([i["name"] for i in plan["possibly_affected"]], ["Fillet1"])
        self.assertIn("Box1 reports no bodies", plan["possibly_affected"][0]["reason"])


class RemovedItemsTests(unittest.TestCase):
    def test_token_or_name_keeps_an_item(self):
        before = [entry(0, "A"), entry(1, "B"), entry(2, "C")]
        renamed = dict(entry(0, "A2"), token="tok-A")
        new_token = dict(entry(1, "B"), token="tok-other")
        self.assertEqual([e["name"] for e in df.removed_items(before, [renamed, new_token])], ["C"])

    def test_equal_names_are_counted(self):
        first, second = dict(entry(0, "Sk"), token=None), dict(entry(1, "Sk"), token=None)
        self.assertEqual(len(df.removed_items([first, second], [dict(entry(0, "Sk"), token=None)])), 1)

    def test_same_name_in_another_component_does_not_keep_it(self):
        gone = entry(0, "A")
        other = dict(entry(0, "A", component="Other"), token="tok-other")
        self.assertEqual(df.removed_items([gone], [other]), [gone])

    def test_empty_timelines(self):
        self.assertEqual(df.removed_items([], []), [])
        self.assertEqual(len(df.removed_items([entry(0, "A")], [])), 1)


class MatchForDeleteTests(unittest.TestCase):
    def test_resolves_after_indices_shift(self):
        planned = df.describe(entry(5, "Extrude2"))
        current = [entry(0, "Sketch1", entity_type="Sketch"), entry(1, "Extrude2")]
        found, err = df.match_for_delete(current, planned)
        self.assertIsNone(err)
        self.assertEqual(found["position"], 1)

    def test_missing_and_duplicated(self):
        planned = df.describe(entry(5, "Extrude2"))
        self.assertIn("no longer", df.match_for_delete([], planned)[1])
        dup = [entry(0, "Extrude2"), entry(1, "Extrude2")]
        self.assertIn("2 timeline items", df.match_for_delete(dup, planned)[1])

    def test_ignores_other_components_and_types(self):
        planned = df.describe(entry(5, "Extrude2"))
        current = [entry(0, "Extrude2", component="Other"), entry(1, "Extrude2", entity_type="Sketch")]
        self.assertIsNone(df.match_for_delete(current, planned)[0])


class HealthTests(unittest.TestCase):
    def test_counts_and_new_errors(self):
        before = [entry(0, "A", health="error")]
        after = [entry(0, "A", health="error"), entry(1, "B", health="error"),
                 entry(2, "C", health="warning"), entry(3, "D"), entry(4, "G", health="unknown")]
        counts, errors = df.health_summary(after)
        self.assertEqual(counts, {"healthy": 1, "warning": 1, "error": 2})
        _, errors_before = df.health_summary(before)
        self.assertEqual([e["name"] for e in df.new_errors(errors_before, errors)], ["B"])


# -- Reading references from fake Fusion objects ------------------------------------


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.root = component("Root")
        self.sk = sketch("Sketch1", self.root)
        self.b = body("Body1", self.root)

    def _refs(self, obj):
        keys = df.EntityKeys()
        out = set()
        df.collect_references(obj, keys, out)
        return {keys.labels[k] for k in out}

    def test_profile_and_curves_map_to_sketch(self):
        profile = Fake("Profile", parentSketch=self.sk)
        path = Fake("Path", count=1, item=lambda i: Fake("PathEntity", entity=Fake("SketchLine", parentSketch=self.sk)))
        self.assertEqual(self._refs(Coll([profile])), {"Sketch 'Sketch1'"})
        self.assertEqual(self._refs(path), {"Sketch 'Sketch1'"})

    def test_topology_maps_to_body_and_proxies_to_native(self):
        face = Fake("BRepFace", body=self.b)
        proxy = Fake("BRepBody", name="Body1", nativeObject=self.b)
        edge_set = Fake("ConstantRadiusFilletEdgeSet", edges=Coll([Fake("BRepEdge", body=self.b)]))
        keys = df.EntityKeys()
        out = set()
        for obj in (face, proxy, Coll([edge_set])):
            df.collect_references(obj, keys, out)
        self.assertEqual(len(out), 1)

    def test_extent_and_construction(self):
        plane = Fake("ConstructionPlane", name="Plane1")
        extent = Fake("ToEntityExtentDefinition", entity=plane)
        self.assertEqual(self._refs(extent), {"ConstructionPlane 'Plane1'"})

    def test_cycles_and_scalars_are_safe(self):
        loop = Fake("Weird")
        loop.entity = loop
        self.assertEqual(self._refs(loop), set())
        self.assertEqual(self._refs([None, 3, "x", True]), set())


class ReadInputsTests(unittest.TestCase):
    def setUp(self):
        self.root = component("Root")
        self.b = body("Body1", self.root)

    def test_top_level_and_nested_raises_are_recorded(self):
        edge_set = RaisingEdgeSet()
        fillet = feature("FilletFeature", "Fillet1", self.root, edgeSets=Coll([edge_set]))
        cut = RaisingCut("Cut1", self.root)
        self.assertEqual(df.read_inputs(fillet, df.EntityKeys()), (set(), {"edgeSets"}))
        self.assertEqual(df.read_inputs(cut, df.EntityKeys()), (set(), {"participantBodies"}))

    def test_outputs_come_from_bodies_and_faces(self):
        other = body("Body2", self.root)
        faces = Coll([Fake("BRepFace", body=other), Fake("BRepFace", body=other)])
        keys = df.EntityKeys()
        out = df.read_outputs(feature("CutFeature", "Cut1", self.root, bodies=[self.b], faces=faces), keys)
        self.assertEqual(sorted(keys.labels[k] for k in out), ["BRepBody 'Body1'", "BRepBody 'Body2'"])

    def test_face_reads_are_capped(self):
        reads = []
        faces = types.SimpleNamespace(count=10_000,
                                      item=lambda i: reads.append(i) or Fake("BRepFace", body=self.b))
        df.read_outputs(feature("CutFeature", "Cut1", self.root, faces=faces), df.EntityKeys())
        self.assertEqual(len(reads), df._MAX_FACES_READ)

    def test_inputs_unresolved(self):
        unresolved = df.inputs_unresolved
        self.assertFalse(unresolved("FilletFeature", {"edgeSets"}, set(), {"body"}), "lineage covers a fillet")
        self.assertTrue(unresolved("FilletFeature", {"edgeSets"}, set(), set()))
        self.assertFalse(unresolved("ExtrudeFeature", {"participantBodies"}, {"sketch"}, {"body"}))
        self.assertTrue(unresolved("ExtrudeFeature", {"profile"}, set(), {"body"}), "a profile has no stand-in")
        self.assertTrue(unresolved("ExtrudeFeature", {"participantBodies"}, {"sketch"}, set()), "no stand-in output")
        self.assertTrue(unresolved("ExtrudeFeature", set(), {"sketch"}, set(), modifies_only=True))
        self.assertFalse(unresolved("ExtrudeFeature", set(), {"sketch"}, set()))
        self.assertTrue(unresolved("ExtrudeFeature", set(), set(), {"body"}), "read nothing at all")
        self.assertFalse(unresolved("Sketch", set(), set(), {"sketch"}))
        self.assertTrue(unresolved("Sketch", {"referencePlane"}, set(), {"sketch"}))
        self.assertFalse(unresolved("Joint", set(), set(), set()))


# -- Snapshot and the full handler over a fake design ---------------------------------


class FakeDesignMixin:
    def build(self, linked=False):
        root = component("Root")
        tl = FakeTimeline()
        sk1 = sketch("Sketch1", root)
        b = body("Body1", root)
        ext = feature("ExtrudeFeature", "Extrude1", root, bodies=[b], profile=Fake("Profile", parentSketch=sk1))
        fil = feature("FilletFeature", "Fillet1", root, bodies=[b],
                      edgeSets=Coll([Fake("ConstantRadiusFilletEdgeSet", edges=Coll([Fake("BRepEdge", body=b)]))]))
        sk3 = sketch("Sketch3", root)
        tl.add("Sketch1", sk1)
        tl.add("Extrude1", ext)
        tl.add("Fillet1", fil)
        tl.add("Sketch3", sk3)
        occurrences = []
        if linked:
            vendor = component("Vendor", "vendor-id")
            occ = Fake("Occurrence", name="Vendor:1", component=vendor, sourceComponent=root,
                       isReferencedComponent=True, entityToken="tok-occ", isValid=True)
            occurrences.append(occ)
            tl.add("Vendor:1", occ)
        return FakeDesign(tl, root, occurrences)

    def call(self, design, args):
        app = types.SimpleNamespace(activeProduct=design, activeDocument=types.SimpleNamespace(name="Scratch"))
        fake_design_cls = types.SimpleNamespace(cast=lambda product: product)
        with mock.patch.object(df, "get_app", return_value=app), \
                mock.patch.object(adsk.fusion, "Design", fake_design_cls, create=True), \
                mock.patch.object(adsk.fusion, "FeatureHealthStates", HEALTH, create=True):
            return df.delete_features(args)


class SnapshotTests(FakeDesignMixin, unittest.TestCase):
    def test_links(self):
        design = self.build(linked=True)
        with mock.patch.object(adsk.fusion, "FeatureHealthStates", HEALTH, create=True):
            entries, handles, keys = df.snapshot(design)
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(by_name["Extrude1"]["consumes"], by_name["Sketch1"]["produces"])
        # The fillet's edges live on the body Extrude1 creates (and the fillet then modifies).
        self.assertEqual(by_name["Fillet1"]["consumes"], by_name["Extrude1"]["produces"])
        self.assertEqual(by_name["Fillet1"]["produces"], by_name["Extrude1"]["produces"])
        self.assertTrue(by_name["Vendor:1"]["referenced"])
        self.assertEqual(by_name["Extrude1"]["health"], "healthy")
        self.assertEqual(by_name["Extrude1"]["component"], "Root")
        self.assertEqual(len(handles), 5)

    def test_collapsed_group_children_are_walked(self):
        design = self.build()
        tl = design.timeline
        children = tl._items[2:4]
        del tl._items[2:4]
        tl._items.append(FakeTimelineObject(tl, "Group1", None, group=True, children=children))
        entries, _, _ = df.snapshot(design)
        self.assertEqual([(e["name"], e["in_collapsed_group"]) for e in entries],
                         [("Sketch1", False), ("Extrude1", False), ("Fillet1", True), ("Sketch3", True)])


class HandlerTests(FakeDesignMixin, unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        design = self.build()
        result = self.call(design, {"names": ["Sketch1"], "include_dependents": True})
        self.assertFalse(result["isError"])
        body = text(result)
        self.assertTrue(body["dry_run"])
        self.assertEqual([i["name"] for i in body["delete_order"]], ["Fillet1", "Extrude1", "Sketch1"])
        self.assertEqual(design.timeline.deleted, [])

    def test_refuses_dependents_without_flag(self):
        design = self.build()
        result = self.call(design, {"names": ["Sketch1"], "dry_run": False})
        self.assertTrue(result["isError"])
        self.assertEqual([d["name"] for d in text(result)["dependents"]], ["Extrude1", "Fillet1"])
        self.assertEqual(design.timeline.deleted, [])

    def test_ambiguous_refuses_whole_call(self):
        design = self.build()
        design.timeline.add("Sketch3", sketch("Sketch3", component("Bracket"), token="tok-other"))
        result = self.call(design, {"names": ["Fillet1", "Sketch3"], "dry_run": False})
        self.assertTrue(result["isError"])
        self.assertEqual(text(result)["unresolved"]["Sketch3"]["reason"], "ambiguous")
        self.assertEqual(design.timeline.deleted, [])

    def test_deletes_latest_first_and_reports_health(self):
        design = self.build()
        result = self.call(design, {"names": ["Sketch1"], "include_dependents": True, "dry_run": False})
        body = text(result)
        self.assertFalse(result["isError"], body)
        self.assertEqual(design.timeline.deleted, ["Fillet1", "Extrude1", "Sketch1"])
        self.assertTrue(all(i["verified_gone"] for i in body["deleted"]))
        self.assertEqual(body["timeline_health"], {"healthy": 1, "warning": 0, "error": 0})
        self.assertEqual(body["new_errors"], [])

    def test_stops_when_entity_token_no_longer_matches(self):
        design = self.build()
        original_delete = FakeTimelineObject.deleteObject

        def swap_then_delete(obj):
            # After the first delete, Extrude1 is replaced by a different entity of the same name.
            result = original_delete(obj)
            for item in design.timeline._items:
                if item.name == "Extrude1":
                    item.entity = feature("ExtrudeFeature", "Extrude1", design.rootComponent, token="tok-new")
            return result

        with mock.patch.object(FakeTimelineObject, "deleteObject", swap_then_delete):
            result = self.call(design, {"names": ["Sketch1"], "include_dependents": True, "dry_run": False})
        body = text(result)
        self.assertTrue(result["isError"])
        self.assertIn("entityToken", body["stopped"])
        self.assertEqual([i["name"] for i in body["deleted"]], ["Fillet1"])
        self.assertEqual([i["name"] for i in body["not_deleted"]], ["Extrude1", "Sketch1"])

    def test_new_errors_reported(self):
        design = self.build()
        original_delete = FakeTimelineObject.deleteObject

        def break_sketch3(obj):
            result = original_delete(obj)
            design.timeline._items[-1].health = "error"
            return result

        with mock.patch.object(FakeTimelineObject, "deleteObject", break_sketch3):
            result = self.call(design, {"names": ["Fillet1"], "dry_run": False})
        body = text(result)
        self.assertEqual(body["timeline_health"]["error"], 1)
        self.assertEqual([e["name"] for e in body["new_errors"]], ["Sketch3"])

    def test_rolled_back_marker_refused(self):
        design = self.build()
        type(design.timeline).markerPosition = property(lambda self: 1)
        try:
            result = self.call(design, {"names": ["Sketch3"]})
        finally:
            type(design.timeline).markerPosition = property(lambda self: len(self._items))
        self.assertTrue(result["isError"])
        self.assertIn("marker", result["content"][0]["text"])


class LiveCaseTests(FakeDesignMixin, unittest.TestCase):
    """Issue 16: sq -> Box1 (new body), sketches, Fillet1 and Cut1 on Box1's body.

    delete_features names=['sq'] include_dependents=true planned [Box1, sq]; the real
    delete took the timeline from 8 to 4, removing Fillet1 and Cut1 unreported.
    """

    def build_live(self, lineage=True):
        root = component("Root")
        tl = FakeTimeline()
        sq, cut_sketch = sketch("sq", root), sketch("CutProfile", root)
        b = body("Body1", root)
        box = feature("ExtrudeFeature", "Box1", root, bodies=[b], profile=Fake("Profile", parentSketch=sq))
        fillet = feature("FilletFeature", "Fillet1", root, bodies=[b] if lineage else [],
                         edgeSets=Coll([RaisingEdgeSet()]))
        cut = RaisingCut("Cut1", root, bodies=[b] if lineage else [], profile=Fake("Profile", parentSketch=cut_sketch))
        tl.add("sq", sq)
        box_item = tl.add("Box1", box)
        tl.add("Sketch2", sketch("Sketch2", root))
        tl.add("Sketch3", sketch("Sketch3", root))
        tl.add("Fillet1", fillet)
        tl.add("CutProfile", cut_sketch)
        tl.add("Cut1", cut)
        tl.add("Sketch4", sketch("Sketch4", root))
        self.assertEqual(tl.count, 8)
        self.box_item = box_item
        return FakeDesign(tl, root)

    def cascade(self, design):
        """Make Box1's delete take Fillet1 and Cut1 with it, as Fusion does."""
        original = FakeTimelineObject.deleteObject

        def delete(obj):
            if obj is self.box_item:
                for item in list(design.timeline._items):
                    if item.name in ("Fillet1", "Cut1"):
                        design.timeline._items.remove(item)
            return original(obj)

        return mock.patch.object(FakeTimelineObject, "deleteObject", delete)

    def test_dry_run_plans_everything_fusion_will_remove(self):
        design = self.build_live()
        result = self.call(design, {"names": ["sq"], "include_dependents": True})
        body = text(result)
        self.assertFalse(result["isError"], body)
        self.assertEqual([(i["name"], i["role"]) for i in body["delete_order"]],
                         [("Cut1", "dependent"), ("Fillet1", "dependent"), ("Box1", "dependent"), ("sq", "requested")])
        self.assertEqual([d["name"] for d in body["dependents"]], ["Box1", "Fillet1", "Cut1"])
        self.assertEqual(body["possibly_affected"], [])
        self.assertEqual(design.timeline.count, 8)

    def test_refused_without_include_dependents(self):
        design = self.build_live()
        result = self.call(design, {"names": ["Box1"], "dry_run": False})
        self.assertTrue(result["isError"])
        self.assertEqual([d["name"] for d in text(result)["dependents"]], ["Fillet1", "Cut1"])
        self.assertEqual(design.timeline.count, 8)

    def test_real_delete_matches_the_plan(self):
        design = self.build_live()
        with self.cascade(design):
            result = self.call(design, {"names": ["sq"], "include_dependents": True, "dry_run": False})
        body = text(result)
        self.assertFalse(result["isError"], body)
        self.assertEqual(design.timeline.deleted, ["Cut1", "Fillet1", "Box1", "sq"])
        self.assertEqual(body["also_removed"], [])
        self.assertEqual(body["timeline_items"], {"before": 8, "after": 4})

    def test_body_lineage_through_faces_alone(self):
        design = self.build_live(lineage=False)
        b = design.timeline._items[1].entity.bodies.item(0)
        for item in design.timeline._items:
            if item.name in ("Fillet1", "Cut1"):
                item.entity.faces = Coll([Fake("BRepFace", body=b)])
        body = text(self.call(design, {"names": ["sq"], "include_dependents": True}))
        self.assertEqual([i["name"] for i in body["delete_order"]], ["Cut1", "Fillet1", "Box1", "sq"])

    def test_no_lineage_lists_them_as_possibly_affected(self):
        design = self.build_live(lineage=False)
        body = text(self.call(design, {"names": ["sq"], "include_dependents": True}))
        self.assertEqual([i["name"] for i in body["delete_order"]], ["Box1", "sq"])
        self.assertEqual([i["name"] for i in body["possibly_affected"]], ["Fillet1", "Cut1"])
        self.assertIn("could not be read", body["possibly_affected"][0]["reason"])

    def test_unplanned_removals_are_reported_first(self):
        # The original failure: the plan does not see them, Fusion removes them anyway.
        design = self.build_live(lineage=False)
        with self.cascade(design):
            result = self.call(design, {"names": ["sq"], "include_dependents": True, "dry_run": False})
        body = text(result)
        self.assertTrue(result["isError"])
        self.assertEqual(list(body)[:2], ["warning", "also_removed"])
        self.assertIn("NOT IN THE PLAN", body["warning"])
        self.assertIn("'Fillet1'", body["warning"])
        self.assertIn("'Cut1'", body["warning"])
        self.assertEqual([i["name"] for i in body["also_removed"]], ["Fillet1", "Cut1"])
        self.assertEqual([i["name"] for i in body["deleted"]], ["Box1", "sq"])
        self.assertEqual(body["timeline_items"], {"before": 8, "after": 4})

    def test_a_raising_delete_still_diffs_the_timeline(self):
        design = self.build_live(lineage=False)
        original = FakeTimelineObject.deleteObject

        def delete(obj):
            if obj is self.box_item:
                design.timeline._items.remove(next(i for i in design.timeline._items if i.name == "Fillet1"))
                raise RuntimeError("compute failed")
            return original(obj)

        with mock.patch.object(FakeTimelineObject, "deleteObject", delete):
            result = self.call(design, {"names": ["sq"], "include_dependents": True, "dry_run": False})
        body = text(result)
        self.assertTrue(result["isError"])
        self.assertIn("compute failed", body["stopped"])
        self.assertEqual([i["name"] for i in body["also_removed"]], ["Fillet1"])
        self.assertEqual([i["name"] for i in body["not_deleted"]], ["Box1", "sq"])

    def test_cut_operation_marks_a_feature_modify_only(self):
        operations = types.SimpleNamespace(JoinFeatureOperation=0, CutFeatureOperation=1,
                                           IntersectFeatureOperation=2, NewBodyFeatureOperation=3)
        root = component("Root")
        with mock.patch.object(adsk.fusion, "FeatureOperations", operations, create=True):
            self.assertTrue(df._modifies_only(feature("ExtrudeFeature", "Cut1", root, operation=1), "ExtrudeFeature"))
            self.assertFalse(df._modifies_only(feature("ExtrudeFeature", "Box1", root, operation=3), "ExtrudeFeature"))


if __name__ == "__main__":
    unittest.main()
