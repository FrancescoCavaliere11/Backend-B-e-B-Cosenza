from typing import List, Dict
from uuid import UUID
import os
from fastapi import UploadFile, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.src.data.model.room import Room
from backend.src.data.model.room_image import RoomImage
from backend.src.data.model.room_service import RoomService as RoomServiceModel
from backend.src.data.repository.room_repository import RoomRepository
from backend.src.data.repository.room_service_repository import RoomServiceRepository
from backend.src.data.schemas.room_image_schema import RoomImageCreateSchema
from backend.src.data.schemas.room_schema import RoomCreateSchema
from backend.src.security.audit_logging import apply_audit_fields

class RoomService:
    def __init__(
            self,
            room_repository: RoomRepository,
            room_service_repository: RoomServiceRepository,
            session: AsyncSession
    ):
        self.room_repository = room_repository
        self.room_service_repository = room_service_repository
        self.session = session


    async def create_room(
            self,
            payload: RoomCreateSchema,
            images: List[UploadFile],
            current_user_id: UUID
    ) -> None:
        async with self.session.begin():
            room_services = await self._validate_and_get_services(payload.room_services_ids)

            image_map = _build_image_map(images, len(payload.room_images))
            room_images = await _process_room_images(payload.room_images, image_map, current_user_id)

            new_room = Room(
                name=payload.name,
                capacity=payload.capacity,
                price=payload.price,
                room_images=room_images,
                services=room_services
            )

            apply_audit_fields(audit=new_room, user_id=current_user_id, is_create=True)
            self.session.add(new_room)

    async def _validate_and_get_services(self, service_ids: List[UUID]) -> List[RoomServiceModel]:
        room_services = await self.room_service_repository.get_all_by_id(service_ids)
        if len(room_services) != len(service_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uno o più servizi selezionati non sono validi"
            )
        return list(room_services)






async def _process_room_images(
    image_schemas: List[RoomImageCreateSchema],
    image_map: Dict[UUID, UploadFile],
    current_user_id: UUID
) -> List[RoomImage]:
    room_images: List[RoomImage] = []
    for room_image_schema in image_schemas:
        img = image_map.get(room_image_schema.client_image_id)
        if not img:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Corrispondenza immagine non trovata per l'ID fornito"
            )

        # TODO: Inserire la logica per salvare il file "img" su Cloudinary/S3/Disco
        generated_url = "url di prova"

        room_image = RoomImage(
            url=generated_url,
            is_primary=room_image_schema.is_primary,
        )
        apply_audit_fields(room_image, current_user_id, is_create=True)
        room_images.append(room_image)
    return room_images


def _build_image_map(images: List[UploadFile], expected_count: int) -> Dict[UUID, UploadFile]:
    if expected_count != len(images):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Il numero di immagini fornite non corrisponde ai metadati"
        )

    image_map: Dict[UUID, UploadFile] = {}
    for img in images:
        if not img.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uno dei file caricati non ha un nome valido"
            )

        filename_without_ext, _ = os.path.splitext(img.filename)
        try:
            image_id = UUID(filename_without_ext)
            image_map[image_id] = img
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Il nome del file '{img.filename}' non contiene un UUID valido"
            )
    return image_map