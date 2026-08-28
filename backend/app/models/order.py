from sqlalchemy import Column, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class Order(BaseModel):
    """An order this platform submitted, recorded whether or not it succeeded.

    Every submission attempt is written here before and after the broker call,
    so the order book reflects reality and there is a trail to reconcile
    against Choice. Manual orders have no strategy run, hence the nullable FK.
    """

    __tablename__ = "orders"

    strategy_run_id = Column(
        String(36), ForeignKey("strategy_runs.id"), nullable=True, index=True
    )
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)

    client_order_no = Column(String(100), index=True, nullable=True)
    exchange_order_no = Column(String(100), nullable=True)
    symbol = Column(String(100), nullable=False)
    segment_id = Column(Integer, nullable=False)
    token = Column(String(50), nullable=False)
    side = Column(String(10), nullable=False)          # BUY, SELL
    order_type = Column(String(20), nullable=False)    # RL_MKT, RL_LIMIT, ...
    product_type = Column(String(20), default="CNC", nullable=False)
    quantity = Column(Integer, nullable=False)
    price_in_paisa = Column(Integer, default=0, nullable=False)
    executed_price = Column(Float, default=0.0, nullable=False)

    # SUBMITTED, ACCEPTED, REJECTED, FAILED, SIMULATED
    status = Column(String(50), default="SUBMITTED", nullable=False, index=True)
    # LIVE, DEMO, PAPER - which channel actually handled it.
    execution_mode = Column(String(20), default="LIVE", nullable=False)
    source = Column(String(20), default="MANUAL", nullable=False)  # MANUAL, STRATEGY
    failure_reason = Column(Text, nullable=True)

    strategy_run = relationship("StrategyRun", back_populates="orders")
