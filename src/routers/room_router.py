from uuid import UUID
from fastapi import APIRouter, status, Form, UploadFile, File, Depends
from typing import Annotated, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.config.database_config import get_async_session
from src.data.model.user import User
from src.data.repository.room_repository import RoomRepository
from src.data.repository.room_service_repository import RoomServiceRepository
from src.data.schemas.room_schema import RoomCreateSchema, RoomPublicSchema, RoomSchema, RoomUpdateSchema
from src.routers.dependencies import parse_form_model
from src.service.room_service import RoomService
from src.security.authorization import is_admin_user
from src.security.rate_limiter import public_rooms_rate_limit


room_router = APIRouter(prefix="/api/v1/room", tags=["Room"])


async def get_room_service(db: AsyncSession = Depends(get_async_session)):
    room_repo = RoomRepository(db)
    room_service_repo = RoomServiceRepository(db)
    return RoomService(
        room_repository=room_repo,
        room_service_repository=room_service_repo,
        session=db
    )

@room_router.get("/", response_model=List[RoomSchema], dependencies=[Depends(is_admin_user)])
async def get_all(
        room_service: Annotated[RoomService, Depends(get_room_service)],
) -> List[RoomSchema]:
    """
    Tutte le camere, **disattivate comprese**, con i campi del back-office.

    Solo per l'admin: il sito pubblico usa `GET /api/v1/room/enabled`, che
    restituisce le sole camere in vendita (lacuna B1).
    """
    return await room_service.get_all()


@room_router.get(
    "/enabled",
    response_model=List[RoomPublicSchema],
    dependencies=[Depends(public_rooms_rate_limit)],
)
async def get_enabled(
        room_service: Annotated[RoomService, Depends(get_room_service)],
) -> List[RoomPublicSchema]:
    """
    Camere in vendita, per il sito pubblico (lacuna B1): solo le abilitate,
    ordinate per numero, con i dati da vetrina. Pubblico, senza login.

    Il filtro sulle disattivate sta qui e non nel browser.
    """
    return await room_service.get_public_rooms()



@room_router.post("/", status_code=status.HTTP_201_CREATED, response_model=RoomSchema)
async def create_room(
    room_form: Annotated[str, Form()],
    image: Annotated[UploadFile, File()],
    service: Annotated[RoomService, Depends(get_room_service)],
    current_user: Annotated[User,Depends(is_admin_user)]
) -> RoomSchema:
    payload = parse_form_model(RoomCreateSchema, room_form)
    return await service.create_room(payload=payload, image=image, current_user_id=current_user.id)


@room_router.put("/", status_code=status.HTTP_200_OK, response_model=RoomSchema)
async def update_room(
    room_form: Annotated[str, Form()],
    service: Annotated[RoomService, Depends(get_room_service)],
    current_user: Annotated[User, Depends(is_admin_user)],
    image: Annotated[Optional[UploadFile], File()] = None,
) -> RoomSchema:
    payload = parse_form_model(RoomUpdateSchema, room_form)
    return await service.update_room(payload=payload, image=image, current_user_id=current_user.id)


@room_router.delete("/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room(
    room_id: UUID,
    service: Annotated[RoomService, Depends(get_room_service)],
    current_user: Annotated[User, Depends(is_admin_user)]
) -> None:
    await service.delete_room(room_id=room_id)
