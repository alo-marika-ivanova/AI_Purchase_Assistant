from __future__ import annotations

from fastapi import FastAPI

from app.db.database import initialize_database
from app.api.whatsapp_webhook import router as whatsapp_router

app = FastAPI(title="AI Purchase Assistant API")

initialize_database()

app.include_router(whatsapp_router)


@app.get("/health")
def health_check():
    return {
        "status": "ok",
    }