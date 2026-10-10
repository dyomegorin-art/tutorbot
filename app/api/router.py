from fastapi import APIRouter

from app.api import auth, bookings, payments, slots, students, webhooks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(slots.router)
api_router.include_router(bookings.router)
api_router.include_router(payments.router)
api_router.include_router(students.router)
api_router.include_router(webhooks.router)
