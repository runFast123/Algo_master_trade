from fastapi import APIRouter
from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.strategies import router as strategies_router
from app.api.v1.orders import router as orders_router
from app.api.v1.portfolio import router as portfolio_router
from app.api.v1.market import router as market_router
from app.api.v1.admin import router as admin_router
from app.api.v1.diagnostics import router as diagnostics_router
from app.api.v1.system import router as system_router

api_router = APIRouter()

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(strategies_router, prefix="/strategies", tags=["strategies"])
api_router.include_router(orders_router, prefix="/orders", tags=["orders"])
api_router.include_router(portfolio_router, prefix="/portfolio", tags=["portfolio"])
api_router.include_router(market_router, prefix="/market", tags=["market"])
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])
api_router.include_router(diagnostics_router, prefix="/diagnostics", tags=["diagnostics"])
api_router.include_router(system_router, prefix="/system", tags=["system"])
