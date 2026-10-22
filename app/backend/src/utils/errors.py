from __future__ import annotations

from fastapi import HTTPException, status

# 未登记的领域码按 400 处理
DOMAIN_STATUS: dict[str, int] = {
    # 认证 / 会话
    "AUTH_UNAUTHENTICATED": status.HTTP_401_UNAUTHORIZED,
    "INVALID_CREDENTIALS": status.HTTP_401_UNAUTHORIZED,
    "ACCOUNT_DISABLED": status.HTTP_403_FORBIDDEN,
    "TENANT_INACTIVE": status.HTTP_403_FORBIDDEN,
    "AUTH_FORBIDDEN": status.HTTP_403_FORBIDDEN,
    "FORBIDDEN": status.HTTP_403_FORBIDDEN,
    # 用户
    "USER_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "USERNAME_TAKEN": status.HTTP_409_CONFLICT,
    "ROLE_NOT_ALLOWED": status.HTTP_400_BAD_REQUEST,
    "TENANT_REQUIRED": status.HTTP_400_BAD_REQUEST,
    "SELF_OPERATION_FORBIDDEN": status.HTTP_400_BAD_REQUEST,
    "INVALID_STATUS": status.HTTP_400_BAD_REQUEST,
    # 租户
    "TENANT_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "TENANT_CODE_TAKEN": status.HTTP_409_CONFLICT,
    # 设备
    "DEVICE_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "DEVICE_CODE_TAKEN": status.HTTP_409_CONFLICT,
    "DEVICE_DISABLED": status.HTTP_400_BAD_REQUEST,
    "DEVICE_UNAUTHENTICATED": status.HTTP_401_UNAUTHORIZED,
    # 告警
    "ALERT_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    # 诊断（``DIAGNOSIS_INPUT_MISSING`` 只在 worker 内部抛，不经 HTTP，故不登记）
    "DIAGNOSIS_TASK_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    # 工单
    "WORK_ORDER_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "WORK_ORDER_NOT_RESOLVED": status.HTTP_400_BAD_REQUEST,
    "ALERT_DEVICE_MISMATCH": status.HTTP_400_BAD_REQUEST,
    "ASSIGNEE_NOT_ALLOWED": status.HTTP_400_BAD_REQUEST,
    # 聊天（会话与长期记忆都按用户私有，取不到一律按"不存在"处理）
    "CHAT_SESSION_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "MEMORY_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    # 案例沉淀
    "CASE_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "WORK_ORDER_NOT_CLOSED": status.HTTP_400_BAD_REQUEST,
    "CASE_SCOPE_INVALID": status.HTTP_400_BAD_REQUEST,
    # 知识文档
    "UNSUPPORTED_FILE_TYPE": status.HTTP_400_BAD_REQUEST,
    "DOCUMENT_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "DOCUMENT_TOO_LARGE": status.HTTP_413_CONTENT_TOO_LARGE,
    "DOCUMENT_DUPLICATE": status.HTTP_409_CONFLICT,
    "DOCUMENT_NOT_INDEXABLE": status.HTTP_400_BAD_REQUEST,
    "DOCUMENT_SCOPE_INVALID": status.HTTP_400_BAD_REQUEST,
    "DOCUMENT_PATH_INVALID": status.HTTP_400_BAD_REQUEST,
    "INDEX_TASK_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    # 服务端存储与数据不一致，不是用户的问题 → 500
    "DOCUMENT_FILE_MISSING": status.HTTP_500_INTERNAL_SERVER_ERROR,
    # 对象存储连不上（MinIO/S3 宕了、凭证不对、网络不通）——同样是服务端问题
    "DOCUMENT_STORAGE_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    # 后台任务
    "JOB_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "INVALID_JOB_KIND": status.HTTP_400_BAD_REQUEST,
    "INVALID_RESOURCE_TYPE": status.HTTP_400_BAD_REQUEST,
}


def to_http(exc: Exception) -> HTTPException:
    """把带 ``code``/``message`` 的领域异常转成 HTTPException。"""
    code = getattr(exc, "code", "")
    message = getattr(exc, "message", str(exc))
    http_status = DOMAIN_STATUS.get(code, status.HTTP_400_BAD_REQUEST)
    headers = (
        {"WWW-Authenticate": "Bearer"}
        if http_status == status.HTTP_401_UNAUTHORIZED
        else None
    )
    return HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
        headers=headers,
    )
