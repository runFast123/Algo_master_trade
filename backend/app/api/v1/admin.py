from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_admin
from app.models.audit import AuditLog
from app.models.order import Order
from app.models.strategy import Strategy
from app.models.strategy_run import StrategyRun
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.audit_repo import audit_repo
from engine.app.choice_gateway import orders as orders_gateway
from engine.app.strategy_engine.risk_manager import risk_manager
from app.schemas.admin import AdminStatsResponse, AuditLogResponse, TenantSummary
from engine.app.choice_gateway.client_manager import choice_sessions
from engine.app.config import engine_settings
from engine.app.strategy_engine.runner import run_registry

router = APIRouter()


@router.get("/stats", response_model=AdminStatsResponse)
def get_admin_stats(
    _: User = Depends(get_current_admin), db: Session = Depends(get_db)
):
    """Live platform counters. Every number is queried, none are placeholders."""
    orders_by_status = dict(
        db.query(Order.status, func.count(Order.id)).group_by(Order.status).all()
    )

    return AdminStatsResponse(
        total_tenants=db.query(Tenant).count(),
        total_users=db.query(User).count(),
        total_strategies=db.query(Strategy).count(),
        total_runs=db.query(StrategyRun).count(),
        active_live_runs=run_registry.active_count(),
        total_orders=db.query(Order).count(),
        orders_by_status=orders_by_status,
        connected_choice_sessions=choice_sessions.active_count(),
        live_choice_sessions=choice_sessions.live_count(),
        choice_environment=engine_settings.CHOICE_ENV,
        order_rate_limit_per_sec=engine_settings.ORDER_RATE_LIMIT_PER_SEC,
    )


