from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from typing import Annotated, List

from backend.src.config.database_config import get_async_session
from backend.src.data.repository.room_service_repository import RoomServiceRepository
from backend.src.service.room_service_service import RoomServiceService
from backend.src.data.schemas.room_service_schema import RoomServiceCreateSchema, RoomServiceSchema
from backend.src.security.authorization import is_admin_user
from backend.src.data.model.user import User

room_service_router = APIRouter(prefix="/room_service", tags=["Room Service"])

async def get_room_service_service(db: AsyncSession = Depends(get_async_session)):
    repo = RoomServiceRepository(db)
    return RoomServiceService(repo)

@room_service_router.post("/", status_code=status.HTTP_201_CREATED)
async def create_room_service(
    payload: RoomServiceCreateSchema,
    service: Annotated[RoomServiceService, Depends(get_room_service_service)],
    current_user: Annotated[User, Depends(is_admin_user)]
) -> None:
    return await service.create_room_service(payload=payload, current_user_id=current_user.id)

@room_service_router.get("/", status_code=status.HTTP_200_OK, response_model=List[RoomServiceSchema])
async def get_all(
    room_service_service: Annotated[RoomServiceService, Depends(get_room_service_service)],
    current_user: User = Depends(is_admin_user)
) -> List[RoomServiceSchema]:
     return await room_service_service.get_all()