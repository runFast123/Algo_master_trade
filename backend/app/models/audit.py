from sqlalchemy import Column, String, JSON
from app.models.base import BaseModel

class AuditLog(BaseModel):
    __tablename__ = "audit_logs"

    actor_id = Column(String(36), nullable=False)
    tenant_id = Column(String(36), nullable=False)
    action = Column(String(100), nullable=False)
    entity_type = Column(String(100), nullable=False)
    entity_id = Column(String(36), nullable=True)
    details = Column(JSON, nullable=True)
