# 项目交接文档 v2：企业知识库 + 工单 Agent（全新项目）

> 用途：新会话启动时先读本文件，即可获得完整项目背景与约束。
> 更新日期：2026-10-30

---

## 0. 一句话背景

大二学生，目标北京大厂 Agent/LLM 应用实习（2027 寒假投递，简历 12 月底定稿），做一个**全新、不依赖 Hello-Agents 框架**的生产级项目：企业知识库 + 工单 Agent。要求能上线、能跑通、能演示，不是 demo。

---

## 1. 用户画像

| 项 | 内容 |
|---|---|
| 身份 | 大二（2029 届），北京大厂日常实习 |
| 投递 | 2027 年 1-2 月（寒假）；简历定稿 **2026 年 12 月底** |
| 技术基础 | All-in-RAG ✅、Hello-Agents 8-12 章 ✅、**力扣 100 题**（继续刷）、**FastAPI 在学** |
| 沟通偏好 | **逐行代读**、**不要"你项目里有 XX"式指路**、**不迎合要有判断力**、**面试导向** |

---

## 2. 项目决策（已锁定）

- ✅ **全新项目，零 hello_agents 依赖**——自己搭 FastAPI 分层 + 自己的 RAG/Agent 循环
- ✅ **Embedding：本地 BGE（用 `fastembed`，不用 torch）**
- ✅ **前端：Gradio**
- ❌ 不做：Hello-Agents 第 13/15 章、Pi、TradingAgent、ai-agent-book 评估、SFT/GRPO 训练、用户系统/支付/多租户/K8s

---

## 3. 技术选型

| 层 | 选型 |
|---|---|
| Web | **FastAPI + Pydantic + Uvicorn** |
| LLM | **openai SDK**（base_url → https://api.deepseek.com） |
| Embedding | **fastembed**（`BAAI/bge-small-zh-v1.5`，onnxruntime，无需 torch） |
| 向量库 | **Qdrant**（Docker：`docker run -p 6333:6333 qdrant/qdrant`） |
| 文档解析 | **MarkItDown** |
| 分块/检索/Agent/评估 | **自己写**（核心贡献点） |
| 前端 | **Gradio** |

---

## 4. 目录结构

```
E:\PythonProject3\kb-agent\
├── .venv/                    # 独立 venv（隔离依赖）
├── app/
│   ├── main.py               # FastAPI 入口（lifespan 管理 Qdrant 连接）
│   ├── core/                 # config / logging / exceptions
│   ├── api/                  # documents / qa / tickets / evaluation 路由
│   ├── services/             # 纯业务逻辑（可单测）
│   │   ├── ingest.py         # 解析 → 分块 → 嵌入 → 入库
│   │   ├── retrieval.py      # 向量检索 + 过滤
│   │   ├── generation.py     # RAG 生成（带引用）
│   │   ├── ticket.py         # 工单生成（分类/优先级/摘要）
│   │   └── evaluation.py     # 召回率/准确率/延迟/成本
│   ├── models/               # Pydantic schema（请求/响应）
│   └── infra/                # LLM / Embedding / Qdrant 客户端封装
├── tests/                    # pytest
├── scripts/                  # 评估脚本、数据导入
├── docker-compose.yml        # qdrant（+app）
├── requirements.txt
├── .env.example
└── README.md
```

---

## 5. 依赖清单

```
fastapi
uvicorn[standard]
pydantic-settings
python-multipart
openai
qdrant-client
fastembed
markitdown
httpx
pytest
gradio
```

> 安装前先 dry-run 检查：**不升级 numpy、不动 mcp、不装 fastmcp**（保护主环境）。

---

## 6. 环境约束

| 项 | 状态 |
|---|---|
| 项目路径 | `E:\PythonProject3\kb-agent\`（独立 venv） |
| .env | 复用主项目：`LLM_API_KEY` / `LLM_MODEL_ID=deepseek-flash` / `LLM_BASE_URL=https://api.deepseek.com` / `SERPAPI_API_KEY` |
| **主环境（不要动）** | numpy 2.5.3、mcp 1.16.0、不装 fastmcp、hello-agents 0.2.9 |
| Qdrant | Docker 起本地服务（6333 端口） |
| 删除策略 | **任何删除操作前必须征得用户同意** |
| 缓存 | pip/HF 已迁 E:\DevCaches；下载模型会落到 E 盘 |

---

## 7. 第一步：最小闭环（2-3 小时）

```
POST /documents/upload  → PDF → MarkItDown → 分块 → fastembed → Qdrant
POST /qa/ask            → 提问 → 检索 → 带引用回答
GET  /health            → 健康检查
```

**验收**：上传一份 PDF → 问文档里的问题 → 回答**带出处（页码/段落）**。

**实现顺序**：
1. `core/config.py`（读 .env）→ `infra/`（LLM/Embedding/Qdrant 客户端）
2. `services/ingest.py` → `services/retrieval.py` → `services/generation.py`
3. `api/documents.py` + `api/qa.py` → `main.py`
4. 起 Qdrant → 跑通验收

---

## 8. 时间表（到 12 月底）

| 时间 | 任务 |
|---|---|
| 11 月第 1 周 | 最小闭环（上传+问答带引用） |
| 11 月第 2 周 | 评估：50 条评估集 + 召回率/延迟/成本 |
| 11 月第 3 周 | 工单 Agent（分类/优先级/摘要） |
| 11 月第 4 周 | 优化（标题分块/混合检索）+ 前后对比 |
| 12 月第 1-2 周 | Docker 部署 + 日志/限流 + 录屏 |
| 12 月第 3-4 周 | 简历 + 面试题 + 架构分析 |

---

## 9. 新会话第一句话（复制给新 AI）

> "先读 E:\PythonProject3\kb-agent\PROJECT_HANDOFF.md，然后我们从最小闭环开始：FastAPI + 文档上传 + 分块 + fastembed + Qdrant + 带引用问答。要求逐行讲解、不要指路式引用、面试导向、每步真实跑通。"

---

## 10. 给新 AI 的沟通要求

1. **逐行代读**代码，基础概念随讲随解释；
2. **不要**说"你项目里有 XX 实现"——直接讲透；
3. **不迎合**：计划有问题直接指出，给具体修正；
4. **每步验收**：写完必须真实跑通（不允许"理论上可以"）；
5. **面试导向**：每个功能附带"面试怎么问/怎么答"。
