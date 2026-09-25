from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


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


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def result_release_checks(merged, lookup, as_of=None):
    """发布前核对：校准有效期、方法覆盖范围、本次测量不确定度。

    返回不合规原因列表；空列表表示可以发布。
    """
    as_of = as_of or _today()
    reasons = []
    instrument = _find_one(lookup, "instrument", "id", merged.get("instrument_id"))
    calibration = _find_one(lookup, "calibration", "id", merged.get("calibration_id"))
    method = _find_one(lookup, "method", "id", merged.get("method_id"))

    if not instrument:
        reasons.append("instrument record not found")
    elif instrument["status"] != "active":
        reasons.append("instrument is not active")

    if not calibration:
        reasons.append("calibration record not found")
    else:
        if instrument and calibration["data"].get("instrument_id") != instrument["id"]:
            reasons.append("calibration does not belong to this instrument")
        if calibration["status"] != "approved":
            reasons.append("calibration is not approved")
        if calibration["data"].get("result") != "passed":
            reasons.append("calibration result is not passed")
        due_at = calibration["data"].get("due_at", "")
        if not due_at:
            reasons.append("calibration has no due date")
        elif not calibration_current(due_at, as_of):
            reasons.append("calibration is expired")

    if not method:
        reasons.append("method record not found")
    elif method["status"] != "validated":
        reasons.append("method is not validated or has been withdrawn")
    elif instrument:
        if instrument["id"] not in method["data"].get("instrument_ids", []):
            reasons.append("method does not cover this instrument")
        parameters = method["data"].get("parameters") or {}
        value = merged.get("value")
        span = parameters.get("range")
        if (
            isinstance(span, (list, tuple))
            and len(span) == 2
            and _is_number(value)
            and (value < span[0] or value > span[1])
        ):
            reasons.append("measurement is outside the method coverage range")

    uncertainty = merged.get("uncertainty")
    if not _is_number(uncertainty) or uncertainty <= 0:
        reasons.append("measurement uncertainty is missing or not positive")
    else:
        cal_uncertainty = calibration and calibration["data"].get("uncertainty")
        if _is_number(cal_uncertainty) and uncertainty < cal_uncertainty:
            reasons.append("measurement uncertainty is below the calibration uncertainty")
        max_uncertainty = method and (method["data"].get("parameters") or {}).get(
            "max_uncertainty"
        )
        if _is_number(max_uncertainty) and uncertainty > max_uncertainty:
            reasons.append("measurement uncertainty exceeds the method limit")
    return reasons


def _evaluate_binding(actor, entity, data, lookup, action):
    """核对一次发布/复核绑定，不合规则转入待复核；所有尝试按时间留痕。"""
    merged = dict(entity["data"])
    merged.update(data)
    reasons = result_release_checks(merged, lookup)
    history = list(entity["data"].get("release_history") or [])
    entry = {
        "at": _now(),
        "actor": actor.user_id,
        "action": action,
        "instrument_id": merged.get("instrument_id"),
        "calibration_id": merged.get("calibration_id"),
        "method_id": merged.get("method_id"),
        "value": merged.get("value"),
        "unit": merged.get("unit"),
        "uncertainty": merged.get("uncertainty"),
        "reasons": reasons,
        "outcome": "pending_review" if reasons else "released",
    }
    if data.get("disposition"):
        entry["disposition"] = data["disposition"]
    history.append(entry)
    patch = {
        "instrument_id": merged.get("instrument_id"),
        "calibration_id": merged.get("calibration_id"),
        "method_id": merged.get("method_id"),
        "value": merged.get("value"),
        "unit": merged.get("unit"),
        "uncertainty": merged.get("uncertainty"),
        "review_reasons": reasons,
        "release_history": history,
    }
    if data.get("disposition"):
        patch["disposition"] = data["disposition"]
    if reasons:
        patch["next_status"] = "pending_review"
    else:
        patch["released_by"] = actor.user_id
        patch["released_at"] = _now()
    return patch


def _validate_result_release(actor, entity, data, lookup):
    return _evaluate_binding(actor, entity, data, lookup, "release")


def _validate_result_review(actor, entity, data, lookup):
    return _evaluate_binding(actor, entity, data, lookup, "review")


CUSTOM_CREATE = {'calibration': _validate_calibration}
CUSTOM_TRANSITIONS = {('calibration', 'perform'): _validate_perform, ('result', 'release'): _validate_result_release, ('result', 'review'): _validate_result_review}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending',), 'released'), 'review': (('pending_review',), 'released'), 'block': (('pending', 'pending_review'), 'blocked'), 'reanalyze': (('blocked',), 'pending')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'calibration_id', 'method_id', 'value', 'unit'), ('result', 'review'): ('instrument_id', 'calibration_id', 'method_id', 'disposition'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst')}

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
        if extra and "next_status" in extra:
            next_status = extra.pop("next_status")
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
