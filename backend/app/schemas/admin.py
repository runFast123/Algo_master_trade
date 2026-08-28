from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict


class AdminStatsResponse(BaseModel):
    total_tenants: int
    total_users: int
    total_strategies: int
    total_runs: int
    active_live_runs: int
    total_orders: int
    orders_by_status: Dict[str, int]
    connected_choice_sessions: int
    live_choice_sessions: int
    choice_environment: str
    order_rate_limit_per_sec: float


class TenantSummary(BaseModel):
    id: str
    name: str
    plan: str
    user_count: int
    strategy_count: int
    order_count: int = 0
    last_activity: Optional[datetime] = None
    # Live sessions only — paper fills are not persisted, so this is not a
    # historical total and is labelled that way in the interface.
    paper_pnl: float = 0.0
    connected_sessions: int = 0


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_id: str
    tenant_id: str
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    created_at: datetime
