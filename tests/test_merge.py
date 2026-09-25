import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, MergeConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

REGION_A = "Region-A"
REGION_B = "Region-B"


def _event_data(title, region, amplitudes, prefix):
    reports = [
        {"station": "%s-A" % prefix, "time_offset": 1, "distance_km": 0.5, "amplitude": amplitudes[0]},
        {"station": "%s-B" % prefix, "time_offset": 2, "distance_km": 1.0, "amplitude": amplitudes[1]},
    ]
    return {
        "title": title,
        "origin_time": "2026-01-01T00:00:00Z",
        "location": region,
        "reports": reports,
    }


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.analyst = Actor("analyst-1", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")
        self.station = Actor("sta-1", "station")
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def make_event(self, title, amplitudes, region=REGION_A, prefix="EV"):
        return self.service.create(
            self.analyst, "event", _event_data(title, region, amplitudes, prefix)
        )

    def merge_payload(self, primary, sources, region=REGION_A, versions=None):
        involved = [primary] + list(sources)
        versions = versions or {item["id"]: item["version"] for item in involved}
        return {
            "primary_event_id": primary["id"],
            "source_event_ids": [item["id"] for item in sources],
            "region": region,
            "expected_versions": versions,
        }

    def submit_merge(self, primary, sources, **kwargs):
        return self.service.create(
            self.analyst, "merge", self.merge_payload(primary, sources, **kwargs)
        )

    def test_create_view_confirm_merge_recomputes_magnitude_and_keeps_old_ids(self):
        primary = self.make_event("P", [2.0, 4.0], prefix="P")
        source = self.make_event("S", [3.0, 5.0], prefix="S")
        merge = self.submit_merge(primary, [source])

        self.assertEqual(merge["status"], "pending")
        self.assertEqual(len(self.service.list("merges")), 1)
        self.assertEqual(self.service.get(merge["id"])["id"], merge["id"])

        confirmed = self.service.transition(self.reviewer, merge["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "confirmed")

        merged_primary = self.service.get(primary["id"])
        self.assertEqual(len(merged_primary["data"]["reports"]), 4)
        # median of 2, 3, 4, 5
        self.assertEqual(merged_primary["data"]["magnitude"], 3.5)
        self.assertEqual(merged_primary["data"]["merged_from"], [source["id"]])

        old = self.service.get(source["id"])
        self.assertEqual(old["status"], "merged")
        self.assertEqual(old["data"]["merged_into"], primary["id"])
        self.assertEqual(len(old["data"]["reports"]), 2)

        timeline = self.service.audit_log(source["id"])
        apply_entries = [entry for entry in timeline if entry["action"] == "merge_apply"]
        self.assertEqual(apply_entries[0]["actor_id"], "reviewer-1")
        self.assertTrue(apply_entries[0]["created_at"])

    def test_version_mismatch_blocks_merge_without_changes(self):
        primary = self.make_event("P", [2.0, 4.0], prefix="P")
        source = self.make_event("S", [3.0, 5.0], prefix="S")
        payload = self.merge_payload(
            primary, [source], versions={primary["id"]: primary["version"], source["id"]: 99}
        )
        with self.assertRaises(MergeConflictError) as caught:
            self.service.create(self.analyst, "merge", payload)
        conflicts = caught.exception.conflicts
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["event_id"], source["id"])
        self.assertEqual(conflicts[0]["reason"], "version_mismatch")
        self.assertEqual(conflicts[0]["current_version"], source["version"])
        self.assertEqual(self.service.list("merges"), [])
        self.assertEqual(self.service.get(source["id"])["status"], "candidate")

    def test_cross_region_blocks_merge(self):
        primary = self.make_event("P", [2.0, 4.0], prefix="P")
        other = self.make_event("O", [1.0, 2.0], region=REGION_B, prefix="O")
        with self.assertRaises(MergeConflictError) as caught:
            self.submit_merge(primary, [other], region=REGION_A)
        self.assertEqual(caught.exception.conflicts[0]["reason"], "cross_region")
        self.assertEqual(caught.exception.conflicts[0]["current_version"], other["version"])

    def test_event_in_another_pending_merge_is_pending_revision(self):
        primary = self.make_event("P", [2.0, 4.0], prefix="P")
        source = self.make_event("S", [3.0, 5.0], prefix="S")
        extra = self.make_event("E", [1.5, 2.5], prefix="E")
        self.submit_merge(primary, [source])
        with self.assertRaises(MergeConflictError) as caught:
            self.submit_merge(extra, [source])
        conflict = caught.exception.conflicts[0]
        self.assertEqual(conflict["reason"], "pending_revision")
        self.assertEqual(conflict["event_id"], source["id"])

    def test_late_reports_route_to_primary_then_return_after_undo(self):
        primary = self.make_event("P", [2.0, 4.0], prefix="P")
        source = self.make_event("S", [3.0, 5.0], prefix="S")
        merge = self.submit_merge(primary, [source])
        self.service.transition(self.reviewer, merge["id"], "confirm", {})

        # a late supplementary report lands on the old number and is routed
        target = self.service.transition(
            self.station,
            source["id"],
            "ingest_report",
            {"report": {"station": "S-LATE", "time_offset": 3, "distance_km": 0.8, "amplitude": 4.5}},
        )
        self.assertEqual(target["id"], primary["id"])
        routed = target["data"]["reports"][-1]
        self.assertEqual(routed["routed_from"], source["id"])
        self.assertEqual(len(self.service.get(source["id"])["data"]["reports"]), 2)

        # reviewer undoes the merge
        cancelled = self.service.transition(
            self.reviewer, merge["id"], "cancel", {"reason": "wrong grouping"}
        )
        self.assertEqual(cancelled["status"], "cancelled")

        restored_source = self.service.get(source["id"])
        self.assertEqual(restored_source["status"], "candidate")
        self.assertNotIn("merged_into", restored_source["data"])
        # original reports plus the routed late report, tag removed
        self.assertEqual(len(restored_source["data"]["reports"]), 3)
        self.assertNotIn("routed_from", restored_source["data"]["reports"][-1])

        restored_primary = self.service.get(primary["id"])
        self.assertEqual(len(restored_primary["data"]["reports"]), 2)
        self.assertNotIn("merged_from", restored_primary["data"])

        # reports after the undo stay on the restored number
        again = self.service.transition(
            self.station,
            source["id"],
            "ingest_report",
            {"report": {"station": "S-AGAIN", "time_offset": 4, "distance_km": 1.1, "amplitude": 4.8}},
        )
        self.assertEqual(again["id"], source["id"])
        self.assertEqual(len(again["data"]["reports"]), 4)

    def test_cancel_pending_merge_does_not_touch_events(self):
        primary = self.make_event("P", [2.0, 4.0], prefix="P")
        source = self.make_event("S", [3.0, 5.0], prefix="S")
        merge = self.submit_merge(primary, [source])
        cancelled = self.service.transition(
            self.reviewer, merge["id"], "cancel", {"reason": "analyst retracted"}
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.service.get(primary["id"])["version"], primary["version"])
        self.assertEqual(self.service.get(source["id"])["status"], "candidate")

    def test_permissions_for_merge_and_confirm(self):
        primary = self.make_event("P", [2.0, 4.0], prefix="P")
        source = self.make_event("S", [3.0, 5.0], prefix="S")
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("viewer-1", "viewer"), "merge", self.merge_payload(primary, [source])
            )
        merge = self.submit_merge(primary, [source])
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.analyst, merge["id"], "confirm", {})

    def test_ingest_report_rejected_for_withdrawn_event(self):
        primary = self.make_event("P", [2.0, 4.0], prefix="P")
        source = self.make_event("S", [3.0, 5.0], prefix="S")
        self.service.transition(self.admin, source["id"], "associate", {})
        self.service.transition(
            self.reviewer, source["id"], "review", {"reviewer": "R", "magnitude": 3.1}
        )
        self.service.transition(self.reviewer, source["id"], "publish", {"communication_id": "C"})
        self.service.transition(self.reviewer, source["id"], "withdraw", {"reason": "dup"})
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.station,
                source["id"],
                "ingest_report",
                {"report": {"station": "X"}},
            )


if __name__ == "__main__":
    unittest.main()
