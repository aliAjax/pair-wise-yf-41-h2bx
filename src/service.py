from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .repository import utcnow
from .rules import RuleEngine, merged_magnitude, tag_report, untag_report


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        kind = self.rules.normalize_kind(kind)
        if field == "status":
            return self.repository.list_entities(kind=kind, status=value)
        return self.repository.find_entities(kind, field, value)

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
        payload = self.rules.validate_create(actor, kind, payload, self._lookup)
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
        if entity["kind"] == "merge" and action == "confirm" and entity["status"] == "pending":
            return self._confirm_merge(actor, entity, dict(data or {}), expected_version)
        if entity["kind"] == "merge" and action == "cancel" and entity["status"] == "confirmed":
            return self._cancel_merge(actor, entity, dict(data or {}), expected_version)
        if entity["kind"] == "event" and action == "report":
            return self._receive_report(actor, entity, dict(data or {}), expected_version)
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

    def _merge_events(self, merge_data):
        primary_id = merge_data.get("primary_event_id")
        source_ids = list(merge_data.get("source_event_ids") or [])
        primary = self.repository.get_entity(primary_id)
        if not primary:
            raise NotFoundError("event not found: " + str(primary_id))
        sources = []
        for source_id in source_ids:
            source = self.repository.get_entity(source_id)
            if not source:
                raise NotFoundError("event not found: " + str(source_id))
            sources.append(source)
        return primary, sources

    def _confirm_merge(self, actor, merge, data, expected_version):
        self.rules.validate_transition(actor, merge, "confirm", data, self._lookup)
        merge_data = dict(merge["data"])
        primary, sources = self._merge_events(merge_data)
        now = utcnow()

        primary_data = dict(primary["data"])
        reports = list(primary_data.get("reports") or [])
        previous_statuses = {}
        for source in sources:
            previous_statuses[source["id"]] = source["status"]
            for report in source["data"].get("reports") or []:
                reports.append(tag_report(report, source["id"], merge["id"]))
        primary_data["reports"] = reports
        magnitude = merged_magnitude(reports)
        if magnitude is not None:
            primary_data["magnitude"] = magnitude

        merge_data["previous_statuses"] = previous_statuses
        merge_data["confirmed_by"] = actor.user_id
        merge_data["confirmed_at"] = now

        expected = (
            int(expected_version) if expected_version is not None else merge["version"]
        )
        updates = [
            {
                "id": primary["id"],
                "status": primary["status"],
                "data": primary_data,
            }
        ]
        audits = [
            {
                "entity_id": primary["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": "merge_apply",
                "from_status": primary["status"],
                "to_status": primary["status"],
                "detail": {
                    "merge_id": merge["id"],
                    "received_from": [source["id"] for source in sources],
                    "magnitude": primary_data.get("magnitude"),
                },
            }
        ]
        for source in sources:
            source_data = dict(source["data"])
            source_data["reports"] = []
            source_data["merged_into"] = primary["id"]
            source_data["merged_via"] = merge["id"]
            updates.append(
                {"id": source["id"], "status": "merged", "data": source_data}
            )
            audits.append(
                {
                    "entity_id": source["id"],
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": "merge_apply",
                    "from_status": source["status"],
                    "to_status": "merged",
                    "detail": {"merge_id": merge["id"], "merged_into": primary["id"]},
                }
            )
        updates.append(
            {
                "id": merge["id"],
                "expected_version": expected,
                "status": "confirmed",
                "data": merge_data,
            }
        )
        audits.append(
            {
                "entity_id": merge["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": "confirm",
                "from_status": merge["status"],
                "to_status": "confirmed",
                "detail": {
                    "primary_event_id": primary["id"],
                    "source_event_ids": [source["id"] for source in sources],
                },
            }
        )
        self.repository.update_entities(updates, audits)
        return self.repository.get_entity(merge["id"])

    def _cancel_merge(self, actor, merge, data, expected_version):
        self.rules.validate_transition(actor, merge, "cancel", data, self._lookup)
        merge_data = dict(merge["data"])
        primary, sources = self._merge_events(merge_data)
        previous_statuses = merge_data.get("previous_statuses") or {}
        now = utcnow()

        primary_data = dict(primary["data"])
        kept_reports = []
        returned = {source["id"]: [] for source in sources}
        for report in primary_data.get("reports") or []:
            source_id = report.get("merged_from")
            if report.get("merge_id") == merge["id"] and source_id in returned:
                returned[source_id].append(untag_report(report))
            else:
                kept_reports.append(report)
        primary_data["reports"] = kept_reports
        magnitude = merged_magnitude(kept_reports)
        if magnitude is not None:
            primary_data["magnitude"] = magnitude

        merge_data["cancelled_by"] = actor.user_id
        merge_data["cancelled_at"] = now

        expected = (
            int(expected_version) if expected_version is not None else merge["version"]
        )
        updates = [
            {
                "id": primary["id"],
                "status": primary["status"],
                "data": primary_data,
            }
        ]
        audits = [
            {
                "entity_id": primary["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": "merge_revert",
                "from_status": primary["status"],
                "to_status": primary["status"],
                "detail": {
                    "merge_id": merge["id"],
                    "returned_to": [source["id"] for source in sources],
                    "magnitude": primary_data.get("magnitude"),
                },
            }
        ]
        for source in sources:
            source_data = dict(source["data"])
            source_data.pop("merged_into", None)
            source_data.pop("merged_via", None)
            source_reports = returned[source["id"]]
            source_data["reports"] = source_reports
            magnitude = merged_magnitude(source_reports)
            if magnitude is not None:
                source_data["magnitude"] = magnitude
            restored = previous_statuses.get(source["id"], source["status"])
            updates.append(
                {"id": source["id"], "status": restored, "data": source_data}
            )
            audits.append(
                {
                    "entity_id": source["id"],
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": "merge_revert",
                    "from_status": source["status"],
                    "to_status": restored,
                    "detail": {"merge_id": merge["id"], "restored_from": primary["id"]},
                }
            )
        updates.append(
            {
                "id": merge["id"],
                "expected_version": expected,
                "status": "cancelled",
                "data": merge_data,
            }
        )
        audits.append(
            {
                "entity_id": merge["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": "cancel",
                "from_status": merge["status"],
                "to_status": "cancelled",
                "detail": {
                    "primary_event_id": primary["id"],
                    "source_event_ids": [source["id"] for source in sources],
                },
            }
        )
        self.repository.update_entities(updates, audits)
        return self.repository.get_entity(merge["id"])

    def _resolve_owner(self, entity):
        """Follow merged_into links to the event currently owning reports."""
        seen = set()
        owner = entity
        while owner["status"] == "merged" and owner["data"].get("merged_into"):
            if owner["id"] in seen:
                break
            seen.add(owner["id"])
            nxt = self.repository.get_entity(owner["data"]["merged_into"])
            if not nxt:
                break
            owner = nxt
        return owner

    def _receive_report(self, actor, entity, data, expected_version):
        owner = self._resolve_owner(entity)
        report = dict(data)
        redirected_from = None
        if owner["id"] != entity["id"]:
            redirected_from = entity["id"]
            report["merged_from"] = entity["id"]
            if entity["data"].get("merged_via"):
                report["merge_id"] = entity["data"]["merged_via"]
        next_status, _ = self.rules.validate_transition(
            actor, owner, "report", dict(report), self._lookup
        )
        owner_data = dict(owner["data"])
        reports = list(owner_data.get("reports") or [])
        reports.append(report)
        owner_data["reports"] = reports
        magnitude = merged_magnitude(reports)
        if magnitude is not None:
            owner_data["magnitude"] = magnitude
        expected = (
            int(expected_version) if expected_version is not None else owner["version"]
        )
        updated = self.repository.update_entity(
            owner["id"], expected, next_status, owner_data
        )
        detail = {"report": report}
        if redirected_from:
            detail["redirected_from"] = redirected_from
        self.audit.record(
            owner["id"],
            actor,
            "report",
            owner["status"],
            updated["status"],
            detail,
        )
        return updated

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
