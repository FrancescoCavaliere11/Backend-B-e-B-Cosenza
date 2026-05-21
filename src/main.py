from fastapi import FastAPI
from src.routers.user_router import user_router
from src.routers.auth_router import auth_router
from src.routers.room_service_router import room_service_router
from src.routers.room_router import room_router

from src.data.model.user import User
from src.data.model.room import Room
from src.data.model.booking import Booking
from src.data.model.room_service_association import room_service_association
from src.data.model.booking_room_association import booking_room_association
from src.data.model.room_service import RoomService
from src.data.model.room_image import RoomImage

app = FastAPI()

app.include_router(user_router)
app.include_router(auth_router)
app.include_router(room_service_router)
app.include_router(room_router)