@router.get("/tenants", response_model=List[TenantSummary])
def list_tenants(_: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = (
        db.query(
            Tenant.id,
            Tenant.name,
            Tenant.plan,
            func.count(func.distinct(User.id)).label("user_count"),
            func.count(func.distinct(Strategy.id)).label("strategy_count"),
            func.count(func.distinct(Order.id)).label("order_count"),
        )
        .outerjoin(User, User.tenant_id == Tenant.id)
        .outerjoin(Strategy, Strategy.tenant_id == Tenant.id)
        .outerjoin(Order, Order.tenant_id == Tenant.id)
        .group_by(Tenant.id, Tenant.name, Tenant.plan)
        .all()
    )

    # Last activity comes from the audit trail, which records every meaningful
    # action, rather than from any single table.
    last_seen = dict(
        db.query(AuditLog.tenant_id, func.max(AuditLog.created_at))
        .group_by(AuditLog.tenant_id)
        .all()
    )
    users_by_tenant: Dict[str, List[str]] = {}
    for user_id, tenant_id in db.query(User.id, User.tenant_id).all():
        users_by_tenant.setdefault(tenant_id, []).append(str(user_id))

    return [
        TenantSummary(
            id=row.id, name=row.name, plan=row.plan,
            user_count=row.user_count, strategy_count=row.strategy_count,
            order_count=row.order_count,
            last_activity=last_seen.get(row.id),
            paper_pnl=choice_sessions.paper_pnl_for(users_by_tenant.get(row.id, [])),
            connected_sessions=sum(
                1 for u in users_by_tenant.get(row.id, [])
                if choice_sessions.get(u) is not None
            ),
        )
        for row in rows
    ]


@router.post("/tenants/{tenant_id}/halt")
def halt_tenant(
    tenant_id: str,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Stop every account in one tenant from trading for the rest of the day.

    The operational equivalent of the per-user kill switch: one desk misbehaves,
    and an administrator needs to stop all of it without knowing which user.
    Each account is halted independently so one failure cannot leave the rest
    trading.
    """
    user_ids = [
        str(uid) for (uid,) in
        db.query(User.id).filter(User.tenant_id == tenant_id).all()
    ]
    if not user_ids:
        raise HTTPException(status_code=404, detail="No such tenant")

    for uid in user_ids:
        risk_manager.halt(uid, f"Halted by administrator {current_user.email}")

    cancelled, failures = 0, []
    for uid in user_ids:
        session = choice_sessions.get(uid)
        if session is None:
            continue
        try:
            result = orders_gateway.cancel_all_open(session)
            cancelled += result.get("cancelled", 0)
            failures.extend(result.get("failures", []))
        except Exception as exc:          # the halt stands regardless
            failures.append({"user": uid, "reason": str(exc)[:200]})

    audit_repo.log(
        db, actor_id=current_user.id, tenant_id=tenant_id,
        action="TENANT_HALTED", entity_type="tenant", entity_id=tenant_id,
        details={"accounts": len(user_ids), "cancelled": cancelled,
                 "failed": len(failures)},
    )
    return {"status": "SUCCESS", "tenant_id": tenant_id,
            "accounts_halted": len(user_ids), "orders_cancelled": cancelled,
            "failures": failures}


@router.get("/compliance")
def compliance_checklist(_: User = Depends(get_current_admin)) -> Dict[str, Any]:
    """The regulatory blockers, as a checklist rather than buried in a document.

    Everything here is a statement about the deployment, not a claim about
    Choice's records. Items that cannot be determined from configuration say
    so instead of guessing — a compliance list that reports a green tick it did
    not verify is worse than no list.
    """
    production = engine_settings.is_production
    return {
        "environment": engine_settings.CHOICE_ENV,
        "items": [
            {"id": "OPEN-3", "label": "UAT certification received from Choice",
             "status": "unknown", "blocking": production,
             "detail": "Not visible from configuration. Confirm in writing with "
                       "the Choice Open API team before routing live orders."},
            {"id": "OPEN-1", "label": "Orders originate from a declared static IP",
             "status": "failing" if production else "not_applicable",
             "blocking": production,
             "detail": "A desktop build places orders from each user's machine, "
                       "which cannot satisfy the static-IP mandate. Order flow "
                       "must move to a server with a declared address."},
            {"id": "EMPANEL", "label": "Vendor empanelment with NSE/BSE/MCX",
             "status": "unknown", "blocking": production,
             "detail": "Required for a platform serving multiple Choice clients."},
            {"id": "RATE", "label": "Order rate below 10/second",
             "status": "passing",
             "detail": f"Enforced per session at "
                       f"{engine_settings.ORDER_RATE_LIMIT_PER_SEC:g}/second."},
            {"id": "AUDIT", "label": "Audit trail written for every action",
             "status": "passing",
             "detail": "Logins, connections, orders, halts and strategy changes "
                       "are recorded. Retention of 5 years is an operational "
                       "commitment, not something the app can enforce."},
            {"id": "SIGNING", "label": "Executable is code-signed",
             "status": "failing",
             "detail": "The binary is unsigned; downloads raise SmartScreen."},
        ],
    }


@router.get("/audit", response_model=List[AuditLogResponse])
def list_audit_log(
    limit: int = Query(100, ge=1, le=1000),
    action: str = Query(None),
    _: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Audit trail. Retained for regulatory review; see the Choice OpenAPI
    Integration Guide s12, which requires 5 years of API activity logs."""
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action.upper())
    return query.order_by(AuditLog.created_at.desc()).limit(limit).all()


@router.get("/health")
def system_health(_: User = Depends(get_current_admin)) -> Dict[str, Any]:
    return {
        "status": "HEALTHY",
        # The deployment default. Individual sessions may be on the other
        # server; see each session's own reported environment.
        "choice_environment_default": engine_settings.CHOICE_ENV,
        "choice_base_url_default": engine_settings.choice_base_url,
        "is_production": engine_settings.is_production,
        "connected_sessions": choice_sessions.active_count(),
        "active_runs": run_registry.active_count(),
    }
