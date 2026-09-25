from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    MergeConflictError,
    PermissionDenied,
    ValidationError,
)


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    return {"associated_count": len(reports)}


def associate_reports(reports, max_delta=120, max_distance=3.0):
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(report.get("distance_km", 0)) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def recalculate_magnitude(reports, fallbacks=()):
    """Median magnitude for merged reports; fall back to event magnitudes, then None."""
    amplitudes = [
        float(report["amplitude"])
        for report in reports or []
        if isinstance(report, dict) and report.get("amplitude") is not None
    ]
    if amplitudes:
        return magnitude_median(amplitudes)
    values = [float(value) for value in (fallbacks or []) if value is not None]
    if values:
        return magnitude_median(values)
    return None


def _validate_ingest_report(actor, entity, data, lookup):
    report = data.get("report")
    if not isinstance(report, dict) or not report.get("station"):
        raise ValidationError("report with station is required")
    return {"report": report}


def _merge_involved(data):
    return [data.get("primary_event_id")] + list(data.get("source_event_ids") or [])


def find_pending_merges(lookup, involved, exclude_id=None):
    """Other unreviewed merges that already touch any of the involved events."""
    found = {}
    for event_id in involved:
        for row in (lookup("merge", "involved_event_ids", event_id) if lookup else None) or []:
            if row["status"] == "pending" and row["id"] != exclude_id:
                found[row["id"]] = row
    return list(found.values())


def collect_merge_conflicts(data, events, pending_merges):
    """Pure precheck: version mismatch, pending revision, cross region, bad status."""
    expected = data.get("expected_versions") or {}
    region = data.get("region")
    conflicts = []
    pending_by_event = {}
    for merge in pending_merges:
        for event_id in merge["data"].get("involved_event_ids") or []:
            pending_by_event.setdefault(event_id, merge["id"])
    for event_id in _merge_involved(data):
        event = events.get(event_id)
        wanted = expected.get(event_id)
        if event is None:
            conflicts.append(
                {"event_id": event_id, "reason": "not_found", "expected_version": wanted, "current_version": None}
            )
            continue
        try:
            version_ok = wanted is not None and int(wanted) == int(event["version"])
        except (TypeError, ValueError):
            version_ok = False
        if not version_ok:
            conflicts.append(
                {
                    "event_id": event_id,
                    "reason": "version_mismatch",
                    "expected_version": wanted,
                    "current_version": event["version"],
                }
            )
            continue
        if event["status"] in ("merged", "withdrawn"):
            conflicts.append(
                {
                    "event_id": event_id,
                    "reason": "invalid_status",
                    "status": event["status"],
                    "expected_version": wanted,
                    "current_version": event["version"],
                }
            )
            continue
        if event["data"].get("location") != region:
            conflicts.append(
                {
                    "event_id": event_id,
                    "reason": "cross_region",
                    "location": event["data"].get("location"),
                    "expected_version": wanted,
                    "current_version": event["version"],
                }
            )
            continue
        if event_id in pending_by_event:
            conflicts.append(
                {
                    "event_id": event_id,
                    "reason": "pending_revision",
                    "merge_id": pending_by_event[event_id],
                    "expected_version": wanted,
                    "current_version": event["version"],
                }
            )
    return conflicts


def _validate_merge(actor, data, lookup):
    primary_id = data.get("primary_event_id")
    source_ids = data.get("source_event_ids")
    if not isinstance(source_ids, list) or not source_ids:
        raise ValidationError("source_event_ids must be a non-empty list")
    if primary_id in source_ids:
        raise ValidationError("primary event cannot be a source event")
    if len(set(source_ids)) != len(source_ids):
        raise ValidationError("duplicate source event ids")
    involved = _merge_involved(data)
    events = {}
    for event_id in involved:
        row = _find_one(lookup, "event", "id", event_id)
        if row:
            events[event_id] = row
    conflicts = collect_merge_conflicts(data, events, find_pending_merges(lookup, involved))
    if conflicts:
        raise MergeConflictError(conflicts)
    return {"involved_event_ids": involved}


