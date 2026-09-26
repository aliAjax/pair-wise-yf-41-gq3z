from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 关联质量关口口径
SUSPICIOUS_TIME_OFFSET = 120.0   # 时间偏移绝对值超过 120 秒 -> 存疑
SUSPICIOUS_DISTANCE_KM = 3.0     # 距离超过 3 公里 -> 存疑
MIN_VALID_REPORTS = 3            # 复核至少需要 3 条有效报文
MAX_AVG_DISTANCE_KM = 2.0        # 有效报文平均距离超过 2 公里 -> 拦截复核
# 质量分扣分口径
SUSPICIOUS_PENALTY = 15.0        # 每条存疑报文扣分
AVG_DISTANCE_PENALTY = 10.0      # 平均距离每公里扣分
MISSING_REPORT_PENALTY = 15.0    # 每条有效报文缺额（距 3 条）扣分


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")


def _coerce_report(raw, index):
    if not isinstance(raw, dict):
        raise ValidationError("report #%s must be an object" % index)
    try:
        time_offset = float(raw.get("time_offset", 0))
        distance_km = float(raw.get("distance_km", 0))
    except (TypeError, ValueError):
        raise ValidationError("report time_offset/distance_km must be numeric")
    report = dict(raw)
    report["time_offset"] = time_offset
    report["distance_km"] = distance_km
    return report


def evaluate_reports(
    reports,
    max_delta=SUSPICIOUS_TIME_OFFSET,
    max_distance=SUSPICIOUS_DISTANCE_KM,
    min_valid=MIN_VALID_REPORTS,
    max_avg_distance=MAX_AVG_DISTANCE_KM,
):
    """关联质量关口：同台站去重、存疑标记、质量分与复核拦截结论。

    结果是一份与输入无关的独立快照，调用方可直接固化。
    """
    normalized = [(index, _coerce_report(raw, index)) for index, raw in enumerate(reports)]

    # 同一台站重复上报，只保留时间偏移绝对值更小的一份（再以距离、原始顺序决胜）
    best = {}
    duplicate_reports = []
    for index, report in normalized:
        station = report.get("station")
        key = ("station", station) if station not in (None, "") else ("index", index)
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = (index, report)
            continue
        kept_index, kept = incumbent
        candidate_key = (abs(report["time_offset"]), report["distance_km"], index)
        kept_key = (abs(kept["time_offset"]), kept["distance_km"], kept_index)
        if candidate_key < kept_key:
            dropped = dict(kept)
            dropped["drop_reason"] = "duplicate_station"
            duplicate_reports.append(dropped)
            best[key] = (index, report)
        else:
            dropped = dict(report)
            dropped["drop_reason"] = "duplicate_station"
            duplicate_reports.append(dropped)

    unique_reports = [report for _, report in sorted(best.values(), key=lambda item: item[0])]
    adopted_reports = []
    suspicious_reports = []
    for report in unique_reports:
        if abs(report["time_offset"]) > max_delta or report["distance_km"] > max_distance:
            suspicious_reports.append(report)
        else:
            adopted_reports.append(report)

    valid_count = len(adopted_reports)
    if adopted_reports:
        avg_distance = (
            sum(report["distance_km"] for report in adopted_reports) / valid_count
        )
    else:
        avg_distance = None

    review_blocked = valid_count < min_valid or (
        avg_distance is not None and avg_distance > max_avg_distance
    )

    score = 100.0
    score -= SUSPICIOUS_PENALTY * len(suspicious_reports)
    score -= AVG_DISTANCE_PENALTY * (avg_distance or 0.0)
    score -= MISSING_REPORT_PENALTY * max(0, min_valid - valid_count)
    quality_score = round(min(100.0, max(0.0, score)), 1)

    return {
        "adopted_reports": adopted_reports,
        "suspicious_reports": suspicious_reports,
        "duplicate_reports": duplicate_reports,
        "valid_count": valid_count,
        "suspicious_count": len(suspicious_reports),
        "duplicate_count": len(duplicate_reports),
        "avg_distance_km": round(avg_distance, 2) if avg_distance is not None else None,
        "quality_score": quality_score,
        "review_blocked": review_blocked,
        "thresholds": {
            "time_offset_seconds": max_delta,
            "distance_km": max_distance,
            "min_valid_reports": min_valid,
            "max_avg_distance_km": max_avg_distance,
        },
    }


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    # 关联时一次性固化采用/存疑/重复报文、质量分与版本，之后补报或修订都改不动
    snapshot = evaluate_reports(reports)
    snapshot["version"] = entity["version"] + 1
    snapshot["associated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "association": snapshot,
        "associated_count": snapshot["valid_count"],
    }


