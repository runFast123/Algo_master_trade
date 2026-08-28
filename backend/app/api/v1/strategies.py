from typing import List

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_choice_session, get_current_user
from app.models.user import User
from app.schemas.strategy import StrategyCreate, StrategyResponse, StrategyUpdate
from app.schemas.strategy_run import (
    BacktestRequest,
    PaperRunRequest,
    StrategyRunResponse,
)
from app.services.paper_run_service import paper_run_service
from app.services.strategy_service import StrategyService
from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.strategy_engine.dsl import DSLError, dsl_engine
from engine.app.strategy_engine.explain import explain

router = APIRouter()


@router.post("/", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
def create_strategy(
    req: StrategyCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a strategy. The DSL is validated here, so an unknown indicator or
    a misspelt field is reported now rather than never firing at run time."""
    return StrategyService.create_strategy(db, current_user, req)


@router.post("/preview")
def preview_strategy(
    req: dict,
    current_user: User = Depends(get_current_user),
):
    """Validate a draft definition and describe it in English, without saving.

    The builder needs to tell someone what they have just assembled *before*
    they commit to it. Doing that in the browser would mean a second
    implementation of both the validator and the explainer, and the moment the
    two disagree the preview is lying about what the engine will run. So the
    engine answers.
    """
    dsl = req.get("dsl_definition") or {}
    try:
        dsl_engine.validate(dsl)
    except DSLError as exc:
        # A draft in progress is expected to be invalid; that is a state to
        # report, not a request to reject.
        return {"valid": False, "error": str(exc), "explanation": explain(dsl)}
    return {"valid": True, "error": None,
            "explanation": explain(dsl, symbol=req.get("symbol") or "")}


@router.get("/", response_model=List[StrategyResponse])
def list_strategies(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return StrategyService.list_strategies(db, current_user)


@router.get("/{strategy_id}", response_model=StrategyResponse)
def get_strategy(
    strategy_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return StrategyService.get_strategy(db, current_user, strategy_id)


@router.put("/{strategy_id}", response_model=StrategyResponse)
def update_strategy(
    strategy_id: str,
    req: StrategyUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return StrategyService.update_strategy(db, current_user, strategy_id, req)


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_strategy(
    strategy_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    StrategyService.delete_strategy(db, current_user, strategy_id)


@router.post("/{strategy_id}/backtest", response_model=StrategyRunResponse)
def run_backtest(
    strategy_id: str,
    req: BacktestRequest,
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
):
    """Run the strategy over historical bars.

    Requires a Choice session because the bars come from the user's own data
    entitlement. Metrics are computed from the run; a run that cannot complete
    returns an error rather than a placeholder result.
    """
    return StrategyService.run_backtest(db, current_user, session, strategy_id, req)


@router.post("/{strategy_id}/start")
def start_paper_run(
    strategy_id: str,
    req: PaperRunRequest,
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
):
    """Run this strategy on paper against live prices.

    PAPER only: fills are simulated at the live traded price and nothing is
    sent to Choice. The run refuses to start without real market data, because
    a strategy with no prices cannot decide anything.
    """
    strategy = StrategyService.get_strategy(db, current_user, strategy_id)
    return paper_run_service.start_run(
        db, current_user, session, strategy, req.model_dump()
    )


@router.post("/{strategy_id}/stop")
def stop_paper_run(
    strategy_id: str,
    run_id: str = Query(..., description="The run to stop"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stop a running strategy. Reachable whether or not the runner is healthy."""
    StrategyService.get_strategy(db, current_user, strategy_id)   # tenant check
    return paper_run_service.stop_run(db, current_user, run_id)


@router.get("/{strategy_id}/run-status")
def paper_run_status(
    strategy_id: str,
    run_id: str = Query(..., description="The run to report on"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    StrategyService.get_strategy(db, current_user, strategy_id)   # tenant check
    return paper_run_service.run_status(db, run_id)


@router.get("/{strategy_id}/runs", response_model=List[StrategyRunResponse])
def list_runs(
    strategy_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return StrategyService.list_runs(db, current_user, strategy_id)
