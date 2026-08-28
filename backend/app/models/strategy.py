from sqlalchemy import Column, String, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseModel

class Strategy(BaseModel):
    __tablename__ = "strategies"

    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    dsl_definition = Column(JSON, nullable=False) # JSON DSL with conditions, indicators, actions
    
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)

    tenant = relationship("Tenant", back_populates="strategies")
    user = relationship("User", back_populates="strategies")
    runs = relationship("StrategyRun", back_populates="strategy", cascade="all, delete-orphan")