def _validate_supplement(actor, entity, data, lookup):
    new_reports = data.get("reports")
    if not isinstance(new_reports, list) or not new_reports:
        raise ValidationError("supplement requires a non-empty reports list")
    combined = [dict(report) for report in (entity["data"].get("reports") or [])]
    for report in new_reports:
        if isinstance(report, dict):
            combined.append(dict(report))
    return {"reports": combined, "report_count": len(combined)}


def _validate_review(actor, entity, data, lookup):
    snapshot = entity["data"].get("association")
    if snapshot is None:
        # 兼容未经关联动作的历史数据
        snapshot = evaluate_reports(entity["data"].get("reports") or [])
    extra = {}
    if snapshot.get("review_blocked"):
        if actor.role != "admin":
            raise PermissionDenied(
                "quality gate blocks review (valid_reports=%s, avg_distance_km=%s); "
                "admin override with override_reason is required"
                % (snapshot.get("valid_count"), snapshot.get("avg_distance_km"))
            )
        reason = data.get("override_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError(
                "override_reason is required when the quality gate blocks review"
            )
        extra["quality_override"] = {
            "reason": reason.strip(),
            "valid_count": snapshot.get("valid_count"),
            "avg_distance_km": snapshot.get("avg_distance_km"),
            "quality_score": snapshot.get("quality_score"),
        }
    return extra


# 列表口径：候选 / 待复核（含已复核待发布）/ 已发布（含修订）
STAGE_STATUSES = {
    'candidate': ('candidate',),
    'review': ('associated', 'reviewed'),
    'published': ('published', 'revised'),
}
STAGE_LABELS = {'candidate': '候选', 'review': '待复核', 'published': '已发布'}
STATUS_STAGE = {
    status: stage
    for stage, statuses in STAGE_STATUSES.items()
    for status in statuses
}
STATUS_LABELS = {
    'candidate': '候选',
    'associated': '待复核',
    'reviewed': '已复核',
    'published': '已发布',
    'revised': '已修订',
    'withdrawn': '已撤回',
}


def associate_reports(reports, max_delta=120, max_distance=3.0):
    return evaluate_reports(
        reports, max_delta=max_delta, max_distance=max_distance
    )["adopted_reports"]


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def event_summary(entity):
    """列表/详情使用的事件摘要（只读派生，不回写实体）。"""
    data = entity.get("data") or {}
    summary = {
        "id": entity["id"],
        "title": data.get("title"),
        "status": entity["status"],
        "stage": STATUS_STAGE.get(entity["status"]),
        "version": entity["version"],
        "report_count": len(data.get("reports") or []),
        "magnitude": data.get("magnitude"),
        "reviewer": data.get("reviewer"),
        "communication_id": data.get("communication_id"),
    }
    snapshot = data.get("association")
    if snapshot:
        summary["association"] = {
            "frozen_version": snapshot.get("version"),
            "quality_score": snapshot.get("quality_score"),
            "valid_count": snapshot.get("valid_count"),
            "suspicious_count": snapshot.get("suspicious_count"),
            "duplicate_count": snapshot.get("duplicate_count"),
            "avg_distance_km": snapshot.get("avg_distance_km"),
            "review_blocked": snapshot.get("review_blocked"),
            "associated_at": snapshot.get("associated_at"),
        }
    override = data.get("quality_override")
    if override:
        summary["quality_override"] = override
    return summary


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event}
CUSTOM_TRANSITIONS = {
    ('event', 'associate'): _validate_associate,
    ('event', 'supplement'): _validate_supplement,
    ('event', 'review'): _validate_review,
}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'supplement': (('candidate',), 'candidate'), 'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'withdraw': (('published', 'revised'), 'withdrawn')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'supplement'): ('reports',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'supplement': ('admin', 'analyst'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer')}

    # 列表口径：候选 / 待复核（含已复核待发布）/ 已发布（含修订）
    STAGE_STATUSES = STAGE_STATUSES
    STAGE_LABELS = STAGE_LABELS
    STATUS_STAGE = STATUS_STAGE
    STATUS_LABELS = STATUS_LABELS

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def stage_statuses(self, stage):
        statuses = self.STAGE_STATUSES.get(stage)
        if not statuses:
            raise ValidationError("unknown stage: " + str(stage))
        return statuses

    def stage_of(self, status):
        return self.STATUS_STAGE.get(status)

    def event_summary(self, entity):
        return event_summary(entity)

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
