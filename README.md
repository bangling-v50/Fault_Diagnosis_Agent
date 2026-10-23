# FaDia Agent

面向工业设备售后运维的故障诊断助手。

设备出故障后，工控机把告警上报过来，系统自动创建诊断任务，后台跑诊断流程、出报告；维修人员照着报告处理，结案后把这次故障沉淀成案例，案例再回流到知识库。下次遇到同样的问题，答案就在里面。

## 技术栈

- **后端**：FastAPI、SQLAlchemy 2（异步）、asyncpg
- **数据库**：PostgreSQL 16 + pgvector（向量和业务数据放同一个库）
- **文件存储**：MinIO，走 S3 兼容协议，换阿里云 OSS / AWS S3 只改配置
- **Agent**：LangChain + LangGraph，模型走 DashScope 的 OpenAI 兼容接口（Qwen）
- **后台任务**：数据库表驱动的 worker，没有引入 MQ
- **前端**：Vue 3 + Vite + Element Plus

## 能做什么

- **登录与租户隔离**：`operator`（租户操作员）、`engineer`（公司售后）、`admin`（平台管理员）。操作员只能看本租户数据。
- **设备与告警**：设备上报令牌、告警接收、重复告警合并。
- **故障诊断**：后台自动执行，产出结构化诊断报告，包含可能原因、处理建议、风险等级和引用证据。过程和每一步的证据都落库，可回放。
- **维修工单**：从诊断报告建单、派单、结案。结案要求填写根因和处理方案。
- **案例沉淀**：已结案工单可以固化成案例，自动生成知识库文档。
- **知识库**：上传 PDF / Markdown / TXT，解析成 Markdown、切分、向量化。PDF 有自己写的解析器，能还原标题层级、表格和跨页段落。
- **诊断助手**：多轮聊天，SSE 流式输出，能调知识库、告警、工单、诊断记录几个工具，支持长期记忆。

## 内部是怎么跑的

主链路：

```
工控机上报 ──> 告警落库 ──> 建诊断任务 ──> 后台 worker 领取
                                              │
                                              ▼
                                   LangGraph 诊断流程
                                   ├─ 检索知识库
                                   ├─ 查同设备、同告警码的历史告警
                                   ├─ 查同设备、同告警码的历史工单
                                   ├─ 写报告
                                   └─ 评估报告 ──不合格──> 带着问题重写（最多 2 版）
                                              │
                                              ▼
                                     报告落库，可查询、可回放
                                              │
                                   建工单 ──> 结案 ──> 沉淀案例 ──> 回流知识库
```

几个设计上的选择：

- 诊断流程是**手搭的 LangGraph 图**，不是 `create_agent`。三个工具按固定顺序调用，模型不参与"调不调、调哪个"，结果是可预测的。
- 评估器先做硬校验（字段齐整、枚举合法、引用编号不越界），过了再让模型评审一次。评审失败不会让整个任务失败。
- 知识库用**父子块**：父块是一个 `##` 小节，只存不索引；子块是段落或表格行，参与检索。子块正文开头带着标题路径，命中后能找回完整上下文。
- 检索是向量 + BM25 混合，用 RRF 融合后 rerank。向量召回有相似度下限，低于下限宁可返回"没查到"。
- 后台任务有租约、心跳、超时、指数退避重试、取消和崩溃恢复，逻辑都在 `background_jobs/runtime.py`。

## 本地跑起来

需要 Docker、Python 3.10+、Node 18+。

**1. 起 PostgreSQL 和 MinIO**

```bash
docker compose -f infra/compose.yaml up -d postgres minio
```

**2. 准备配置**

```bash
cp config/project.template.json config/project.json
cp config/user.project.template.json config/user.project.json
```

然后在 `config/user.project.json` 里填上模型服务的 `apiKey`。这两个文件已被 `.gitignore` 忽略，不会进版本库。

**3. 装依赖、建表**

```bash
pip install -e ".[dev]"
alembic -c alembic_src.ini upgrade head
```

**4. 启动后端**

```bash
python -m uvicorn app.backend.src.main:create_app --factory --host 127.0.0.1 --port 8000
```

接口文档在 http://127.0.0.1:8000/docs 。

**5. 启动前端**

```bash
cd app/frontend
npm install
npm run dev
```

打开 http://localhost:5174 。

**6. 建第一个账号**

```bash
python -m app.backend.src.cli create-tenant --code acme --name "ACME"
python -m app.backend.src.cli create-user --username admin --role admin
```

`create-user` 不传 `--password` 会交互式输入。其余命令用 `--help` 看。

## 目录

```
app/
  backend/src/
    agents/          诊断流程的 LangGraph 图：状态、提示词、解析、评估、报告渲染
    background_jobs/ 后台任务 runtime 和 handler 注册
    documents/       文件存储、PDF 转 Markdown、切分
    llm_provider/    聊天 / 向量 / 精排模型客户端
    models/          数据库模型
    repositories/    数据访问层
    retrieval/       BM25 与 RRF 融合
    routers/         HTTP 接口
    schemas/         请求与响应结构
    services/        业务编排
    tools/           给 Agent 用的工具
    vector_store/    pgvector 读写
    utils/           鉴权、错误、令牌
    cli.py           命令行工具
    containers.py    组合根，启动时装配所有单例
    main.py          FastAPI 入口
    settings.py      配置加载
  frontend/          Vue 3 前端
alembic_src/         数据库迁移
config/              配置模板（真实配置不入库）
infra/               docker compose：PostgreSQL + MinIO
```

## 说明

- 数据库里的时间统一用 `TIMESTAMPTZ`，主键是 `BIGINT IDENTITY`，约束尽量写在库上（CHECK / UNIQUE / 外键）。
- 登录取的 bearer token 和设备上报令牌都只存 SHA-256，明文不落库。
- 角色只有三个：`operator` / `engineer` / `admin`。
- 文档目前支持 `.pdf`、`.md`、`.txt`。扫描件和图片还没有走视觉模型。
