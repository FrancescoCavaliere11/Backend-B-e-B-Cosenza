from typing import List
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.model.extra_service import ExtraService


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


    async def delete(self, extra_service_id: UUID) -> bool:
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
