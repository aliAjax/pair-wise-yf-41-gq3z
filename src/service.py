from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine, event_summary


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
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        self._ensure_frozen_association(entity, action, patch)
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(
            entity_id, expected, next_status, merged, actor.user_id
        )
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch, "version": updated["version"]},
        )
        return updated

    @staticmethod
    def _ensure_frozen_association(entity, action, patch):
        # 关联动作固化：关联之后任何动作都不能改写已采用/存疑报文、质量分与口径
        if entity["kind"] != "event":
            return
        frozen = entity["data"].get("association")
        if not frozen:
            return
        if action == "associate":
            raise ValidationError("association is frozen and cannot be recomputed")
        incoming = patch.get("association")
        if incoming is not None and incoming != frozen:
            raise ValidationError("association snapshot is immutable after freezing")

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def detail(self, entity_id):
        entity = self.get(entity_id)
        detail = dict(entity)
        detail["summary"] = event_summary(entity)
        detail["versions"] = self.repository.list_versions(entity_id)
        return detail

    def versions(self, entity_id):
        if not self.repository.get_entity(entity_id):
            raise NotFoundError("entity not found: " + entity_id)
        return self.repository.list_versions(entity_id)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
