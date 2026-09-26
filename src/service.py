from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


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
        if action != "associate":
            # 关联快照在 associate 时固化，后续补报、复核、修订都改不动
            patch.pop("association", None)
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(
            entity_id, expected, next_status, merged, actor_id=actor.user_id
        )
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def entity_detail(self, entity_id):
        entity = self.get(entity_id)
        detail = dict(entity)
        detail["stage"] = self.rules.stage_of(entity["status"])
        if entity["kind"] == "event":
            detail["summary"] = self.rules.event_summary(entity)
        detail["versions"] = self.repository.list_versions(entity_id)
        return detail

    def list(self, kind=None, status=None, stage=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        statuses = self.rules.stage_statuses(stage) if stage else None
        items = self.repository.list_entities(kind=kind, status=status, statuses=statuses)
        for entity in items:
            entity["stage"] = self.rules.stage_of(entity["status"])
        return items

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
