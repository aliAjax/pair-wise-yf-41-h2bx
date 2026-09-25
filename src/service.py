from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, MergeConflictError, NotFoundError
from .rules import (
    RuleEngine,
    collect_merge_conflicts,
    find_pending_merges,
    plan_merge_cancellation,
    plan_merge_confirmation,
    recalculate_magnitude,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "merge" and action == "confirm":
            return self._confirm_merge(actor, entity, data, expected_version)
        if kind == "merge" and action == "cancel":
            return self._cancel_merge(actor, entity, data, expected_version)
        if kind == "event" and action == "ingest_report":
            return self._ingest_report(actor, entity, data, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _merge_events(self, merge):
        data = merge["data"]
        involved = [data.get("primary_event_id")] + list(data.get("source_event_ids") or [])
        events = {}
        for event_id in involved:
            entity = self.repository.get_entity(event_id)
            if entity:
                events[event_id] = entity
        return events

    def _confirm_merge(self, actor, merge, data, expected_version):
        next_status, patch = self.rules.validate_transition(
            actor, merge, "confirm", dict(data or {}), self._lookup
        )
        expected = int(expected_version) if expected_version is not None else merge["version"]
        involved = [merge["data"].get("primary_event_id")] + list(
            merge["data"].get("source_event_ids") or []
        )
        events = self._merge_events(merge)
        conflicts = collect_merge_conflicts(
            merge["data"],
            events,
            find_pending_merges(self._lookup, involved, exclude_id=merge["id"]),
        )
        if conflicts:
            raise MergeConflictError(conflicts)
        plan = plan_merge_confirmation(merge, events)
        merge_data = dict(merge["data"])
        merge_data.update(patch)
        merge_data["snapshot"] = plan["snapshot"]
        primary_id = merge["data"]["primary_event_id"]
        source_ids = list(merge["data"]["source_event_ids"])
        updates = list(plan["updates"])
        updates.append(
            {"id": merge["id"], "expected_version": expected, "status": next_status, "data": merge_data}
        )
        audits = [
            self.audit.entry(
                merge["id"],
                actor,
                "confirm",
                merge["status"],
                next_status,
                {"primary_event_id": primary_id, "source_event_ids": source_ids},
            ),
            self.audit.entry(
                primary_id,
                actor,
                "merge_apply",
                events[primary_id]["status"],
                events[primary_id]["status"],
                {"merge_id": merge["id"], "source_event_ids": source_ids, "magnitude": plan["magnitude"]},
            ),
        ]
        for source_id in source_ids:
            audits.append(
                self.audit.entry(
                    source_id,
                    actor,
                    "merge_apply",
                    events[source_id]["status"],
                    "merged",
                    {"merge_id": merge["id"], "merged_into": primary_id},
                )
            )
        self.repository.update_entities(updates, audits)
        return self.repository.get_entity(merge["id"])

    def _cancel_merge(self, actor, merge, data, expected_version):
        next_status, patch = self.rules.validate_transition(
            actor, merge, "cancel", dict(data or {}), self._lookup
        )
        expected = int(expected_version) if expected_version is not None else merge["version"]
        merge_data = dict(merge["data"])
        merge_data.update(patch)
        updates = []
        audits = []
        if merge["status"] == "confirmed":
            events = self._merge_events(merge)
            plan = plan_merge_cancellation(merge, events)
            updates.extend(plan["updates"])
            status_by_id = {item["id"]: item["status"] for item in plan["updates"]}
            for event_id in events:
                audits.append(
                    self.audit.entry(
                        event_id,
                        actor,
                        "merge_undo",
                        events[event_id]["status"],
                        status_by_id.get(event_id, events[event_id]["status"]),
                        {"merge_id": merge["id"]},
                    )
                )
        updates.append(
            {"id": merge["id"], "expected_version": expected, "status": next_status, "data": merge_data}
        )
        audits.append(
            self.audit.entry(
                merge["id"], actor, "cancel", merge["status"], next_status, {"patch": patch}
            )
        )
        self.repository.update_entities(updates, audits)
        return self.repository.get_entity(merge["id"])

    def _ingest_report(self, actor, event, data, expected_version):
        next_status, patch = self.rules.validate_transition(
            actor, event, "ingest_report", dict(data or {}), self._lookup
        )
        report = patch["report"]
        target_id = event["data"].get("merged_into")
        if target_id:
            target = self.repository.get_entity(target_id)
            if not target:
                raise NotFoundError("merge target not found: " + target_id)
            if target["status"] in ("merged", "withdrawn"):
                raise InvalidTransition("merge target %s is not accepting reports" % target_id)
            routed = dict(report)
            routed["routed_from"] = event["id"]
            reports = list(target["data"].get("reports") or []) + [routed]
            target_data = dict(target["data"])
            target_data["reports"] = reports
            magnitude = recalculate_magnitude(reports, [target["data"].get("magnitude")])
            if magnitude is not None:
                target_data["magnitude"] = magnitude
            updates = [
                {"id": target["id"], "expected_version": target["version"], "status": target["status"], "data": target_data}
            ]
            audits = [
                self.audit.entry(
                    target["id"],
                    actor,
                    "ingest_report",
                    target["status"],
                    target["status"],
                    {"report": routed, "routed_from": event["id"]},
                ),
                self.audit.entry(
                    event["id"],
                    actor,
                    "ingest_report",
                    event["status"],
                    event["status"],
                    {"report": report, "routed_to": target["id"]},
                ),
            ]
            self.repository.update_entities(updates, audits)
            return self.repository.get_entity(target["id"])
        expected = int(expected_version) if expected_version is not None else event["version"]
        reports = list(event["data"].get("reports") or []) + [report]
        event_data = dict(event["data"])
        event_data["reports"] = reports
        magnitude = recalculate_magnitude(reports, [event["data"].get("magnitude")])
        if magnitude is not None:
            event_data["magnitude"] = magnitude
        updates = [
            {"id": event["id"], "expected_version": expected, "status": next_status, "data": event_data}
        ]
        audits = [
            self.audit.entry(
                event["id"],
                actor,
                "ingest_report",
                event["status"],
                next_status,
                {"report": report},
            )
        ]
        self.repository.update_entities(updates, audits)
        return self.repository.get_entity(event["id"])

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
