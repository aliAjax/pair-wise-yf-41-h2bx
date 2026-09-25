from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
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


def _event_region(event):
    data = event.get("data") or {}
    return data.get("region") or data.get("location")


def _lookup_events(lookup, event_ids):
    events = {}
    for event_id in event_ids:
        rows = lookup("event", "id", event_id) if lookup else []
        if not rows:
            raise ValidationError("event not found: " + str(event_id))
        events[event_id] = rows[0]
    return events


def _merge_reference_ids(merge_entity):
    data = merge_entity.get("data") or {}
    ids = [data.get("primary_event_id")]
    ids.extend(data.get("source_event_ids") or [])
    return {item for item in ids if item}


def collect_merge_conflicts(data, lookup, exclude_merge_id=None):
    """Check a merge request against current events.

    Returns a list of conflicts, each carrying the conflicting event id and
    its current version so the caller can report them without touching data.
    """
    primary_id = data.get("primary_event_id")
    source_ids = list(data.get("source_event_ids") or [])
    region = data.get("region")
    expected = data.get("expected_versions") or {}
    event_ids = [primary_id] + source_ids
    events = _lookup_events(lookup, event_ids)

    pending_ids = set()
    confirmed_primaries = set()
    if lookup:
        for merge in lookup("merge", "status", "pending") or []:
            if exclude_merge_id and merge["id"] == exclude_merge_id:
                continue
            pending_ids.update(_merge_reference_ids(merge))
        for merge in lookup("merge", "status", "confirmed") or []:
            if exclude_merge_id and merge["id"] == exclude_merge_id:
                continue
            confirmed_primaries.add((merge.get("data") or {}).get("primary_event_id"))

    conflicts = []
    for event_id in event_ids:
        event = events[event_id]
        current_version = event["version"]

        def add(reason):
            conflicts.append(
                {
                    "event_id": event_id,
                    "current_version": current_version,
                    "reason": reason,
                }
            )

        submitted = expected.get(event_id)
        if submitted is None or int(submitted) != current_version:
            add("version_mismatch")
        if event["status"] in ("merged", "withdrawn"):
            add("status_" + event["status"])
        if event_id in pending_ids:
            add("pending_revision")
        if region is not None and _event_region(event) != region:
            add("region_mismatch")
        if event_id in source_ids and event_id in confirmed_primaries:
            add("already_merge_primary")
    return conflicts


def _validate_merge_request(actor, data, lookup):
    primary_id = data.get("primary_event_id")
    source_ids = list(data.get("source_event_ids") or [])
    if primary_id in source_ids:
        raise ValidationError("primary event cannot be merged into itself")
    if len(set(source_ids)) != len(source_ids):
        raise ValidationError("duplicate source event id")
    conflicts = collect_merge_conflicts(data, lookup)
    if conflicts:
        raise ConflictError(
            "merge request conflicts", details={"conflicts": conflicts}
        )


def _validate_merge_confirm(actor, entity, data, lookup):
    conflicts = collect_merge_conflicts(
        entity["data"], lookup, exclude_merge_id=entity["id"]
    )
    if conflicts:
        raise ConflictError(
            "merge confirm conflicts", details={"conflicts": conflicts}
        )
    return {}


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


def merged_magnitude(reports):
    """Recalculate magnitude from every report carrying an amplitude."""
    amplitudes = [
        report["amplitude"]
        for report in reports or []
        if report.get("amplitude") is not None
    ]
    if not amplitudes:
        return None
    return magnitude_median(amplitudes)


def tag_report(report, source_event_id, merge_id):
    tagged = dict(report)
    tagged["merged_from"] = source_event_id
    tagged["merge_id"] = merge_id
    return tagged


def untag_report(report):
    cleaned = dict(report)
    cleaned.pop("merged_from", None)
    cleaned.pop("merge_id", None)
    return cleaned


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event, 'merge': _validate_merge_request}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate, ('merge', 'confirm'): _validate_merge_confirm}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event', 'merges': 'merge'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate', 'merge': 'pending'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'withdraw': (('published', 'revised'), 'withdrawn'), 'report': (('candidate', 'associated', 'reviewed', 'published', 'revised'), None)}, 'merge': {'confirm': (('pending',), 'confirmed'), 'cancel': (('pending', 'confirmed'), 'cancelled')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports'), 'merge': ('primary_event_id', 'source_event_ids', 'region', 'expected_versions')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',), ('event', 'report'): ('station',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst'), 'merge': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer'), 'report': ('admin', 'station'), 'confirm': ('admin', 'reviewer'), 'cancel': ('admin', 'analyst', 'reviewer')}

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
        extra = custom(actor, data, lookup) if custom else None
        payload = dict(data)
        if extra:
            payload.update(extra)
        return payload

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        if next_status is None:
            next_status = entity["status"]
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
