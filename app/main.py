# backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import logging
import sys
from app.core.config import settings
from app.api.router import api_router
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import HTTPException
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from app.api.exception_handlers import register_exception_handlers
from app.db.session import MasterSessionLocal
from app.services.error_logger import log_error, format_exception
from app.utils.jwt import extract_tenant_from_request
from fastapi.responses import JSONResponse
from fastapi import Request

# from app.api.routes_lis_device import public_router as lis_public_router

app = FastAPI(
    title=settings.PROJECT_NAME,
    docs_url="/docs",
    redoc_url="/redoc",
)
register_exception_handlers(app)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,  # important if uvicorn already configured logging
    )

setup_logging()


logger = logging.getLogger("app")
logger.info("✅ App starting...")

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Catch-all for unhandled exceptions (true 500s).
    """
    status_code = HTTP_500_INTERNAL_SERVER_ERROR
    stack = format_exception(exc)

    tenant_code = extract_tenant_from_request(request)

    # basic request context
    try:
        body = await request.body()
    except Exception:
        body = b""

    db = MasterSessionLocal()
    try:
        log_error(
            db=db,
            description=str(exc),
            error_source="backend",
            endpoint=f"{request.method} {request.url.path}",
            module=request.scope.get("endpoint").__module__
            if request.scope.get("endpoint")
            else None,
            function=request.scope.get("endpoint").__name__
            if request.scope.get("endpoint")
            else None,
            http_status=status_code,
            tenant_code=tenant_code,
            request_payload={
                "path_params": request.path_params,
                "query_params": dict(request.query_params),
                "body": body.decode("utf-8", errors="ignore") or None,
            },
            response_payload=None,
            stack_trace=stack,
        )
    finally:
        db.close()

    return JSONResponse(
        status_code=status_code,
        content={"detail": "Internal server error. Our team has been notified."},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """
    Optional: log HTTPExceptions (4xx, 403, etc.) as well.
    """
    tenant_code = extract_tenant_from_request(request)

    db = MasterSessionLocal()
    try:
        log_error(
            db=db,
            description=str(exc.detail),
            error_source="backend",
            endpoint=f"{request.method} {request.url.path}",
            module=request.scope.get("endpoint").__module__
            if request.scope.get("endpoint")
            else None,
            function=request.scope.get("endpoint").__name__
            if request.scope.get("endpoint")
            else None,
            http_status=exc.status_code,
            tenant_code=tenant_code,
            request_payload={
                "path_params": request.path_params,
                "query_params": dict(request.query_params),
            },
            response_payload=None,
            stack_trace=None,
        )
    finally:
        db.close()

    # return default-style HTTPException response
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),

    )

@app.options("/{rest_of_path:path}")
async def cors_preflight_handler(rest_of_path: str, request: Request):
    origin = (request.headers.get("origin") or "").rstrip("/")

    allowed_origins = {str(o).rstrip("/") for o in settings.BACKEND_CORS_ORIGINS}

    headers = {
        "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", "*"),
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }

    if origin in allowed_origins:
        headers["Access-Control-Allow-Origin"] = origin

    return JSONResponse(status_code=204, content=None, headers=headers)


# # 🔥 Global OPTIONS handler – CORS + Cloudflare safe
# @app.options("/{rest_of_path:path}")
# async def cors_preflight_handler(rest_of_path: str, request: Request):
#     origin = request.headers.get("origin")
#     allowed_origins = settings.BACKEND_CORS_ORIGINS

#     headers = {
#         "Access-Control-Allow-Methods":
#         "GET, POST, PUT, PATCH, DELETE, OPTIONS",
#         "Access-Control-Allow-Headers":
#         request.headers.get("access-control-request-headers", "*"),
#         "Access-Control-Allow-Credentials":
#         "true",
#         "Vary":
#         "Origin",
#     }

#     # Only echo origin if it is in the allowed list
#     if origin in allowed_origins:
#         headers["Access-Control-Allow-Origin"] = origin

#     return JSONResponse(
#         status_code=200,
#         content={"message": "preflight ok"},
#         headers=headers,
#     )

# app.include_router(lis_public_router, prefix=settings.API_V1_STR)
app.include_router(api_router, prefix=settings.API_V1_STR)

# Media mount
# MEDIA_ROOT = Path(settings.STORAGE_DIR).resolve()
# MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
# app.mount("/media", StaticFiles(directory=str(MEDIA_ROOT)), name="media")

MEDIA_ROOT = Path(settings.STORAGE_DIR).resolve()
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount(settings.MEDIA_URL,
          StaticFiles(directory=str(MEDIA_ROOT)),
          name="media")


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
