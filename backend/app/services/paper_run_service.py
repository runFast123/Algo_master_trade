"""Start and stop paper strategy runs.

A run is two things kept in step: a `StrategyRun` row that survives a restart,
and a `LiveStrategyRunner` in memory that does not. The row is the record; the
runner is the activity. When they disagree — because the process was killed
mid-run — the row is the one that has to be corrected, which is what
`recover_orphaned_runs` does at boot.

PAPER only. Nothing here can submit an order to Choice: the runner's own
`_submit` branches on mode before any client is touched, and `start_run`
refuses any mode but PAPER outright.
"""

import logging
from typing import Any, Dict, List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.strategy_run import StrategyRun
from app.models.user import User
from app.repositories.audit_repo import audit_repo
from engine.app.choice_gateway import market as market_gateway
from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceGatewayError
from engine.app.choice_gateway.scrip_master import resolve_instrument
from engine.app.strategy_engine.runner import (
    LiveStrategyRunner,
    RunMode,
    run_registry,
)
from engine.app.strategy_engine.scheduler import BAR_SECONDS, paper_scheduler

logger = logging.getLogger("paper_run")

RUNNING_STATES = {"RUNNING"}


def _summary(runner: LiveStrategyRunner) -> Dict[str, Any]:
    return {
        "position_qty": runner.position_qty,
        "entry_price": round(runner.entry_price, 2),
        "realized_pnl": round(runner.realized_pnl, 2),
        "orders": len(runner.orders),
        "errors": runner.errors[-3:],
    }


