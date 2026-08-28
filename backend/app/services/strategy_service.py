import logging
from typing import List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.strategy import Strategy
from app.models.strategy_run import StrategyRun
from app.models.user import User
from app.repositories.audit_repo import audit_repo
from app.schemas.strategy import StrategyCreate, StrategyUpdate
from app.schemas.strategy_run import BacktestRequest
from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceGatewayError
from engine.app.strategy_engine.backtest_runner import run_backtest
from engine.app.strategy_engine.dsl import dsl_engine

logger = logging.getLogger("strategies")


class StrategyService:
    @staticmethod
    def create_strategy(db: Session, user: User, req: StrategyCreate) -> Strategy:
        # Validate up front so a malformed strategy is rejected in the editor
        # rather than silently never trading.
        dsl_engine.validate(req.dsl_definition)

        strategy = Strategy(
            name=req.name,
            description=req.description,
            dsl_definition=req.dsl_definition,
            tenant_id=user.tenant_id,
            user_id=user.id,
        )
        db.add(strategy)
        db.commit()
        db.refresh(strategy)
        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id, action="STRATEGY_CREATED",
            entity_type="strategy", entity_id=strategy.id,
            details={"name": strategy.name},
        )
        return strategy

    @staticmethod
    def list_strategies(db: Session, user: User) -> List[Strategy]:
        return (
            db.query(Strategy)
            .filter(Strategy.tenant_id == user.tenant_id)
            .order_by(Strategy.created_at.desc())
            .all()
        )

    @staticmethod
    def get_strategy(db: Session, user: User, strategy_id: str) -> Strategy:
        strategy = (
            db.query(Strategy)
            .filter(Strategy.id == strategy_id, Strategy.tenant_id == user.tenant_id)
            .first()
        )
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")
        return strategy

    @staticmethod
    def update_strategy(
        db: Session, user: User, strategy_id: str, req: StrategyUpdate
    ) -> Strategy:
        strategy = StrategyService.get_strategy(db, user, strategy_id)
        if req.dsl_definition is not None:
            dsl_engine.validate(req.dsl_definition)
            strategy.dsl_definition = req.dsl_definition
        if req.name is not None:
            strategy.name = req.name
        if req.description is not None:
            strategy.description = req.description
        db.commit()
        db.refresh(strategy)
        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id, action="STRATEGY_UPDATED",
            entity_type="strategy", entity_id=strategy.id,
        )
        return strategy

    @staticmethod
    def delete_strategy(db: Session, user: User, strategy_id: str) -> None:
        strategy = StrategyService.get_strategy(db, user, strategy_id)
        db.delete(strategy)
        db.commit()
        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id, action="STRATEGY_DELETED",
            entity_type="strategy", entity_id=strategy_id,
        )

    @staticmethod
    def run_backtest(
        db: Session,
        user: User,
        session: ChoiceSession,
        strategy_id: str,
        req: BacktestRequest,
    ) -> StrategyRun:
        """Run a backtest and store the real result.

        If the run cannot complete, the run row is marked FAILED with the
        reason and the error is surfaced. There is no fallback that returns
        invented metrics.
        """
        strategy = StrategyService.get_strategy(db, user, strategy_id)

        run = StrategyRun(
            strategy_id=strategy.id,
            run_type="BACKTEST",
            status="RUNNING",
            params=req.model_dump(),
            logs=["Backtest started"],
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        try:
            result = run_backtest(
                session=session,
                dsl_def=strategy.dsl_definition,
                params=req.model_dump(),
            )
        except (ChoiceGatewayError, ValueError) as exc:
            run.status = "FAILED"
            run.logs = ["Backtest failed", str(exc)]
            run.metrics = None
            db.commit()
            db.refresh(run)
            logger.warning("Backtest %s failed: %s", run.id, exc)
            raise

        run.status = result["status"]
        run.metrics = result["metrics"]
        run.logs = result["logs"]
        run.data_source = result["data_source"].get("source")
        # The full provenance, not just the source name. It carries the window
        # actually covered, and a backtest that silently tested one year of a
        # three-year request reports every metric over a period nobody asked
        # for. `metrics` is already a JSON column, so this rides along with it.
        run.metrics = dict(run.metrics or {}, provenance=result["data_source"])
        db.commit()
        db.refresh(run)

        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id, action="BACKTEST_COMPLETED",
            entity_type="strategy_run", entity_id=run.id,
            details={
                "strategy_id": strategy.id,
                "symbol": req.symbol,
                "trades": result["metrics"]["total_trades"],
                "return_pct": result["metrics"]["return_pct"],
                "data_source": run.data_source,
            },
        )
        return run

    @staticmethod
    def list_runs(db: Session, user: User, strategy_id: str) -> List[StrategyRun]:
        strategy = StrategyService.get_strategy(db, user, strategy_id)
        return (
            db.query(StrategyRun)
            .filter(StrategyRun.strategy_id == strategy.id)
            .order_by(StrategyRun.created_at.desc())
            .all()
        )
