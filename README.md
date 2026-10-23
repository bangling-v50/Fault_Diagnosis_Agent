# FaDia Agent

面向工业设备售后运维的故障诊断助手。

工控机上报设备告警后，系统创建诊断任务，由后台 worker 执行诊断流程并生成报告；维修人员依据报告处理设备，工单结案后沉淀为案例，案例回流知识库，供后续诊断检索。

## 功能

- 多租户与角色权限：`operator` / `engineer` / `admin`
- 设备管理、设备上报令牌、告警接收与合并
- 自动故障诊断：检索知识库、历史告警、历史工单，生成结构化报告
- 诊断步骤、证据、报告全量落库，支持查询与回放
- 维修工单：建单、派单、结案
- 案例沉淀与知识库回流
- 知识库：PDF、Markdown、TXT 解析，父子块切分，向量化
- 诊断助手：SSE 流式对话，支持工具调用与长期记忆
- 前端管理界面：概览、设备、告警、诊断、工单、知识库、聊天、用户与租户

## 技术栈

- 后端：FastAPI、SQLAlchemy 2（异步）、asyncpg
- 数据库：PostgreSQL 16 + pgvector
- 对象存储：MinIO
- Agent：LangChain、LangGraph
- 模型：Qwen（DashScope OpenAI 兼容接口）
- 前端：Vue 3、Vite、Element Plus

## 内部流程

```
工控机上报
   │
   ▼
告警落库 ──> 创建诊断任务 ──> 后台 worker 领取
                                  │
                                  ▼
                         LangGraph 诊断流程
                         ├─ 检索知识库
                         ├─ 查询同设备、同告警码的历史告警
                         ├─ 查询同设备、同告警码的历史工单
                         ├─ 生成诊断报告
                         └─ 评估报告 ──不通过──> 重新生成（最多 2 版）
                                  │
                                  ▼
                          报告与证据落库
                                  │
                          创建工单 ──> 结案 ──> 沉淀案例 ──> 回流知识库
```

诊断流程中，三个数据查询步骤按固定顺序执行，报告由模型生成，评估器负责字段校验、引用校验和内容评审。

知识库采用父子块切分：父块为章节，仅存储；子块为段落或表格行，带标题路径参与检索。查询时向量召回与 BM25 召回经 RRF 融合，再做精排。

后台任务基于数据库表实现，包含租约、心跳、超时、退避重试、取消和崩溃恢复。

## 快速开始

环境要求：Docker、Python 3.10+、Node 20.19+。

**1. 启动 PostgreSQL 与 MinIO**

```bash
docker compose -f infra/compose.yaml up -d postgres minio
```

**2. 准备配置**

```bash
cp config/project.template.json config/project.json
cp config/user.project.template.json config/user.project.json
```

在 `config/user.project.json` 中填写 `llm.apiKey`。

**3. 安装依赖并初始化数据库**

```bash
pip install -e ".[dev]"
alembic -c alembic_src.ini upgrade head
```

**4. 启动后端**

```bash
python -m uvicorn app.backend.src.main:create_app --factory --host 127.0.0.1 --port 8000
```

接口文档：http://127.0.0.1:8000/docs

**5. 启动前端**

```bash
cd app/frontend
npm install
npm run dev
```

访问 http://localhost:5174 。

**6. 创建初始账号**

```bash
python -m app.backend.src.cli create-tenant --code acme --name "ACME"
python -m app.backend.src.cli create-user --username admin --role admin
```

`create-user` 未提供 `--password` 时会交互式输入。其他命令可通过 `--help` 查看参数。

## 目录结构

```
app/
  backend/src/
    agents/          诊断流程：状态、提示词、解析、评估、报告渲染
    background_jobs/ 后台任务 runtime 与 handler 注册
    documents/       文件存储、PDF 转 Markdown、文本切分
    llm_provider/    聊天、向量、精排模型客户端
    models/          数据库模型
    repositories/    数据访问层
    retrieval/       BM25 与 RRF 融合
    routers/         HTTP 接口
    schemas/         请求与响应结构
    services/        业务编排
    tools/           Agent 工具
    vector_store/    pgvector 读写
    utils/           鉴权、错误、令牌
    cli.py           命令行工具
    containers.py    依赖装配
    main.py          FastAPI 入口
    settings.py      配置加载
  frontend/          Vue 3 前端
alembic_src/         数据库迁移
config/              配置模板
infra/               Docker Compose：PostgreSQL 与 MinIO
```

## 说明

- 登录令牌与设备上报令牌仅存储 SHA-256 哈希。
- 角色分为 `operator`、`engineer`、`admin` 三种。
- 文档支持 `.pdf`、`.md`、`.txt`。
- `config/project.json` 与 `config/user.project.json` 属于本地配置，不纳入版本管理。
