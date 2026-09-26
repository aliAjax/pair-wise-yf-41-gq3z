from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 关联质量关口口径
SUSPECT_TIME_OFFSET = 120.0          # 时间偏移绝对值超过 120 秒 -> 存疑
SUSPECT_DISTANCE_KM = 3.0            # 距离超过 3 公里 -> 存疑
REVIEW_MIN_VALID_REPORTS = 3         # 有效报文少于 3 条不能复核
REVIEW_MAX_AVERAGE_DISTANCE_KM = 2.0  # 平均距离超过 2 公里不能复核


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_suspect_report(report):
    return (
        abs(_to_float(report.get("time_offset"))) > SUSPECT_TIME_OFFSET
        or _to_float(report.get("distance_km")) > SUSPECT_DISTANCE_KM
    )


def evaluate_report_quality(reports):
    """按口径评估一批台站报文。

    同一台站重复上报只保留时间偏移绝对值更小的一份（持平保留先到的）；
    超过 120 秒或 3 公里的保留报文标记为存疑，不计入有效报文。
    """
    raw = list(reports or [])
    winners = {}
    order = []
    duplicate_reports = []
    for index, report in enumerate(raw):
        station = report.get("station") or report.get("station_code")
        key = str(station) if station else "#%d" % index
        candidate = dict(report)
        if key not in winners:
            winners[key] = candidate
            order.append(key)
            continue
        current = winners[key]
        if abs(_to_float(candidate.get("time_offset"))) < abs(
            _to_float(current.get("time_offset"))
        ):
            duplicate_reports.append(dict(current, dropped="duplicate_station"))
            winners[key] = candidate
        else:
            duplicate_reports.append(dict(candidate, dropped="duplicate_station"))

    accepted_reports = []
    suspect_reports = []
    valid_reports = []
    for key in order:
        report = dict(winners[key])
        report["suspect"] = _is_suspect_report(report)
        accepted_reports.append(report)
        if report["suspect"]:
            suspect_reports.append(report)
        else:
            valid_reports.append(report)

    valid_count = len(valid_reports)
    if valid_count:
        average_distance = round(
            sum(_to_float(item.get("distance_km")) for item in valid_reports)
            / valid_count,
            3,
        )
    else:
        average_distance = 0.0
    quality_score = round(100 * valid_count / len(raw)) if raw else 0
    passes_gate = (
        valid_count >= REVIEW_MIN_VALID_REPORTS
        and average_distance <= REVIEW_MAX_AVERAGE_DISTANCE_KM
    )
    return {
        "accepted_reports": accepted_reports,
        "suspect_reports": suspect_reports,
        "valid_reports": valid_reports,
        "duplicate_reports": duplicate_reports,
        "metrics": {
            "raw_count": len(raw),
            "accepted_count": len(accepted_reports),
            "valid_count": valid_count,
            "suspect_count": len(suspect_reports),
            "duplicate_count": len(duplicate_reports),
            "average_distance_km": average_distance,
        },
        "quality_score": quality_score,
        "review_gate": {
            "passes": passes_gate,
            "min_valid_reports": REVIEW_MIN_VALID_REPORTS,
            "max_average_distance_km": REVIEW_MAX_AVERAGE_DISTANCE_KM,
        },
    }


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
    quality = evaluate_report_quality(reports)
    # 关联动作固化：采用/存疑报文、质量分、口径与版本一次性快照，此后不可变
    snapshot = {
        "frozen_version": int(entity.get("version", 1)) + 1,
        "frozen_at": _utcnow(),
        "accepted_reports": quality["accepted_reports"],
        "suspect_reports": quality["suspect_reports"],
        "valid_reports": quality["valid_reports"],
        "duplicate_reports": quality["duplicate_reports"],
        "metrics": quality["metrics"],
        "quality_score": quality["quality_score"],
        "review_gate": quality["review_gate"],
    }
    return {
        "associated_count": quality["metrics"]["accepted_count"],
        "association": snapshot,
    }


