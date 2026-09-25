from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _resolve_calibration(lookup, data, instrument, reasons):
    """确定用于核对的校准记录：显式指定的 calibration_id，否则取仪器最新已批准的校准。"""
    calibration_id = data.get("calibration_id")
    if calibration_id:
        record = _find_one(lookup, "calibration", "id", calibration_id)
        if not record:
            reasons.append("校准记录不存在: %s" % calibration_id)
            return None
        if record["data"].get("instrument_id") != instrument["id"]:
            reasons.append("校准记录 %s 不属于所引用的仪器" % calibration_id)
            return None
        if record["status"] != "approved":
            reasons.append("校准记录未获批准（当前状态: %s）" % record["status"])
            return None
        return record
    records = [
        row
        for row in (lookup("calibration", "instrument_id", instrument["id"]) or [])
        if row["status"] == "approved"
    ]
    if not records:
        reasons.append("仪器缺少已批准的校准记录")
        return None
    records.sort(key=lambda row: str(row["data"].get("due_at", "")))
    return records[-1]


def _release_checks(data, lookup):
    """发布前核对：校准有效期、方法覆盖范围、本次测量不确定度。

    返回 (不合规原因列表, 核对用的校准记录)。任一不合规项都会列入原因。
    """
    reasons = []
    calibration = None
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        reasons.append("引用的仪器不存在")
    elif instrument["status"] != "active":
        reasons.append("仪器当前状态不可用于放行: %s" % instrument["status"])
    if instrument:
        calibration = _resolve_calibration(lookup, data, instrument, reasons)
        if calibration:
            due_at = calibration["data"].get("due_at", "")
            if not due_at or not calibration_current(due_at, _today()):
                reasons.append("校准已超出有效期（due_at=%s）" % (due_at or "未记录"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not method:
        reasons.append("引用的方法不存在")
    else:
        if method["status"] != "validated":
            reasons.append("方法版本未处于有效状态（当前: %s）" % method["status"])
        if instrument and instrument["id"] not in method["data"].get("instrument_ids", []):
            reasons.append("方法覆盖范围不包含所引用的仪器")
        span = (method["data"].get("parameters") or {}).get("range")
        if span:
            try:
                value = float(data.get("value"))
                low, high = float(span[0]), float(span[1])
                if not low <= value <= high:
                    reasons.append(
                        "测量值 %s 超出方法覆盖范围 [%s, %s]"
                        % (data.get("value"), span[0], span[1])
                    )
            except (TypeError, ValueError, IndexError):
                reasons.append("测量值无法与方法覆盖范围比对")
    uncertainty = data.get("uncertainty")
    try:
        uncertainty_value = float(uncertainty)
        if uncertainty_value <= 0:
            raise ValueError
    except (TypeError, ValueError):
        reasons.append("未声明有效的本次测量不确定度")
        uncertainty_value = None
    if uncertainty_value is not None and calibration:
        try:
            floor = float(calibration["data"].get("uncertainty"))
        except (TypeError, ValueError):
            floor = None
        if floor is not None and uncertainty_value < floor:
            reasons.append(
                "本次测量不确定度 %s 小于校准不确定度 %s" % (uncertainty, floor)
            )
    return reasons, calibration


def _history_entry(actor, action, data, calibration, outcome, reasons, disposition=None):
    return {
        "at": _now_iso(),
        "by": actor.user_id,
        "role": actor.role,
        "action": action,
        "instrument_id": data.get("instrument_id"),
        "method_id": data.get("method_id"),
        "calibration_id": (calibration or {}).get("id") or data.get("calibration_id"),
        "value": data.get("value"),
        "unit": data.get("unit"),
        "uncertainty": data.get("uncertainty"),
        "outcome": outcome,
        "reasons": list(reasons),
        "disposition": disposition,
    }


def _release_outcome(actor, entity, action, data, lookup, disposition=None):
    reasons, calibration = _release_checks(data, lookup)
    outcome = "review" if reasons else "released"
    history = list(entity["data"].get("release_history", []))
    history.append(
        _history_entry(actor, action, data, calibration, outcome, reasons, disposition)
    )
    extra = {
        "_next_status": outcome,
        "release_history": history,
        "review_reasons": reasons,
    }
    if calibration:
        extra["calibration_id"] = calibration["id"]
    if disposition is not None:
        extra["disposition"] = disposition
        extra["resolved_by"] = actor.user_id
        extra["resolved_at"] = _now_iso()
    if not reasons:
        extra["released_by"] = actor.user_id
        extra["released_at"] = _now_iso()
    return extra


def _validate_result_release(actor, entity, data, lookup):
    return _release_outcome(actor, entity, "release", data, lookup)


def _validate_result_resolve(actor, entity, data, lookup):
    return _release_outcome(
        actor, entity, "resolve_review", data, lookup, disposition=data.get("disposition")
    )


CUSTOM_CREATE = {'calibration': _validate_calibration}
CUSTOM_TRANSITIONS = {('calibration', 'perform'): _validate_perform, ('result', 'release'): _validate_result_release, ('result', 'resolve_review'): _validate_result_resolve}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending',), 'released'), 'resolve_review': (('review',), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'), ('result', 'resolve_review'): ('instrument_id', 'method_id', 'value', 'unit', 'disposition'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'resolve_review': ('admin', 'authorizer'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst')}

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
            next_status = extra.pop("_next_status", next_status)
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
