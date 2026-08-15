import copy
import json
from pathlib import Path
import unittest
from urllib.parse import urlparse

import requests

from camera_agent.provisioning import (
    MainfluxProvisioner,
    ProvisioningError,
    build_profile,
    build_pentest_rules,
    build_rules,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code=200, payload=None, reason="OK"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.reason = reason
        self.ok = 200 <= status_code < 400

    def json(self):
        return copy.deepcopy(self._payload)

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(
                f"{self.status_code} {self.reason}", response=self
            )


class FakeMainfluxSession:
    """Small stateful v0.41.1 API fake used to test reconciliation."""

    def __init__(
        self,
        *,
        group_id="group-1",
        page_cap=200,
        normalize_action_ids=False,
        inject_legacy_overlap_after_rule_create=False,
    ):
        self.group_id = group_id
        self.page_cap = page_cap
        self.normalize_action_ids = normalize_action_ids
        self.inject_legacy_overlap_after_rule_create = (
            inject_legacy_overlap_after_rule_create
        )
        self.profiles = []
        self.things = []
        self.rules = []
        self.assignments = {}
        self.calls = []
        self._next = 1

    def _id(self, prefix):
        value = f"{prefix}-{self._next}"
        self._next += 1
        return value

    @property
    def mutations(self):
        safe_posts = {"/tokens", "/identify"}
        return [
            call
            for call in self.calls
            if call[0] in {"PUT", "PATCH", "DELETE"}
            or (call[0] == "POST" and call[1] not in safe_posts)
        ]

    def clear_calls(self):
        self.calls.clear()

    def request(self, method, url, **kwargs):
        method = method.upper()
        path = urlparse(url).path
        payload = copy.deepcopy(kwargs.get("json"))
        params = copy.deepcopy(kwargs.get("params") or {})
        self.calls.append((method, path, payload, params))

        if method == "POST" and path == "/tokens":
            return FakeResponse(payload={"token": "user-token"})
        if method == "POST" and path == "/identify":
            key = str((payload or {}).get("key") or "")
            match = next((item for item in self.things if item["key"] == key), None)
            if match is None:
                return FakeResponse(404, {"error": "not found"}, "Not Found")
            return FakeResponse(payload={"id": match["id"]})

        if method == "GET" and path == f"/groups/{self.group_id}/profiles":
            return self._page("profiles", self.profiles, params)
        if method == "POST" and path == f"/groups/{self.group_id}/profiles":
            created = []
            for spec in payload:
                item = copy.deepcopy(spec)
                item.update({"id": self._id("profile"), "group_id": self.group_id})
                self.profiles.append(item)
                created.append(item)
            return FakeResponse(201, {"profiles": created}, "Created")
        if method == "PUT" and path.startswith("/profiles/"):
            profile_id = path.rsplit("/", 1)[1]
            item = self._find(self.profiles, profile_id)
            item.update(copy.deepcopy(payload))
            return FakeResponse(204)

        if method == "GET" and path == f"/groups/{self.group_id}/things":
            items = [item for item in self.things if item["group_id"] == self.group_id]
            return self._page("things", items, params)
        if method == "GET" and path == "/things":
            return self._page("things", self.things, params)
        if path.startswith("/things/"):
            thing_id = path.split("/")[2]
            item = self._find(self.things, thing_id, required=False)
            if item is None:
                return FakeResponse(404, {"error": "not found"}, "Not Found")
            if method == "GET":
                return FakeResponse(payload=item)
            if method == "PUT":
                item.update(copy.deepcopy(payload))
                return FakeResponse(204)
            if method == "PATCH":
                item.update(copy.deepcopy(payload))
                return FakeResponse(204)
        if method == "POST" and path.startswith("/profiles/") and path.endswith("/things"):
            profile_id = path.split("/")[2]
            profile = self._find(self.profiles, profile_id)
            created = []
            for spec in payload:
                item = copy.deepcopy(spec)
                item.update(
                    {
                        "id": self._id("thing"),
                        "key": item.get("key") or self._id("key"),
                        "group_id": profile["group_id"],
                        "profile_id": profile_id,
                    }
                )
                self.things.append(item)
                created.append(item)
            return FakeResponse(201, {"things": created}, "Created")

        rules_path = f"/svcrules/groups/{self.group_id}/rules"
        if method == "GET" and path == rules_path:
            items = []
            for stored in self.rules:
                if stored["group_id"] != self.group_id:
                    continue
                item = copy.deepcopy(stored)
                item["input"]["thing_ids"] = sorted(
                    self.assignments.get(item["id"], set())
                )
                self._normalize_rule_response(item)
                items.append(item)
            return self._page("rules", items, params)
        if method == "POST" and path == rules_path:
            created = []
            created_thing_ids = set()
            for spec in payload["rules"]:
                item = copy.deepcopy(spec)
                rule_id = self._id("rule")
                thing_ids = set(item.get("input", {}).pop("thing_ids", []))
                created_thing_ids.update(thing_ids)
                item.update({"id": rule_id, "group_id": self.group_id})
                self.rules.append(item)
                self.assignments[rule_id] = thing_ids
                response_item = copy.deepcopy(item)
                response_item["input"]["thing_ids"] = sorted(thing_ids)
                self._normalize_rule_response(response_item)
                created.append(response_item)
            if self.inject_legacy_overlap_after_rule_create and created_thing_ids:
                self.rules.append(
                    {
                        "id": "late-legacy-rule",
                        "group_id": self.group_id,
                        "name": "Late legacy shared rule",
                        "description": "Injected after per-device rule creation",
                        "input": {"type": "message"},
                        "conditions": [
                            {"field": "x", "comparator": "==", "threshold": 1}
                        ],
                        "actions": [{"type": "alarm", "level": 4}],
                    }
                )
                self.assignments["late-legacy-rule"] = set(created_thing_ids)
                self.inject_legacy_overlap_after_rule_create = False
            return FakeResponse(201, {"rules": created}, "Created")

        if path.startswith("/svcrules/rules/"):
            parts = path.strip("/").split("/")
            rule_id = parts[2]
            rule = self._find(self.rules, rule_id, required=False)
            if rule is None:
                return FakeResponse(404, {"error": "not found"}, "Not Found")
            if len(parts) == 3 and method == "GET":
                item = copy.deepcopy(rule)
                item["input"]["thing_ids"] = sorted(
                    self.assignments.get(rule_id, set())
                )
                self._normalize_rule_response(item)
                return FakeResponse(payload=item)
            if len(parts) == 3 and method == "PUT":
                rule.update(copy.deepcopy(payload))
                return FakeResponse(204)
            if len(parts) == 4 and parts[3] == "things" and method == "GET":
                return FakeResponse(
                    payload={"thing_ids": sorted(self.assignments.get(rule_id, set()))}
                )
            if len(parts) == 4 and parts[3] == "things" and method == "POST":
                self.assignments.setdefault(rule_id, set()).update(payload["thing_ids"])
                return FakeResponse(204)

        return FakeResponse(404, {"error": f"unhandled {method} {path}"}, "Not Found")

    def _normalize_rule_response(self, rule):
        if not self.normalize_action_ids:
            return
        for action in rule.get("actions", []):
            action.setdefault("id", "")

    def _page(self, key, items, params):
        offset = int(params.get("offset", 0))
        requested = int(params.get("limit", 200))
        limit = min(requested, self.page_cap)
        page = copy.deepcopy(items[offset : offset + limit])
        return FakeResponse(
            payload={
                key: page,
                "total": len(items),
                "offset": offset,
                "limit": limit,
            }
        )

    @staticmethod
    def _find(items, item_id, *, required=True):
        match = next((item for item in items if item.get("id") == item_id), None)
        if required and match is None:
            raise AssertionError(f"missing fake resource {item_id}")
        return match


def provisioner(session, *, group_id="group-1"):
    return MainfluxProvisioner(
        base_url="http://mainflux",
        group_id=group_id,
        session=session,
        verify_tls=True,
        token="user-token",
    )


class ProvisioningTests(unittest.TestCase):
    def test_pentest_rules_use_security_events_not_camera_events(self):
        rules = build_pentest_rules("security-agent-01", "thing-1")
        self.assertEqual(len(rules), 2)
        fields = {rule["conditions"][0]["field"] for rule in rules}
        self.assertEqual(fields, {"pentest_finding_event", "pentest_scan_error_event"})
        self.assertTrue(all("Facebook" not in rule["name"] for rule in rules))

    def test_templates_render_per_device_names_and_exact_assignment(self):
        profile = build_profile()
        rules = build_rules("warehouse-01", "thing-1")

        self.assertEqual(profile["name"], "Camera Agent - SenML")
        self.assertEqual(
            [rule["name"] for rule in rules],
            [
                "Camera Agent - warehouse-01 - Feature violation",
                "Camera Agent - warehouse-01 - Camera offline",
            ],
        )
        self.assertTrue(
            all(rule["input"]["thing_ids"] == ["thing-1"] for rule in rules)
        )

        profile_template = json.loads(
            (PROJECT_DIR / "mainflux" / "profile.template.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(profile_template, [profile])

    def test_first_run_creates_thing_profile_and_two_isolated_rules(self):
        session = FakeMainfluxSession()
        result = provisioner(session).provision_device(
            "warehouse-01", "Warehouse camera"
        )

        self.assertEqual(result.status, "created")
        self.assertIsNotNone(result.thing_id)
        self.assertIsNotNone(result.thing_key)
        self.assertIsNotNone(result.profile_id)
        self.assertEqual(set(result.rule_ids), {"feature_violation", "camera_offline"})
        self.assertEqual(len(session.profiles), 1)
        self.assertEqual(len(session.things), 1)
        self.assertEqual(len(session.rules), 2)
        thing = session.things[0]
        self.assertEqual(thing["metadata"]["managed_by"], "camera-agent")
        self.assertEqual(thing["metadata"]["device_id"], "warehouse-01")
        for rule in session.rules:
            self.assertEqual(session.assignments[rule["id"]], {result.thing_id})

    def test_new_thing_requires_a_safe_key_sink_before_any_mutation(self):
        session = FakeMainfluxSession()

        with self.assertRaisesRegex(
            ProvisioningError,
            r"tools\.add_device --provision-mainflux",
        ):
            provisioner(session).provision_device(
                "warehouse-01",
                "Warehouse camera",
                allow_create=False,
            )

        self.assertEqual(session.mutations, [])
        self.assertEqual(session.profiles, [])
        self.assertEqual(session.things, [])
        self.assertEqual(session.rules, [])

    def test_no_create_mode_reconciles_existing_deterministic_or_explicit_thing(self):
        session = FakeMainfluxSession()
        created = provisioner(session).provision_device(
            "warehouse-01", "Warehouse camera"
        )

        session.clear_calls()
        deterministic = provisioner(session).provision_device(
            "warehouse-01",
            "Warehouse camera",
            allow_create=False,
        )
        self.assertEqual(deterministic.status, "unchanged")
        self.assertEqual(deterministic.thing_id, created.thing_id)
        self.assertEqual(session.mutations, [])

        session.clear_calls()
        explicit = provisioner(session).provision_device(
            "warehouse-01",
            "Warehouse camera",
            thing_id=created.thing_id,
            allow_create=False,
        )
        self.assertEqual(explicit.status, "unchanged")
        self.assertEqual(explicit.thing_id, created.thing_id)
        self.assertEqual(session.mutations, [])

    def test_second_run_is_idempotent_and_has_no_mutations(self):
        session = FakeMainfluxSession()
        first = provisioner(session).provision_device("warehouse-01", "Warehouse")
        session.clear_calls()

        second = provisioner(session).provision_device("warehouse-01", "Warehouse")

        self.assertEqual(second.status, "unchanged")
        self.assertEqual(second.thing_id, first.thing_id)
        self.assertEqual(second.rule_ids, first.rule_ids)
        self.assertEqual(session.mutations, [])

    def test_existing_thing_can_be_found_by_secret_key_without_leaking_repr(self):
        session = FakeMainfluxSession()
        first = provisioner(session).provision_device("warehouse-01", "Warehouse")
        secret = first.thing_key
        session.clear_calls()

        second = provisioner(session).provision_device(
            "warehouse-01", "Warehouse", thing_key=secret
        )

        self.assertEqual(second.thing_id, first.thing_id)
        self.assertNotIn(str(secret), repr(second))
        self.assertEqual(session.mutations, [])

    def test_managed_rule_drift_is_updated_without_changing_assignment(self):
        session = FakeMainfluxSession()
        first = provisioner(session).provision_device("warehouse-01", "Warehouse")
        rule_id = first.rule_ids["feature_violation"]
        session.rules[0]["conditions"][0]["threshold"] = 9
        before = copy.deepcopy(session.assignments)
        session.clear_calls()

        second = provisioner(session).provision_device("warehouse-01", "Warehouse")

        self.assertEqual(second.status, "updated")
        self.assertEqual(session.assignments, before)
        puts = [call for call in session.mutations if call[:2] == ("PUT", f"/svcrules/rules/{rule_id}")]
        self.assertEqual(len(puts), 1)

    def test_legacy_managed_facebook_rule_is_renamed_in_place(self):
        session = FakeMainfluxSession()
        first = provisioner(session).provision_device("warehouse-01", "Warehouse")
        rule_id = first.rule_ids["feature_violation"]
        rule = next(item for item in session.rules if item["id"] == rule_id)
        rule["name"] = "Camera Agent - warehouse-01 - Facebook violation"
        session.clear_calls()

        result = provisioner(session).provision_device("warehouse-01", "Warehouse")

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.rule_ids["feature_violation"], rule_id)
        self.assertEqual(session.assignments[rule_id], {first.thing_id})
        self.assertEqual(rule["name"], "Camera Agent - warehouse-01 - Feature violation")
        self.assertEqual(
            [call[:2] for call in session.mutations],
            [("PUT", f"/svcrules/rules/{rule_id}")],
        )

    def test_empty_action_id_added_by_mainflux_is_semantically_ignored(self):
        session = FakeMainfluxSession(normalize_action_ids=True)

        first = provisioner(session).provision_device("warehouse-01", "Warehouse")
        session.clear_calls()
        second = provisioner(session).provision_device("warehouse-01", "Warehouse")

        self.assertEqual(first.status, "created")
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(session.mutations, [])

    def test_missing_target_assignment_is_repaired_without_rule_recreation(self):
        session = FakeMainfluxSession()
        first = provisioner(session).provision_device("warehouse-01", "Warehouse")
        rule_id = first.rule_ids["camera_offline"]
        session.assignments[rule_id].clear()
        session.clear_calls()

        second = provisioner(session).provision_device("warehouse-01", "Warehouse")

        self.assertEqual(second.status, "updated")
        self.assertEqual(session.assignments[rule_id], {first.thing_id})
        creates = [
            call for call in session.mutations
            if call[0] == "POST" and call[1].endswith("/rules")
        ]
        self.assertEqual(creates, [])

    def test_adding_third_device_does_not_touch_existing_device_rules(self):
        session = FakeMainfluxSession()
        first = provisioner(session).provision_device("camera-01", "Camera 1")
        second = provisioner(session).provision_device("camera-02", "Camera 2")
        before = copy.deepcopy(session.assignments)
        session.clear_calls()

        third = provisioner(session).provision_device("camera-03", "Camera 3")

        self.assertEqual(len(session.rules), 6)
        for rule_id in set(first.rule_ids.values()) | set(second.rule_ids.values()):
            self.assertEqual(session.assignments[rule_id], before[rule_id])
        self.assertTrue(
            all(
                session.assignments[rule_id] == {third.thing_id}
                for rule_id in third.rule_ids.values()
            )
        )

    def test_dry_run_does_not_mutate_files_or_mainflux(self):
        session = FakeMainfluxSession()
        result = provisioner(session).provision_device(
            "warehouse-01", "Warehouse", dry_run=True
        )

        self.assertEqual(result.status, "dry-run")
        self.assertEqual(session.mutations, [])
        self.assertEqual(session.profiles, [])
        self.assertEqual(session.things, [])
        self.assertEqual(session.rules, [])

    def test_foreign_group_thing_fails_before_mutation(self):
        session = FakeMainfluxSession()
        session.things.append(
            {
                "id": "foreign-thing",
                "key": "secret",
                "name": "camera-agent-warehouse-01",
                "type": "device",
                "group_id": "other-group",
                "profile_id": "other-profile",
                "metadata": {"managed_by": "camera-agent", "device_id": "warehouse-01"},
            }
        )

        with self.assertRaisesRegex(ProvisioningError, "different Mainflux group"):
            provisioner(session).provision_device(
                "warehouse-01", "Warehouse", thing_id="foreign-thing"
            )
        self.assertEqual(session.mutations, [])

    def test_duplicate_or_unmanaged_rule_fails_closed(self):
        session = FakeMainfluxSession()
        result = provisioner(session).provision_device("warehouse-01", "Warehouse")
        duplicate = copy.deepcopy(session.rules[0])
        duplicate["id"] = "duplicate-rule"
        session.rules.append(duplicate)
        session.assignments[duplicate["id"]] = {result.thing_id}
        session.clear_calls()

        with self.assertRaisesRegex(ProvisioningError, "duplicate Mainflux rules"):
            provisioner(session).provision_device("warehouse-01", "Warehouse")
        self.assertEqual(session.mutations, [])

        session.rules.pop()
        session.assignments.pop("duplicate-rule")
        session.rules[0]["description"] = "Created manually"
        session.clear_calls()
        with self.assertRaisesRegex(ProvisioningError, "unmanaged"):
            provisioner(session).provision_device("warehouse-01", "Warehouse")
        self.assertEqual(session.mutations, [])

    def test_extra_rule_assignment_fails_without_unassigning(self):
        session = FakeMainfluxSession()
        result = provisioner(session).provision_device("warehouse-01", "Warehouse")
        rule_id = result.rule_ids["feature_violation"]
        session.assignments[rule_id].add("another-thing")
        session.clear_calls()

        with self.assertRaisesRegex(ProvisioningError, "extra Thing assignment"):
            provisioner(session).provision_device("warehouse-01", "Warehouse")
        self.assertEqual(session.assignments[rule_id], {result.thing_id, "another-thing"})
        self.assertEqual(session.mutations, [])

    def test_legacy_rule_overlapping_target_fails_before_per_device_rule_creation(self):
        session = FakeMainfluxSession()
        first = provisioner(session).provision_device("warehouse-01", "Warehouse")
        for rule_id in first.rule_ids.values():
            session.assignments.pop(rule_id)
        session.rules = [
            rule for rule in session.rules if rule["id"] not in first.rule_ids.values()
        ]
        session.rules.append(
            {
                "id": "legacy-facebook-rule",
                "group_id": session.group_id,
                "name": "Camera Agent - Facebook violation",
                "description": "Legacy shared rule",
                "input": {"type": "message"},
                "conditions": [
                    {
                        "field": "rule_violation_event",
                        "comparator": "==",
                        "threshold": 1,
                    }
                ],
                "actions": [{"type": "alarm", "level": 4}],
            }
        )
        session.assignments["legacy-facebook-rule"] = {first.thing_id}
        session.clear_calls()

        with self.assertRaisesRegex(
            ProvisioningError, "legacy or foreign.*explicitly migrate"
        ):
            provisioner(session).provision_device("warehouse-01", "Warehouse")

        self.assertEqual(
            session.assignments["legacy-facebook-rule"], {first.thing_id}
        )
        self.assertEqual(session.mutations, [])

    def test_final_verification_detects_late_foreign_rule_overlap(self):
        session = FakeMainfluxSession(
            inject_legacy_overlap_after_rule_create=True
        )

        with self.assertRaisesRegex(
            ProvisioningError, "legacy or foreign.*explicitly migrate"
        ):
            provisioner(session).provision_device("warehouse-01", "Warehouse")

        self.assertTrue(session.assignments["late-legacy-rule"])
        self.assertFalse(
            any(
                method == "PATCH" and path == "/svcrules/rules/late-legacy-rule/things"
                for method, path, _, _ in session.calls
            )
        )

    def test_profile_reconciliation_preserves_unrelated_metadata_and_config(self):
        session = FakeMainfluxSession()
        provisioner(session).provision_device("warehouse-01", "Warehouse")
        profile = session.profiles[0]
        profile["metadata"]["operator_note"] = "retain-this"
        profile["metadata"].pop("provisioning_version")
        profile["config"]["transformer"]["supported_extension"] = "retain-this"
        session.clear_calls()

        result = provisioner(session).provision_device("warehouse-01", "Warehouse")

        self.assertEqual(result.status, "updated")
        self.assertEqual(profile["metadata"]["operator_note"], "retain-this")
        self.assertEqual(
            profile["config"]["transformer"]["supported_extension"],
            "retain-this",
        )
        profile_puts = [
            call
            for call in session.mutations
            if call[0] == "PUT" and call[1].startswith("/profiles/")
        ]
        self.assertEqual(len(profile_puts), 1)
        self.assertEqual(
            profile_puts[0][2]["metadata"]["operator_note"], "retain-this"
        )

    def test_all_collection_reads_are_paginated(self):
        session = FakeMainfluxSession()
        first = provisioner(session).provision_device("warehouse-01", "Warehouse")
        session.profiles.insert(
            0,
            {
                "id": "filler-profile",
                "group_id": session.group_id,
                "name": "Filler",
                "config": {},
                "metadata": {},
            },
        )
        session.things.insert(
            0,
            {
                "id": "filler-thing",
                "key": "filler-key",
                "group_id": session.group_id,
                "profile_id": "filler-profile",
                "name": "Filler",
                "type": "device",
                "metadata": {},
            },
        )
        session.rules.insert(
            0,
            {
                "id": "filler-rule",
                "group_id": session.group_id,
                "name": "Filler",
                "description": "Filler",
                "input": {"type": "message"},
                "conditions": [{"field": "x", "comparator": "==", "threshold": 1}],
                "actions": [{"type": "alarm", "level": 1}],
            },
        )
        session.assignments["filler-rule"] = {"filler-thing"}
        session.page_cap = 1
        session.clear_calls()

        second = provisioner(session).provision_device("warehouse-01", "Warehouse")

        self.assertEqual(second.status, "unchanged")
        self.assertEqual(second.thing_id, first.thing_id)
        self.assertEqual(session.mutations, [])
        paged_paths = {
            path
            for method, path, _, params in session.calls
            if method == "GET" and int(params.get("offset", 0)) > 0
        }
        self.assertIn(f"/groups/{session.group_id}/profiles", paged_paths)
        self.assertIn(f"/groups/{session.group_id}/things", paged_paths)
        self.assertIn(f"/svcrules/groups/{session.group_id}/rules", paged_paths)


if __name__ == "__main__":
    unittest.main()
