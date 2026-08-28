from sqlalchemy import Column, String, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.models.base import BaseModel

class User(BaseModel):
    __tablename__ = "users"

    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    role = Column(String(50), default="trader", nullable=False) # admin, trader
    is_active = Column(Boolean, default=True, nullable=False)
    
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    
    tenant = relationship("Tenant", back_populates="users")
    strategies = relationship("Strategy", back_populates="user", cascade="all, delete-orphan")
