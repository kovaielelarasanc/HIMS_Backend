# backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.core.config import settings
from app.api.router import api_router

app = FastAPI(
    title=settings.PROJECT_NAME,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(api_router, prefix=settings.API_V1_STR)

# Media mount
# MEDIA_ROOT = Path(settings.STORAGE_DIR).resolve()
# MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
# app.mount("/media", StaticFiles(directory=str(MEDIA_ROOT)), name="media")


MEDIA_ROOT = Path(settings.STORAGE_DIR).resolve()
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount(settings.MEDIA_URL, StaticFiles(directory=str(MEDIA_ROOT)), name="media")


# Health
@app.get("/")
def root():
    return {"message": "NABH HIMS & EMR API running", "version": "v1"}


# @app.get("/favicon.ico")
# def favicon():
#     from fastapi.responses import Response
#     return Response(status_code=204)
# @app.on_event("startup")
# def _debug_media():
#     p = Path(settings.STORAGE_DIR).resolve()
#     print("STORAGE_DIR =", p)
#     test = p / "uploads" / "2025" / "11" / "06"
#     print("Exists?", test.exists(), "->", test)
#     if test.exists():
#         print("Files:", [f.name for f in test.iterdir()])
