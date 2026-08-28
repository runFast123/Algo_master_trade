from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    # Segment ids per Live Data Feed Specification Annexure A:
    # 1 = NSE cash, 2 = NSE derivatives, 3 = BSE cash, 5 = MCX derivatives.
    segment_id: int = Field(default=1, ge=1, le=99)
    token: Optional[str] = Field(default=None, max_length=32)
    timeframe: str = Field(default="1d")
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial_capital: float = Field(default=100000.0, gt=0, le=1_000_000_000)

    @field_validator("timeframe")
    @classmethod
    def _check_timeframe(cls, value: str) -> str:
        allowed = {"1m", "3m", "5m", "15m", "30m", "1h", "1d", "1w"}
        lowered = value.strip().lower()
        if lowered not in allowed:
            raise ValueError(f"timeframe must be one of {sorted(allowed)}")
        return lowered

    @field_validator("end_date")
    @classmethod
    def _check_range(cls, value: str, info) -> str:
        start = (info.data or {}).get("start_date")
        if start and value < start:
            raise ValueError("end_date must be on or after start_date")
        return value


class StrategyRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    strategy_id: str
    run_type: str
    status: str
    data_source: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None
    logs: Optional[List[Any]] = None
    created_at: datetime
    updated_at: datetime


class PaperRunRequest(BaseModel):
    """Start a paper run.

    Bounded like an order request: a run places orders, so the same discipline
    applies. Timeframe is validated against what the scheduler can aggregate,
    rather than accepted and failed later.
    """

    symbol: str = Field(min_length=1, max_length=100)
    segment_id: Optional[int] = Field(default=None, ge=1, le=99)
    token: Optional[str] = Field(default=None, max_length=32)
    timeframe: str = Field(default="1m")

    @field_validator("timeframe")
    @classmethod
    def _check_timeframe(cls, value: str) -> str:
        from engine.app.strategy_engine.scheduler import BAR_SECONDS

        key = str(value).strip().lower()
        if key not in BAR_SECONDS:
            raise ValueError(
                f"timeframe must be one of {', '.join(sorted(BAR_SECONDS))}"
            )
        return key

    @field_validator("token")
    @classmethod
    def _check_token(cls, value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return None
        token = str(value).strip()
        if not token.isdigit() or int(token) <= 0:
            raise ValueError("token must be a positive integer instrument token")
        return token
