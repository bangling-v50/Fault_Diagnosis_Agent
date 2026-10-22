from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from app.backend.src.containers import Container
from app.backend.src.services.job_service import JobError
from app.backend.src.services.tenant_service import TenantError
from app.backend.src.services.user_service import UserError
from app.backend.src.settings import load_settings

# CLI 视为超级用户：仅供本地 bootstrap，不经过 HTTP 权限层
_CLI_ACTOR = SimpleNamespace(id=0, role="admin")


async def _create_tenant(container: Container, args: argparse.Namespace) -> None:
    tenant = await container.tenant_service.create(
        tenant_code=args.code,
        tenant_name=args.name,
        region=args.region,
        address=args.address,
        contact_person=args.contact,
        contact_phone=args.phone,
    )
    print(
        f"已创建租户：id={tenant.id} code={tenant.tenant_code} "
        f"name={tenant.tenant_name} status={tenant.status}"
    )


async def _create_user(container: Container, args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass("请输入密码（至少 8 位）：")
    user = await container.user_service.create_user(
        _CLI_ACTOR,
        username=args.username,
        password=password,
        role=args.role,
        tenant_id=args.tenant_id,
        real_name=args.real_name,
        email=args.email,
    )
    print(
        f"已创建账号：id={user.id} username={user.username} "
        f"role={user.role} tenant_id={user.tenant_id}"
    )


async def _enqueue_job(container: Container, args: argparse.Namespace) -> None:
    payload = json.loads(args.payload) if args.payload else {}
    if args.fail_first:
        payload["fail_first"] = True
    job = await container.job_service.enqueue(
        kind=args.kind,
        resource_type=args.resource_type,
        resource_id=args.resource_id,
        payload=payload,
        tenant_id=args.tenant_id,
        max_attempts=args.max_attempts,
        timeout_seconds=args.timeout,
    )
    print(
        f"已入队：job id={job.id} kind={job.kind} "
        f"resource={job.resource_type}#{job.resource_id} status={job.status} "
        f"max_attempts={job.max_attempts}"
    )


async def _job_status(container: Container, args: argparse.Namespace) -> None:
    job = await container.job_service.get(args.id)
    print(
        f"job id={job.id} kind={job.kind} status={job.status} "
        f"attempt={job.attempt}/{job.max_attempts} "
        f"lease_owner={job.lease_owner} error={job.error_message}"
    )
    for event in await container.job_service.events(args.id):
        print(f"  #{event.sequence} [{event.event_type}] {json.dumps(event.payload, ensure_ascii=False)}")


async def _cancel_job(container: Container, args: argparse.Namespace) -> None:
    job = await container.job_service.cancel(args.id)
    print(f"job id={job.id} status={job.status} cancel_requested_at={job.cancel_requested_at}")


def _parse_pdf(args: argparse.Namespace) -> int:
    """只做「PDF → Markdown」，**不碰数据库**（所以不走容器、不连库）。

    默认输出到原文件旁的 ``<原名>.extracted.md``——和索引时自动写的那份是同一个位置、
    同一个后缀，所以这个命令既能"提前看看解析效果"，也能"补一份当时没写的"。

    目标已存在时**拒绝覆盖**：这个命令是拿来看解析效果的，不是拿来覆盖文件的。
    """
    from app.backend.src.documents.extraction import extract_text
    from app.backend.src.documents.storage import SIDECAR_SUFFIX

    source = Path(args.path)
    if not source.is_file():
        print(f"失败：文件不存在 —— {source}", file=sys.stderr)
        return 1

    target = (
        Path(args.out)
        if args.out
        else source.with_name(f"{source.stem}{SIDECAR_SUFFIX}")
    )
    if target.exists():
        print(f"失败：目标已存在，不覆盖 —— {target}", file=sys.stderr)
        return 1

    text = extract_text(source.read_bytes(), filename=source.name)
    target.write_text(text, encoding="utf-8", newline="")  # 保持 \n，见 storage.write_sidecar
    headings = sum(1 for line in text.splitlines() if line.startswith("#"))
    print(f"已解析：{source.name}")
    print(f"  输出：{target}")
    print(f"  {len(text)} 字符，{headings} 个标题")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.backend.src.cli", description="FaDia 本地运维 CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    tenant = sub.add_parser("create-tenant", help="创建租户")
    tenant.add_argument("--code", required=True, help="租户编码（唯一）")
    tenant.add_argument("--name", required=True, help="租户名称")
    tenant.add_argument("--region")
    tenant.add_argument("--address")
    tenant.add_argument("--contact", help="联系人")
    tenant.add_argument("--phone", help="联系电话")

    user = sub.add_parser("create-user", help="创建账号")
    user.add_argument("--username", required=True)
    user.add_argument("--password", help="不传则交互输入")
    user.add_argument("--role", default="operator", choices=["operator", "engineer", "admin"])
    user.add_argument("--tenant-id", type=int, dest="tenant_id", help="operator 必填")
    user.add_argument("--real-name", dest="real_name")
    user.add_argument("--email")

    job = sub.add_parser("enqueue-job", help="入队一个后台任务（调试用）")
    job.add_argument("--kind", required=True, choices=["diagnosis", "document_index"])
    job.add_argument(
        "--resource-type",
        required=True,
        dest="resource_type",
        choices=["diagnosis_task", "document"],
    )
    job.add_argument("--resource-id", required=True, type=int, dest="resource_id")
    job.add_argument("--tenant-id", type=int, dest="tenant_id")
    job.add_argument("--payload", help="JSON 字符串，透传给 handler")
    job.add_argument("--max-attempts", type=int, default=3, dest="max_attempts")
    job.add_argument("--timeout", type=int, default=900, help="秒")
    job.add_argument(
        "--fail-first", action="store_true", dest="fail_first",
        help="占位 handler 首次尝试故意失败，用于观察退避重试",
    )

    status = sub.add_parser("job-status", help="查看某个后台任务与事件")
    status.add_argument("--id", required=True, type=int)

    cancel = sub.add_parser("cancel-job", help="取消某个后台任务")
    cancel.add_argument("--id", required=True, type=int)

    parse_pdf = sub.add_parser("parse-pdf", help="把 PDF 解析成 Markdown 看效果（不碰库）")
    parse_pdf.add_argument("--path", required=True, help="源 PDF 路径")
    parse_pdf.add_argument("--out", help="输出路径（默认：原文件旁 <原名>.extracted.md）")
    return parser


async def _run(args: argparse.Namespace) -> None:
    container = Container(load_settings())
    try:
        if args.command == "create-tenant":
            await _create_tenant(container, args)
        elif args.command == "create-user":
            await _create_user(container, args)
        elif args.command == "enqueue-job":
            await _enqueue_job(container, args)
        elif args.command == "job-status":
            await _job_status(container, args)
        else:
            await _cancel_job(container, args)
    finally:
        await container.aclose()


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "parse-pdf":
        return _parse_pdf(args)  # 纯本地文件操作，不必启动容器
    try:
        asyncio.run(_run(args))
    except (UserError, TenantError, JobError) as exc:
        print(f"失败：{exc.message}（{exc.code}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
