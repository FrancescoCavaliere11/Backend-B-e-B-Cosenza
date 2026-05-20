from fastapi import FastAPI
from backend.src.routers.user_router import user_router
from backend.src.routers.auth_router import auth_router
from backend.src.routers.room_service_router import room_service_router
from backend.src.routers.room_router import room_router

app = FastAPI()

app.include_router(user_router)
app.include_router(auth_router)
app.include_router(room_service_router)
app.include_router(room_router)
