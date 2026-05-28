from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.routers.extra_service_router import extra_service_router
from src.routers.user_router import user_router
from src.routers.auth_router import auth_router
from src.routers.room_service_router import room_service_router
from src.routers.room_router import room_router
from src.exception.exception_handler import setup_exception_handler

from src.data.model.user import User
from src.data.model.room import Room
from src.data.model.booking import Booking
from src.data.model.room_service_association import room_service_association
from src.data.model.booking_room_association import booking_room_association
from src.data.model.room_service import RoomService
from src.data.model.room_image import RoomImage
from src.data.model.extra_service import ExtraService

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

setup_exception_handler(app)

app.include_router(user_router)
app.include_router(auth_router)
app.include_router(room_service_router)
app.include_router(room_router)
app.include_router(extra_service_router)
