from __future__ import annotations

import secrets
from hashlib import sha256

# 设备令牌带可识别前缀：日志、配置文件里一眼能认出这是凭据
DEVICE_TOKEN_PREFIX = "fadia_dev_"


def generate_token(prefix: str = "") -> str:
    """生成不透明随机令牌（默认 32 字节 urlsafe，约 43 字符）。"""
    return f"{prefix}{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    """令牌哈希（SHA-256 十六进制，64 字符）。"""
    return sha256(token.encode("utf-8")).hexdigest()
