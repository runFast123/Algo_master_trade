from sqlalchemy import JSON, Column, ForeignKey, String
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class StrategyRun(BaseModel):
    __tablename__ = "strategy_runs"

    strategy_id = Column(String(36), ForeignKey("strategies.id"), nullable=False)
    run_type = Column(String(50), nullable=False)   # BACKTEST, PAPER, LIVE
    # PENDING, RUNNING, COMPLETED, FAILED, STOPPED
    status = Column(String(50), default="PENDING", nullable=False)

    params = Column(JSON, nullable=True)
    metrics = Column(JSON, nullable=True)
    logs = Column(JSON, nullable=True)

    # Where the bars came from: CHOICE_OPENAPI or SANDBOX_SYNTHETIC. Recorded
    # so a result computed from generated prices can never be mistaken for one
    # computed from exchange data.
    data_source = Column(String(50), nullable=True)

    strategy = relationship("Strategy", back_populates="runs")
    orders = relationship("Order", back_populates="strategy_run")
