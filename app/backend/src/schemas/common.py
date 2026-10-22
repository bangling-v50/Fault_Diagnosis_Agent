from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageQuery(BaseModel):
    """分页查询参数；在路由里作为 Query 依赖使用：``page: Annotated[PageQuery, Depends()]``。"""

    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


class APIResponse(BaseModel, Generic[T]):
    code: int = 0
    message: str = "ok"
    data: T | None = None
    error: str | None = None


class PageData(BaseModel, Generic[T]):
    total: int
    page: int
    page_size: int
    items: list[T]


def ok(data: Any = None, message: str = "ok") -> APIResponse:
    """成功响应。"""
    return APIResponse(code=0, message=message, data=data)


def page_data(*, items: list[Any], total: int, page: int, page_size: int) -> PageData:
    return PageData(total=total, page=page, page_size=page_size, items=items)
