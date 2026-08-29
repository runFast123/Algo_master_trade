from fastapi import APIRouter, Query
from app.config import settings
from app.services import updater_service
from pydantic import BaseModel

router = APIRouter()


@router.get("/version")
def get_version_info():
    """Returns application name, current version, and repository information."""
    return {
        "app_name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "repo": getattr(settings, "GITHUB_REPO", "runFast123/Algo_master_trade"),
    }


@router.get("/update-check")
def check_updates(force: bool = Query(default=False, description="Force re-check GitHub API")):
    """Checks GitHub releases for new versions and returns update details."""
    repo = getattr(settings, "GITHUB_REPO", "runFast123/Algo_master_trade")
    return updater_service.check_for_updates(
        repo=repo,
        current_version=settings.VERSION,
        force_check=force,
    )


class ApplyUpdateRequest(BaseModel):
    download_url: str


@router.post("/update/apply")
def apply_update(req: ApplyUpdateRequest):
    """Downloads the new executable and applies the update."""
    try:
        updater_service.apply_update_async(req.download_url)
        return {"status": "updating", "message": "Downloading update. The application will restart shortly."}
    except RuntimeError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
