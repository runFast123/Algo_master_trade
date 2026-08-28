from sqlalchemy.orm import Session
from app.models.audit import AuditLog

class AuditRepository:
    @staticmethod
    def log(db: Session, actor_id: str, tenant_id: str, action: str, entity_type: str, entity_id: str = None, details: dict = None) -> AuditLog:
        entry = AuditLog(
            actor_id=actor_id,
            tenant_id=tenant_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

audit_repo = AuditRepository()
