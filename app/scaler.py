"""Small, request-billed control service using the same deployment image."""

from fastapi import FastAPI

from app.routers.internal_worker_pool import router

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
app.include_router(router)
