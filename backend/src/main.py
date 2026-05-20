from fastapi import FastAPI
from backend.src.routers.user_router import user_router
from backend.src.routers.auth_router import auth_router
from backend.src.routers.room_service_router import room_service_router
from backend.src.routers.room_router import room_router
from backend.src.data.model.user import User
from backend.src.data.model.room import Room
from backend.src.data.model.room_service import RoomService
from backend.src.data.model.room_image import RoomImage
from backend.src.data.model.booking import Booking
from backend.src.data.model.room_service_association import room_service_association
from backend.src.data.model.booking_room_association import booking_room_association

app = FastAPI()

app.include_router(user_router)
app.include_router(auth_router)
app.include_router(room_service_router)
app.include_router(room_router)