def _validate_review(actor, entity, data, lookup):
    association = entity["data"].get("association")
    if not association:
        raise ValidationError("event must be associated before review")
    metrics = association.get("metrics") or {}
    valid_count = int(metrics.get("valid_count", 0))
    average_distance = _to_float(metrics.get("average_distance_km"))
    passes = (
        valid_count >= REVIEW_MIN_VALID_REPORTS
        and average_distance <= REVIEW_MAX_AVERAGE_DISTANCE_KM
    )
    extra = {
        "review_quality": {
            "passes_gate": passes,
            "valid_count": valid_count,
            "average_distance_km": average_distance,
            "quality_score": association.get("quality_score"),
        }
    }
    if not passes:
        reason = str(data.get("override_reason") or "").strip()
        if actor.role != "admin":
            raise ValidationError(
                "review blocked by quality gate: %d valid report(s) (need %d), "
                "average distance %.2f km (limit %.1f); admin override required"
                % (
                    valid_count,
                    REVIEW_MIN_VALID_REPORTS,
                    average_distance,
                    REVIEW_MAX_AVERAGE_DISTANCE_KM,
                )
            )
        if not reason:
            raise ValidationError(
                "review blocked by quality gate; admin must provide override_reason "
                "with the basis for release"
            )
        extra["review_override"] = {
            "reason": reason,
            "actor_id": actor.user_id,
            "valid_count": valid_count,
            "average_distance_km": average_distance,
            "recorded_at": _utcnow(),
        }
    return extra


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


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event}
CUSTOM_TRANSITIONS = {
    ('event', 'associate'): _validate_associate,
    ('event', 'review'): _validate_review,
}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'withdraw': (('published', 'revised'), 'withdrawn')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer')}

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
            custom(actor, data, lookup)
        return dict(data)

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


# 列表三大分区：候选 / 待复核 / 已发布
EVENT_STAGES = (
    ("candidate", "候选", ("candidate",)),
    ("review", "待复核", ("associated", "reviewed")),
    ("published", "已发布", ("published", "revised", "withdrawn")),
)
STATUS_LABELS = {
    "candidate": "候选",
    "associated": "待复核",
    "reviewed": "复核通过待发布",
    "published": "已发布",
    "revised": "已修订",
    "withdrawn": "已撤回",
    "online": "在线",
    "offline": "离线",
}


def event_stage(status):
    for stage, _label, statuses in EVENT_STAGES:
        if status in statuses:
            return stage
    return "candidate"


def event_summary(entity):
    """列表项/详情使用的摘要。关联冻结后从固化快照读取。"""
    data = entity.get("data", {})
    association = data.get("association") or {}
    metrics = association.get("metrics") or {}
    gate = association.get("review_gate") or {}
    report_count = len(data.get("reports") or [])
    summary = {
        "title": data.get("title"),
        "origin_time": data.get("origin_time"),
        "location": data.get("location"),
        "report_count": report_count,
        "status_label": STATUS_LABELS.get(entity.get("status"), entity.get("status")),
        "stage": event_stage(entity.get("status"))
        if entity.get("kind") == "event"
        else None,
    }
    if association:
        summary.update(
            {
                "associated": True,
                "quality_score": association.get("quality_score"),
                "valid_count": metrics.get("valid_count"),
                "suspect_count": metrics.get("suspect_count"),
                "duplicate_count": metrics.get("duplicate_count"),
                "average_distance_km": metrics.get("average_distance_km"),
                "gate_passes": gate.get("passes"),
                "frozen_version": association.get("frozen_version"),
            }
        )
    else:
        summary["associated"] = False
    if entity.get("kind") == "event":
        summary["magnitude"] = data.get("magnitude")
        summary["reviewer"] = data.get("reviewer")
    override = data.get("review_override")
    if override:
        summary["override_reason"] = override.get("reason")
    return summary