class PaperRunService:
    @staticmethod
    def _refuse(status_code: int, detail: str, **context) -> HTTPException:
        """Refuse a run, and say why in the log as well as the response.

        A 409 with no server-side trace forces the next person to reproduce the
        problem to learn anything about it, which is the dead end the Health
        panel exists to close.
        """
        logger.warning(
            "Paper run refused (%d): %s%s", status_code, detail,
            f" | {context}" if context else "",
        )
        return HTTPException(status_code=status_code, detail=detail)

    @staticmethod
    def start_run(
        db: Session,
        user: User,
        session: ChoiceSession,
        strategy,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Begin a paper run for one strategy."""
        logger.info(
            "Paper run requested: strategy=%s symbol=%s timeframe=%s mode=%s",
            getattr(strategy, "name", "?"), params.get("symbol"),
            params.get("timeframe"), session.mode.value,
        )
        # A strategy with no prices cannot decide anything. Refusing here beats
        # a run that looks alive and never acts.
        if not session.mode.uses_broker_data:
            raise PaperRunService._refuse(
                409,
                "Paper runs need real market data. This session is a sandbox — "
                "reconnect in paper mode against your Choice account.",
                mode=session.mode.value,
            )
        timeframe = str(params.get("timeframe", "1m")).lower()
        if timeframe not in BAR_SECONDS:
            raise PaperRunService._refuse(
                422,
                f"Unsupported timeframe {timeframe!r}. "
                f"Supported: {', '.join(sorted(BAR_SECONDS))}",
            )

        # One live run per strategy. Two runners on one strategy would compete
        # for the same position and neither would be correct.
        existing = (
            db.query(StrategyRun)
            .filter(StrategyRun.strategy_id == strategy.id,
                    StrategyRun.run_type == "PAPER",
                    StrategyRun.status.in_(RUNNING_STATES))
            .first()
        )
        if existing is not None and paper_scheduler.is_running(existing.id):
            raise PaperRunService._refuse(
                409, "This strategy is already running. Stop it first.",
                existing_run=existing.id,
            )

        symbol = params.get("symbol") or ""
        instrument = resolve_instrument(
            session, symbol, params.get("segment_id"), params.get("token")
        )
        segment_id = int(instrument["segment_id"])
        token = str(instrument["token"])

        # Prove the instrument is quotable *now*, rather than trusting a flag
        # cached earlier in the session. A cached verdict goes stale in both
        # directions: one transient failure would block a run for the rest of
        # the session, and an entitlement withdrawn since would let a run start
        # that could never work. This also checks the specific instrument, which
        # a session-wide flag never could — and the run would have failed on its
        # first poll anyway, so failing here costs nothing and explains more.
        try:
            quotes = market_gateway.get_multiple_touchline(
                session, f"{segment_id}_{token}"
            )
            priced = next(
                (row for row in (quotes.get("data") or []) if row.get("ltp")), None
            )
        except ChoiceGatewayError as exc:
            raise PaperRunService._refuse(
                409,
                f"Cannot price {symbol or token} right now, so the strategy has "
                f"nothing to act on. Choice said: "
                f"{exc.message} {exc.details}".strip(),
                instrument=f"{segment_id}_{token}",
            )
        except Exception as exc:
            raise PaperRunService._refuse(
                409, f"Cannot price {symbol or token} right now: {exc}",
                instrument=f"{segment_id}_{token}",
                error_type=type(exc).__name__,
            )

        if priced is None:
            raise PaperRunService._refuse(
                409,
                f"Choice returned no price for {symbol or token}. The instrument "
                "may be wrong, or market data may not be enabled for this "
                "account. Health -> Run diagnostics reports which.",
                instrument=f"{segment_id}_{token}",
                rows_returned=len(quotes.get("data") or []),
            )

        run = StrategyRun(
            strategy_id=strategy.id,
            run_type="PAPER",
            status="RUNNING",
            params={**params, "segment_id": segment_id, "token": str(token),
                    "timeframe": timeframe},
            logs=[f"Paper run started on {symbol} ({timeframe})"],
            data_source="CHOICE_OPENAPI",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        runner = LiveStrategyRunner(
            run_id=run.id,
            session=session,
            dsl_def=strategy.dsl_definition,
            params={"segment_id": segment_id, "token": token, "symbol": symbol},
            mode=RunMode.PAPER,
        )
        runner.start()
        run_registry.add(runner)

        # The halt callback closes the database row, so a run that stops
        # because of a feed gap or an expired key does not sit at RUNNING.
        def _on_halt(run_id: str, reason: str) -> None:
            PaperRunService._close_run(run_id, "HALTED", reason)

        try:
            paper_scheduler.start(runner, timeframe=timeframe, on_halt=_on_halt)
        except Exception as exc:
            run.status = "FAILED"
            run.logs = (run.logs or []) + [f"Could not start: {exc}"]
            db.commit()
            raise HTTPException(status_code=500, detail=f"Could not start the run: {exc}")

        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id,
            action="PAPER_RUN_STARTED", entity_type="strategy_run",
            entity_id=run.id,
            details={"strategy": strategy.name, "symbol": symbol,
                     "timeframe": timeframe},
        )
        return {"status": "SUCCESS", "run_id": run.id, "state": "RUNNING",
                "symbol": symbol, "timeframe": timeframe}

    @staticmethod
    def stop_run(db: Session, user: User, run_id: str) -> Dict[str, Any]:
        run = db.query(StrategyRun).filter(StrategyRun.id == run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail="No such run")

        paper_scheduler.stop(run_id)
        runner = run_registry.get(run_id)
        if runner is not None:
            runner.stop()

        run.status = "STOPPED"
        run.metrics = _summary(runner) if runner else run.metrics
        run.logs = (run.logs or []) + ["Stopped by user"]
        db.commit()

        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id,
            action="PAPER_RUN_STOPPED", entity_type="strategy_run",
            entity_id=run_id,
        )
        return {"status": "SUCCESS", "run_id": run_id, "state": "STOPPED",
                "metrics": run.metrics}

    @staticmethod
    def run_status(db: Session, run_id: str) -> Dict[str, Any]:
        run = db.query(StrategyRun).filter(StrategyRun.id == run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail="No such run")

        runner = run_registry.get(run_id)
        live = paper_scheduler.is_running(run_id)
        return {
            "run_id": run_id,
            "status": run.status,
            "scheduled": live,
            "state": runner.state.value if runner else None,
            "params": run.params,
            "metrics": _summary(runner) if runner else run.metrics,
            "orders": (runner.orders[-20:] if runner else []),
            "logs": (run.logs or [])[-20:],
        }

    @staticmethod
    def _close_run(run_id: str, status: str, reason: str) -> None:
        """Close a run row from outside a request, e.g. a scheduler halt."""
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            run = db.query(StrategyRun).filter(StrategyRun.id == run_id).first()
            if run is None or run.status not in RUNNING_STATES:
                return
            runner = run_registry.get(run_id)
            run.status = status
            run.metrics = _summary(runner) if runner else run.metrics
            run.logs = (run.logs or []) + [reason]
            db.commit()
        except Exception:
            logger.exception("Could not close run %s", run_id)
        finally:
            db.close()


def recover_orphaned_runs() -> int:
    """Mark runs that were RUNNING when the process died as INTERRUPTED.

    Called at startup. Without it a killed process leaves rows claiming to be
    running forever, and the interface would show a strategy trading when
    nothing is. A phantom position is worse than a run that refuses to start.
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        orphans: List[StrategyRun] = (
            db.query(StrategyRun)
            .filter(StrategyRun.run_type == "PAPER",
                    StrategyRun.status.in_(RUNNING_STATES))
            .all()
        )
        for run in orphans:
            run.status = "INTERRUPTED"
            run.logs = (run.logs or []) + [
                "Interrupted: the application stopped while this run was active. "
                "Paper positions from it were not carried over."
            ]
        if orphans:
            db.commit()
            logger.warning(
                "Marked %d paper run(s) as INTERRUPTED after a restart", len(orphans)
            )
        return len(orphans)
    except Exception:
        logger.exception("Could not recover orphaned runs")
        return 0
    finally:
        db.close()


paper_run_service = PaperRunService()
