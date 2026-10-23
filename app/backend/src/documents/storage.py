import hashlib
import logging
from pathlib import PurePosixPath
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.backend.src.documents.errors import DocumentError

logger = logging.getLogger(__name__)

MAX_STEM_CHARS = 80
# Windows 非法字符（另避开控制字符），一律替换成下划线
_ILLEGAL_CHARS = '<>:"/\\|?*'
# 抽取文本的副产物后缀：``xx.pdf`` → ``xx.extracted.md``。
# **永远带这个后缀**，所以它绝不可能覆盖同名的原对象。
SIDECAR_SUFFIX = ".extracted.md"


def content_hash(content: bytes) -> str:
    """内容指纹（sha256 十六进制）。上传去重要在**落盘之前**用它判断。"""
    return hashlib.sha256(content).hexdigest()


def safe_filename(filename: str) -> str:
    """把用户给的文件名变安全：剥掉目录成分、替换非法字符、截断（保留扩展名）。

    对象存储的 key 同样不能让用户随便拼——``../../other`` 在 S3 里虽然不会真的穿越
    文件系统，但会写出**不属于该租户前缀**的 key，等于绕过隔离。
    """
    name = PurePosixPath(filename or "").name.strip() or "unnamed"
    cleaned = "".join(
        "_" if (ch in _ILLEGAL_CHARS or ord(ch) < 32) else ch for ch in name
    ).strip(". ")
    if not cleaned:
        cleaned = "unnamed"
    stem = (PurePosixPath(cleaned).stem or "unnamed")[:MAX_STEM_CHARS]
    return f"{stem}{PurePosixPath(cleaned).suffix}"


def key_prefix(scope: str, tenant_id: int | None) -> str:
    """按 scope 分前缀：global 一份、每个租户一份（便于按租户清理与统计）。"""
    if scope == "global":
        return "global"
    if tenant_id is None:
        raise DocumentError("DOCUMENT_SCOPE_INVALID", "scope='tenant' 时必须提供 tenant_id")
    return f"tenant_{tenant_id}"


def sidecar_key(key: str) -> str:
    """原对象的 key → 副产物的 key。

    ``global/ab12_手册.pdf`` → ``global/ab12_手册.extracted.md``
    """
    path = PurePosixPath(key)
    return str(path.with_name(f"{path.stem}{SIDECAR_SUFFIX}"))


class ObjectStorage:
    """S3 兼容对象存储客户端（MinIO / 阿里云 OSS / AWS S3 通用）。"""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                # MinIO 与多数自建 S3 网关走 path-style（``host/bucket/key``），
                # 而不是 AWS 的 virtual-hosted（``bucket.host/key``）。云厂商两种都支持。
                s3={"addressing_style": "path"},
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    def ensure_bucket(self) -> None:
        """确保 bucket 存在（幂等）。启动时调一次——省得第一次上传才发现没建。"""
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except ClientError as exc:
            if _error_code(exc) not in ("404", "NoSuchBucket", "NotFound"):
                raise self._unavailable(exc) from exc
        try:
            self._client.create_bucket(Bucket=self._bucket)
            logger.info("已创建对象存储 bucket：%s", self._bucket)
        except (ClientError, BotoCoreError) as exc:
            raise self._unavailable(exc) from exc

    # ---------- 对象读写 ----------

    def save_file(
        self,
        scope: str,
        tenant_id: int | None,
        filename: str,
        content: bytes,
        digest: str | None = None,
    ) -> tuple[str, str]:
        """写入原文件，返回 ``(对象 key, sha256)``。

        ``digest`` 可传入调用方已经算好的指纹（上传时先用它做去重判断），避免重复计算。
        """
        digest = digest or content_hash(content)
        key = f"{key_prefix(scope, tenant_id)}/{uuid4().hex}_{safe_filename(filename)}"
        self._put(key, content, content_type=_content_type(filename))
        return key, digest

    def put_object(self, key: str, body: bytes, *, content_type: str | None = None) -> None:
        """按**指定的 key** 写一个对象。

        与 :meth:`save_file` 的区别：那个自己生成 key（``<uuid>_<name>``，防同名互相覆盖），
        这个由调用方给定。**迁移历史文件时必须用它**——那些文件在数据库里的 ``file_path``
        已经是固定的 key，换个 key 就对不上了。
        """
        self._put(key, body, content_type=content_type or _content_type(key))

    def write_sidecar(self, key: str, text: str) -> str:
        """把抽取出的文本写成**原对象旁边的副产物**；返回副产物的 key。

        ⚠️ **这是给人看的，不是索引的输入**——索引用的是内存里那一份 text。
        所以调用方要把它包在 try 里：写不进去也不该影响索引。

        进对象存储是为了兑现"服务器本地不留实体文件"。要看的时候进 MinIO 控制台
        （9001 端口）点开预览，或者下载下来。
        """
        target = sidecar_key(key)
        self._put(target, text.encode("utf-8"), content_type="text/markdown; charset=utf-8")
        return target

    def read_file(self, key: str) -> bytes:
        """读回原文件字节。

        对象不在时抛明确错误，而不是让 ``NoSuchKey`` 冒到用户面上——索引失败的原因要能看懂。
        """
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if _error_code(exc) in ("NoSuchKey", "404", "NotFound"):
                raise DocumentError(
                    "DOCUMENT_FILE_MISSING", f"原文件不存在：{key}"
                ) from exc
            raise self._unavailable(exc) from exc
        except BotoCoreError as exc:
            raise self._unavailable(exc) from exc

    def delete_file(self, key: str) -> bool:
        """删除原文件**与它的副产物**；返回是否真的删掉了。

        副产物要一起删——否则知识库删了文档，存储里还躺着一份抽取文本，
        既是垃圾也可能残留业务内容。S3 的 delete 本身是幂等的（删不存在的 key 也算成功），
        所以这里先探一次存在性，好让返回值有意义。
        """
        removed = self.exists(key)
        for target in (key, sidecar_key(key)):
            try:
                self._client.delete_object(Bucket=self._bucket, Key=target)
            except (ClientError, BotoCoreError) as exc:
                raise self._unavailable(exc) from exc
        return removed

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if _error_code(exc) in ("NoSuchKey", "404", "NotFound"):
                return False
            raise self._unavailable(exc) from exc
        except BotoCoreError as exc:
            raise self._unavailable(exc) from exc

    # ---------- 内部 ----------
    def _put(self, key: str, body: bytes, *, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=body, ContentType=content_type
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._unavailable(exc) from exc

    def _unavailable(self, exc: Exception) -> DocumentError:
        logger.warning("对象存储不可用（bucket=%s）：%s", self._bucket, exc)
        return DocumentError(
            "DOCUMENT_STORAGE_UNAVAILABLE",
            f"对象存储不可用（bucket={self._bucket}）：{exc}",
        )


def _error_code(exc: ClientError) -> str:
    """S3 的错误码：优先取 ``Error.Code``，退回 HTTP 状态码。

    两者都要看——``NoSuchKey`` 是码，而 head 一个不存在的 key 时 boto3 只给 404。
    """
    error = exc.response.get("Error") or {}
    metadata = exc.response.get("ResponseMetadata") or {}
    return str(error.get("Code") or metadata.get("HTTPStatusCode") or "")


_CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".pdf": "application/pdf",
}


def _content_type(filename: str) -> str:
    return _CONTENT_TYPES.get(PurePosixPath(filename).suffix.lower(), "application/octet-stream")
