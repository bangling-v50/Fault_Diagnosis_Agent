from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from app.backend.src.containers import Container
from app.backend.src.routers.alerts import router as alerts_router
from app.backend.src.routers.auth import router as auth_router
from app.backend.src.routers.cases import router as cases_router
from app.backend.src.routers.chat import router as chat_router
from app.backend.src.routers.devices import router as devices_router
from app.backend.src.routers.diagnoses import router as diagnoses_router
from app.backend.src.routers.documents import router as documents_router
from app.backend.src.routers.jobs import router as jobs_router
from app.backend.src.routers.tenants import router as tenants_router
from app.backend.src.routers.users import router as users_router
from app.backend.src.routers.work_orders import router as work_orders_router
from app.backend.src.schemas.common import APIResponse, ok
from app.backend.src.settings import load_settings

# 本地前端 dev 常用端口（5173 常被其他项目占用，实际多跑在 5174）
_ALLOW_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5174",
]


def _envelope(code: int, message: str, error: str | None = None) -> dict:
    """统一响应形状（与 schemas/common.APIResponse 保持一致）。"""
    return {"code": code, "message": message, "data": None, "error": error}


def register_exception_handlers(app: FastAPI) -> None:
    """把 HTTPException 与参数校验错误也渲染成统一形状 {code,message,data,error}。"""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        error: str | None = None
        if isinstance(detail, dict):
            error = detail.get("code")
            message = str(detail.get("message") or detail)
        else:
            message = str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.status_code, message, error),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            message = f"参数校验失败：{location} {first.get('msg', '')}".strip()
        else:
            message = "参数校验失败"
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope(422, message, "VALIDATION_ERROR"),
        )


def create_app() -> FastAPI:
    container = Container(load_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await container.start()  # 启动后台 worker（durable job runtime）
        try:
            yield
        finally:
            await container.aclose()

    app = FastAPI(title="FaDia Agent", version="0.1.0", lifespan=lifespan)
    app.state.container = container

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOW_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)

    # 逐个挂载已实现的路由；未实现的功能在后续步骤补
    app.include_router(auth_router)
    app.include_router(tenants_router)
    app.include_router(users_router)
    app.include_router(devices_router)
    app.include_router(jobs_router)
    app.include_router(alerts_router)
    app.include_router(diagnoses_router)
    app.include_router(work_orders_router)
    app.include_router(cases_router)
    app.include_router(chat_router)
    app.include_router(documents_router)

    @app.get("/health", response_model=APIResponse[dict])
    async def health() -> APIResponse:
        return ok({"status": "ok"})

    @app.get("/ready", response_model=APIResponse[dict])
    async def ready():
        try:
            await container.ping()
        except Exception as exc:  # noqa: BLE001 —— 就绪检查要如实上报失败原因
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=_envelope(503, f"数据库不可用：{exc}", "DATABASE_UNAVAILABLE"),
            )
        return ok({"status": "ready", "database": "ok"})

    return app


app = create_app()