def plan_merge_confirmation(merge, events):
    """Build the multi-event update plan and a snapshot for undo."""
    data = merge["data"]
    primary_id = data["primary_event_id"]
    source_ids = list(data["source_event_ids"])
    primary = events[primary_id]
    combined = list(primary["data"].get("reports") or [])
    for source_id in source_ids:
        for report in events[source_id]["data"].get("reports") or []:
            tagged = dict(report)
            tagged["origin_event"] = source_id
            combined.append(tagged)
    all_magnitudes = [
        events[event_id]["data"].get("magnitude")
        for event_id in [primary_id] + source_ids
    ]
    magnitude = recalculate_magnitude(combined, all_magnitudes)
    snapshot_events = {}
    for event_id in [primary_id] + source_ids:
        entity = events[event_id]
        snapshot_events[event_id] = {
            "status": entity["status"],
            "reports": entity["data"].get("reports") or [],
            "magnitude": entity["data"].get("magnitude"),
        }
    primary_data = dict(primary["data"])
    primary_data["reports"] = combined
    if magnitude is not None:
        primary_data["magnitude"] = magnitude
    primary_data["merged_from"] = source_ids
    updates = [
        {
            "id": primary_id,
            "expected_version": primary["version"],
            "status": primary["status"],
            "data": primary_data,
        }
    ]
    for source_id in source_ids:
        source = events[source_id]
        source_data = dict(source["data"])
        source_data["merged_into"] = primary_id
        updates.append(
            {
                "id": source_id,
                "expected_version": source["version"],
                "status": "merged",
                "data": source_data,
            }
        )
    return {"updates": updates, "snapshot": {"events": snapshot_events}, "magnitude": magnitude}


def plan_merge_cancellation(merge, events):
    """Restore ownership after an undo, including reports routed in while merged."""
    data = merge["data"]
    primary_id = data["primary_event_id"]
    source_ids = list(data["source_event_ids"])
    snapshot = (data.get("snapshot") or {}).get("events") or {}
    primary = events[primary_id]
    current_reports = primary["data"].get("reports") or []
    own_reports = [
        report for report in current_reports
        if not report.get("origin_event") and not report.get("routed_from")
    ]
    primary_data = dict(primary["data"])
    primary_data["reports"] = own_reports
    primary_data.pop("merged_from", None)
    primary_magnitude = recalculate_magnitude(
        own_reports, [(snapshot.get(primary_id) or {}).get("magnitude")]
    )
    if primary_magnitude is not None:
        primary_data["magnitude"] = primary_magnitude
    updates = [
        {
            "id": primary_id,
            "expected_version": primary["version"],
            "status": primary["status"],
            "data": primary_data,
        }
    ]
    for source_id in source_ids:
        source = events[source_id]
        snap = snapshot.get(source_id) or {}
        restored = list(snap.get("reports") or [])
        for report in current_reports:
            if report.get("routed_from") == source_id:
                clean = dict(report)
                clean.pop("routed_from", None)
                restored.append(clean)
        source_data = dict(source["data"])
        source_data["reports"] = restored
        source_data.pop("merged_into", None)
        source_magnitude = recalculate_magnitude(restored, [snap.get("magnitude")])
        if source_magnitude is not None:
            source_data["magnitude"] = source_magnitude
        updates.append(
            {
                "id": source_id,
                "expected_version": source["version"],
                "status": snap.get("status") or source["status"],
                "data": source_data,
            }
        )
    return {"updates": updates}


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event, 'merge': _validate_merge}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate, ('event', 'ingest_report'): _validate_ingest_report}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event', 'merges': 'merge'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate', 'merge': 'pending'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'withdraw': (('published', 'revised'), 'withdrawn'), 'ingest_report': (('candidate', 'associated', 'reviewed', 'published', 'revised', 'merged'), None)}, 'merge': {'confirm': (('pending',), 'confirmed'), 'cancel': (('pending', 'confirmed'), 'cancelled')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports'), 'merge': ('primary_event_id', 'source_event_ids', 'region', 'expected_versions')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',), ('event', 'ingest_report'): ('report',), ('merge', 'cancel'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst'), 'merge': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer'), 'ingest_report': ('admin', 'station'), 'confirm': ('admin', 'reviewer'), 'cancel': ('admin', 'reviewer')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            extra = custom(actor, data, lookup)
            if extra:
                data.update(extra)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if next_status is None:
            next_status = entity["status"]
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
