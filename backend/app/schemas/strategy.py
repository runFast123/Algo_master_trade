from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, computed_field

from engine.app.strategy_engine.explain import explain

class StrategyBase(BaseModel):
    name: str
    description: Optional[str] = None
    dsl_definition: Dict[str, Any]

class StrategyCreate(StrategyBase):
    pass

class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    dsl_definition: Optional[Dict[str, Any]] = None

class StrategyResponse(StrategyBase):
    id: str
    tenant_id: str
    user_id: str
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def plain_english(self) -> str:
        """The DSL rendered as a sentence.

        Separate from ``description``, which is whatever the user typed. This
        one is derived from the definition, so it cannot drift from what the
        strategy actually does — which is the point of showing it.
        """
        return explain(self.dsl_definition)

    class Config:
        from_attributes = True
