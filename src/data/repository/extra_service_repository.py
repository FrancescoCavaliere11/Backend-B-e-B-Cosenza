from typing import List
from unittest import result
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.model.extra_service import ExtraService
from src.exception.custom_exception import EntityNotFound


class ExtraServiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> List[ExtraService]:
        query = select(ExtraService)
        result = await self.session.execute(query)
        return list(result.scalars().all())


    async def create(self, extra_service: ExtraService) -> ExtraService:
        try:
            self.session.add(extra_service)
            await self.session.commit()
            await self.session.refresh(extra_service)
            return extra_service
        except IntegrityError as e:
            await self.session.rollback()
            raise e


    async def update(self, extra_service: ExtraService) -> ExtraService:
        try:
            updated_extra_service = await self.session.merge(extra_service)
            await self.session.commit()
            await self.session.refresh(updated_extra_service)
            return updated_extra_service

        except IntegrityError as e:
            await self.session.rollback()
            raise e


    async def delete_by_id(self, extra_service_id: UUID) -> bool:
        try:
            extra_service = await self.session.get(ExtraService, extra_service_id)
            if not extra_service:
                return False
            await self.session.delete(extra_service)
            await self.session.commit()
            return True
        except IntegrityError as e:
            await self.session.rollback()
            raise e


    async def exists_by_id(self, extra_service_id: UUID) -> bool:
        extra_service = await self.session.get(ExtraService, extra_service_id)
        if not extra_service:
            return False
        return True


    async def exists_by_name_excluding_id(self, name: str, extra_service_id: UUID) -> bool:
        query = select(ExtraService).where(
            ExtraService.name == name,
            ExtraService.id != extra_service_id
        )
        result = await self.session.execute(query)
        existing_service = result.scalar_one_or_none()
        return existing_service is not None


    async def exists_by_name(self, name: str) -> bool:
        query = select(ExtraService).where(ExtraService.name == name)
        result = await self.session.execute(query)
        existing_service = result.scalar_one_or_none()
        return existing_service is not None