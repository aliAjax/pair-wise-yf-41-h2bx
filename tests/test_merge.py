import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst-1", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")
        self.station = Actor("STA-1", "station")

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, title, region, amplitudes, actor=None):
        reports = [
            {
                "station": "STA-%d" % (index + 1),
                "time_offset": index,
                "distance_km": 1.0,
                "amplitude": amplitude,
            }
            for index, amplitude in enumerate(amplitudes)
        ]
        return self.service.create(
            actor or self.analyst,
            "event",
            {
                "title": title,
                "origin_time": "2026-01-01T00:00:00Z",
                "location": region,
                "reports": reports,
            },
        )

    def _versions(self, *events):
        return {event["id"]: event["version"] for event in events}

    def _submit_merge(self, primary, sources, region="Region-A", versions=None):
        return self.service.create(
            self.analyst,
            "merge",
            {
                "primary_event_id": primary["id"],
                "source_event_ids": [source["id"] for source in sources],
                "region": region,
                "expected_versions": versions or self._versions(primary, *sources),
            },
        )

    def test_confirm_merge_moves_reports_and_recalculates(self):
        primary = self._event("main", "Region-A", [2.0, 4.0])
        source = self._event("split", "Region-A", [3.0, 5.0])
        merge = self._submit_merge(primary, [source])
        self.assertEqual(merge["status"], "pending")

        confirmed = self.service.transition(self.reviewer, merge["id"], "confirm")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["data"]["confirmed_by"], "reviewer-1")
        self.assertTrue(confirmed["data"]["confirmed_at"])

        updated_primary = self.service.get(primary["id"])
        self.assertEqual(len(updated_primary["data"]["reports"]), 4)
        self.assertEqual(updated_primary["data"]["magnitude"], 3.5)

        old = self.service.get(source["id"])
        self.assertEqual(old["status"], "merged")
        self.assertEqual(old["data"]["merged_into"], primary["id"])
        self.assertEqual(old["data"]["reports"], [])

        timeline = self.service.audit_log(entity_id=source["id"])
        apply_entries = [row for row in timeline if row["action"] == "merge_apply"]
        self.assertEqual(len(apply_entries), 1)
        self.assertEqual(apply_entries[0]["actor_id"], "reviewer-1")
        self.assertTrue(apply_entries[0]["created_at"])

    def test_submit_conflict_returns_ids_and_current_versions(self):
        primary = self._event("main", "Region-A", [2.0, 4.0])
        source = self._event("split", "Region-A", [3.0, 5.0])
        stale = self._versions(primary, source)
        # someone revises the source event after the analyst read it
        self.service.transition(
            self.admin, primary["id"], "associate", {}, expected_version=primary["version"]
        )
        with self.assertRaises(ConflictError) as ctx:
            self._submit_merge(primary, [source], versions=stale)
        conflicts = ctx.exception.details["conflicts"]
        self.assertEqual(
            [(item["event_id"], item["reason"]) for item in conflicts],
            [(primary["id"], "version_mismatch")],
        )
        self.assertEqual(conflicts[0]["current_version"], primary["version"] + 1)
        # nothing was changed
        self.assertEqual(self.service.get(source["id"])["version"], source["version"])
        self.assertEqual(self.service.list("merge"), [])

    def test_pending_revision_conflict(self):
        primary = self._event("main", "Region-A", [2.0, 4.0])
        source = self._event("split", "Region-A", [3.0, 5.0])
        other = self._event("other", "Region-A", [1.0, 2.0])
        self._submit_merge(primary, [source])
        with self.assertRaises(ConflictError) as ctx:
            self._submit_merge(other, [source])
        reasons = {
            item["reason"] for item in ctx.exception.details["conflicts"]
        }
        self.assertIn("pending_revision", reasons)

    def test_region_mismatch_conflict(self):
        primary = self._event("main", "Region-A", [2.0, 4.0])
        source = self._event("split", "Region-B", [3.0, 5.0])
        with self.assertRaises(ConflictError) as ctx:
            self._submit_merge(primary, [source], region="Region-A")
        conflicts = ctx.exception.details["conflicts"]
        self.assertEqual(conflicts[0]["event_id"], source["id"])
        self.assertEqual(conflicts[0]["reason"], "region_mismatch")
        self.assertEqual(conflicts[0]["current_version"], source["version"])

    def test_confirm_conflict_when_event_changed_after_submit(self):
        primary = self._event("main", "Region-A", [2.0, 4.0])
        source = self._event("split", "Region-A", [3.0, 5.0])
        merge = self._submit_merge(primary, [source])
        self.service.transition(
            self.admin, primary["id"], "associate", {}, expected_version=primary["version"]
        )
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.reviewer, merge["id"], "confirm")
        self.assertEqual(
            ctx.exception.details["conflicts"][0]["event_id"], primary["id"]
        )
        self.assertEqual(self.service.get(merge["id"])["status"], "pending")
        self.assertEqual(len(self.service.get(primary["id"])["data"]["reports"]), 2)

    def test_late_report_redirects_to_primary(self):
        primary = self._event("main", "Region-A", [2.0, 4.0])
        source = self._event("split", "Region-A", [3.0, 5.0])
        merge = self._submit_merge(primary, [source])
        self.service.transition(self.reviewer, merge["id"], "confirm")

        updated = self.service.transition(
            self.station,
            source["id"],
            "report",
            {"station": "STA-9", "time_offset": 3, "distance_km": 0.8, "amplitude": 6.0},
        )
        self.assertEqual(updated["id"], primary["id"])
        reports = updated["data"]["reports"]
        self.assertEqual(len(reports), 5)
        self.assertEqual(reports[-1]["merged_from"], source["id"])
        self.assertEqual(reports[-1]["merge_id"], merge["id"])
        self.assertEqual(updated["data"]["magnitude"], 4.0)
        # old id stays merged and keeps no reports
        self.assertEqual(self.service.get(source["id"])["data"]["reports"], [])

    def test_cancel_restores_reports_and_ownership(self):
        primary = self._event("main", "Region-A", [2.0, 4.0])
        source = self._event("split", "Region-A", [3.0, 5.0])
        merge = self._submit_merge(primary, [source])
        self.service.transition(self.reviewer, merge["id"], "confirm")
        # late report arrives on the old id while merged
        self.service.transition(
            self.station,
            source["id"],
            "report",
            {"station": "STA-9", "time_offset": 3, "distance_km": 0.8, "amplitude": 6.0},
        )

        cancelled = self.service.transition(self.reviewer, merge["id"], "cancel")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["data"]["cancelled_by"], "reviewer-1")

        restored = self.service.get(source["id"])
        self.assertEqual(restored["status"], "candidate")
        self.assertNotIn("merged_into", restored["data"])
        self.assertEqual(len(restored["data"]["reports"]), 3)
        self.assertEqual(restored["data"]["magnitude"], 5.0)
        for report in restored["data"]["reports"]:
            self.assertNotIn("merged_from", report)
            self.assertNotIn("merge_id", report)

        updated_primary = self.service.get(primary["id"])
        self.assertEqual(len(updated_primary["data"]["reports"]), 2)
        self.assertEqual(updated_primary["data"]["magnitude"], 3.0)

        # late reports now land on the restored original id
        after = self.service.transition(
            self.station,
            source["id"],
            "report",
            {"station": "STA-10", "time_offset": 4, "distance_km": 0.9},
        )
        self.assertEqual(after["id"], source["id"])
        self.assertEqual(len(after["data"]["reports"]), 4)

    def test_cancel_pending_merge_leaves_events_untouched(self):
        primary = self._event("main", "Region-A", [2.0, 4.0])
        source = self._event("split", "Region-A", [3.0, 5.0])
        merge = self._submit_merge(primary, [source])
        cancelled = self.service.transition(self.analyst, merge["id"], "cancel")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.service.get(primary["id"])["version"], primary["version"])
        self.assertEqual(self.service.get(source["id"])["version"], source["version"])

    def test_merge_listing_and_permissions(self):
        primary = self._event("main", "Region-A", [2.0, 4.0])
        source = self._event("split", "Region-A", [3.0, 5.0])
        merge = self._submit_merge(primary, [source])
        listed = self.service.list("merge")
        self.assertEqual([item["id"] for item in listed], [merge["id"]])
        self.assertEqual(self.service.list("merge", status="pending")[0]["id"], merge["id"])

        with self.assertRaises(PermissionDenied):
            self.service.transition(self.analyst, merge["id"], "confirm")
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("viewer", "viewer"), merge["id"], "cancel")
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("viewer", "viewer"),
                "merge",
                {
                    "primary_event_id": primary["id"],
                    "source_event_ids": [source["id"]],
                    "region": "Region-A",
                    "expected_versions": self._versions(primary, source),
                },
            )

    def test_single_event_flow_still_works(self):
        event = self._event("solo", "Region-A", [2.0, 4.0])
        event = self.service.transition(self.analyst, event["id"], "associate", {})
        self.assertEqual(event["status"], "associated")
        event = self.service.transition(
            self.reviewer, event["id"], "review", {"reviewer": "R-1", "magnitude": 4.2}
        )
        self.assertEqual(event["status"], "reviewed")
        event = self.service.transition(
            self.reviewer, event["id"], "publish", {"communication_id": "C-1"}
        )
        self.assertEqual(event["status"], "published")


if __name__ == "__main__":
    unittest.main()
