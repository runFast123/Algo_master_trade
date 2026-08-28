from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VALID_SIDES = {"BUY", "SELL"}
VALID_ORDER_TYPES = {"RL_MKT", "RL_LIMIT", "SL_MKT", "SL_LIMIT"}
VALID_PRODUCT_TYPES = {"CNC", "MIS", "NRML", "CO", "BO"}


class OrderRequest(BaseModel):
    """A new order.

    Prices are integer paisa so nothing is lost to float rounding in transit.
    Bounds here are the first of two checks; the engine applies notional and
    rate limits before anything reaches Choice.
    """

    segment_id: int = Field(ge=1, le=99, description="1=NSE cash, 2=NSE F&O, 3=BSE cash")
    token: str = Field(min_length=1, max_length=32)
    symbol: Optional[str] = Field(default=None, max_length=100)
    side: str
    order_type: str
    product_type: str = "CNC"
    quantity: int = Field(gt=0, le=100000)
    price_in_paisa: int = Field(default=0, ge=0, le=100_000_000)
    trigger_price_in_paisa: int = Field(default=0, ge=0, le=100_000_000)
    disclosed_quantity: int = Field(default=0, ge=0, le=100000)
    validity: int = Field(default=1, ge=1, le=5, description="1=Day, 2=IOC")

    @field_validator("side")
    @classmethod
    def _check_side(cls, value: str) -> str:
        upper = value.strip().upper()
        if upper not in VALID_SIDES:
            raise ValueError(f"side must be one of {sorted(VALID_SIDES)}")
        return upper

    @field_validator("order_type")
    @classmethod
    def _check_order_type(cls, value: str) -> str:
        upper = value.strip().upper()
        if upper not in VALID_ORDER_TYPES:
            raise ValueError(f"order_type must be one of {sorted(VALID_ORDER_TYPES)}")
        return upper

    @field_validator("product_type")
    @classmethod
    def _check_product_type(cls, value: str) -> str:
        upper = value.strip().upper()
        if upper not in VALID_PRODUCT_TYPES:
            raise ValueError(f"product_type must be one of {sorted(VALID_PRODUCT_TYPES)}")
        return upper

    @field_validator("token")
    @classmethod
    def _check_token(cls, value: str) -> str:
        token = value.strip()
        if not token.isdigit() or int(token) <= 0:
            raise ValueError("token must be a positive integer instrument token")
        return token


class OrderPreviewRequest(BaseModel):
    """A costing question, not an order.

    Deliberately looser than :class:`OrderRequest`: a preview is asked while
    the form is still being filled in, and refusing to price a half-typed
    order would make the preview useless exactly when it is most wanted.
    Nothing here can reach the broker.
    """

    side: str = "BUY"
    quantity: int = Field(default=0, ge=0, le=100000)
    price_in_paisa: int = Field(default=0, ge=0, le=100_000_000)
    price: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    # Intraday and delivery are charged under different schedules, so a preview
    # that ignores the product quotes the wrong number and a break-even price
    # that cannot be reached.
    product_type: str = "CNC"

    @field_validator("side")
    @classmethod
    def _check_side(cls, value: str) -> str:
        upper = str(value).strip().upper()
        if upper not in VALID_SIDES:
            raise ValueError(f"side must be one of {sorted(VALID_SIDES)}")
        return upper


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    strategy_run_id: Optional[str] = None
    client_order_no: Optional[str] = None
    exchange_order_no: Optional[str] = None
    symbol: str
    segment_id: int
    token: str
    side: str
    order_type: str
    product_type: str
    quantity: int
    price_in_paisa: int
    executed_price: float
    status: str
    execution_mode: str
    source: str
    failure_reason: Optional[str] = None
    created_at: datetime


class OrderPlacedResponse(BaseModel):
    status: str
    mode: Optional[str] = None
    order_id: str
    broker_order_id: Optional[str] = None
    message: Optional[str] = None
    order: OrderResponse
    # Simulated fills only: the resulting paper position and running P&L.
    position: Optional[Dict[str, Any]] = None
    paper: Optional[Dict[str, Any]] = None


class OrderModifyRequest(OrderRequest):
    """An amendment to a working order.

    Inherits every bound from :class:`OrderRequest` on purpose: raising the
    quantity or the limit price of a live order increases exposure exactly as
    a new order would, so it is validated exactly as strictly. The three
    identifiers below are what Choice needs to find the order to amend, and
    all three come from the order book.
    """

    client_order_no: int = Field(ge=0)
    exchange_order_no: str = Field(default="", max_length=64)
    gateway_order_no: str = Field(default="", max_length=64)
