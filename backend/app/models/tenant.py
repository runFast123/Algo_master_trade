from sqlalchemy import Column, String, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseModel

class Tenant(BaseModel):
    __tablename__ = "tenants"

    name = Column(String(100), nullable=False)
    plan = Column(String(50), default="STANDARD", nullable=False)
    settings = Column(JSON, nullable=True)

    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    strategies = relationship("Strategy", back_populates="tenant", cascade="all, delete-orphan")
