from uuid import UUID

from fastapi import APIRouter, Depends, status, Form, File, UploadFile, HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.annotation import Annotated
from typing import Annotated, List, Optional
import json

from src.config.database_config import get_async_session
from src.data.model.user import User
from src.data.repository.extra_service_repository import ExtraServiceRepository
from src.data.schemas.extra_service_schema import ExtraServiceCreateSchema, ExtraServiceSchema, ExtraServiceUpdateSchema
from src.security.authorization import is_admin_user
from src.service.extra_service_service import ExtraServiceService

extra_service_router = APIRouter(prefix="/api/v1/extra-service", tags=["Extra Service"])

async def get_extra_service_service(db: AsyncSession = Depends(get_async_session)):
    repo = ExtraServiceRepository(db)
    return ExtraServiceService(repo, db)


@extra_service_router.get("/", response_model=List[ExtraServiceSchema])
async def get_all(
        extra_service_service: Annotated[ExtraServiceService, Depends(get_extra_service_service)]
) -> List[ExtraServiceSchema]:
    return await extra_service_service.get_all()


@extra_service_router.post("/", status_code=status.HTTP_201_CREATED, response_model=ExtraServiceSchema)
async def create_extra_service(
    extra_service_form: Annotated[str, Form()],
    image: Annotated[UploadFile, File()],
    extra_service_service: Annotated[ExtraServiceService, Depends(get_extra_service_service)],
    current_user: Annotated[User, Depends(is_admin_user)]
) -> ExtraServiceSchema:
    extra_service_data_json = json.loads(extra_service_form)
    payload = ExtraServiceCreateSchema(**extra_service_data_json)
    return await extra_service_service.create_extra_service(
        payload=payload,
        image=image,
        current_user_id=current_user.id
    )


@extra_service_router.put("/", status_code=status.HTTP_202_ACCEPTED, response_model=ExtraServiceSchema)
async def update_extra_service(
        extra_service_form: Annotated[str, Form()],
        extra_service_service: Annotated[ExtraServiceService, Depends(get_extra_service_service)],
        current_user: Annotated[User, Depends(is_admin_user)],
        image: Annotated[Optional[UploadFile], File()] = None,
):
    extra_service_data_json = json.loads(extra_service_form)
    payload = ExtraServiceUpdateSchema(**extra_service_data_json)
    return await extra_service_service.update_extra_service(
        payload=payload,
        image=image,
        current_user_id=current_user.id
    )

@extra_service_router.delete("/{extra_service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room_service(
    extra_service_id: UUID,
    room_service_service: Annotated[ExtraServiceService, Depends(get_extra_service_service)],
    current_user: Annotated[User, Depends(is_admin_user)]
) -> None :
    await room_service_service.delete_extra_service(extra_service_id=extra_service_id)