from fastapi import APIRouter, Depends

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.status import StatusMetadataProviderTestResource
from src.service.system.status_service import StatusService

router = APIRouter(
    tags=["metadata-providers"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post(
    "/metadata-providers/{provider}/test",
    response_model=StatusMetadataProviderTestResource,
)
def test_metadata_provider(provider: str):
    normalized_provider = provider.strip().lower()
    if normalized_provider not in {"javdb"}:
        raise ApiError(
            422,
            "invalid_metadata_provider",
            "Metadata provider must be javdb",
            {"provider": provider},
        )
    return StatusService.test_metadata_provider(normalized_provider)
