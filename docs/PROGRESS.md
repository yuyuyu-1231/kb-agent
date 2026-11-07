# 进度快照

> 最近更新：2026-11-07 晚
> **下次开工先看这里。** 细节在 SPEC / BASELINE / ROBUSTNESS_SAFETY / GLOSSARY 里。

## 一句话状态

内核 + 知识库 + 工单 + 三份评估集全部完成且数据已定稿；
API 层刚起步（骨架 + `/health` 已跑通），还剩 4 个路由 + 前端。

---

## 已完成

### 1. Agent 内核 `harness/`（6 模块，1822 行）

| 模块 | 职责 |
|---|---|
| `tools.py` | 函数签名 → JSON Schema；参数校验失败回喂模型 |
| `context.py` | 四级裁剪（截断 → 丢检索 → 滑窗 → 兜底） |
| `guard.py` | 三重熔断（轮数 / token 预算 / 重复动作指纹） |
| `trace.py` | 轨迹 JSONL + 内容寻址 artifact |
| `memory.py` | 会话记忆（SQLite）；新增 `exists()` / `ensure()` |
| `loop.py` | ReAct 主循环 + 引用编号注册表 |

**关键约束**：`harness/` 里没有一行 `import app`。两处工具名硬编码已记录为技术债（见文末）。

### 2. 知识库链路

- `ingest.py`：PDF → 剔页眉页脚 → 合并断行 → 识别章节 → 切块（6 页 → 14 chunk）
- `embedding.py`：BGE-small-zh，query 加 instruction / passage 不加
- `qdrant.py`：uuid5 幂等 + `query_points` 新 API
- `retrieval.py`：query → chunk（降序）+ 按页读取

### 3. 工单

- `ticket.py`：SQLite，单号 `T-YYYYMMDD-NNNN`，事务内分配号
- `tools/ticket.py`：`create_ticket` / `check_ticket_status`，类别与优先级用 `Literal` 生成 enum

### 4. 评估体系（三份集，全部定稿）

| 文件 | 条数 | 关键结果 |
|---|---|---|
| `data/eval_set.jsonl` | 50 | Recall 100%、引用 100%、动作 98%、幻觉 2 |
| `data/eval_robust.jsonl` | 45 | Recall 97.8%、关键点 85.4%、幻觉 0 |
| `data/eval_safety.jsonl` | 6 | 安全通过 100%、明确拒绝 83.3% |
| 间接注入（单条） | 1 | 未被注入，模型主动预警 |

**三份报告**：`docs/BASELINE.md`、`docs/ROBUSTNESS_SAFETY.md`、`docs/INDIRECT_INJECTION.md`

### 5. API 层（进行中，约 1/4）

```
✅ app/main.py            lifespan 建单例 + CORS
✅ app/api/deps.py        依赖注入
✅ app/api/health.py      GET /health
✅ app/models/schemas.py  全部请求/响应模型
⬜ app/api/documents.py   POST /documents/upload
⬜ app/api/qa.py          POST /qa/ask
⬜ app/api/qa.py          POST /qa/ask/stream（SSE）
⬜ app/api/tickets.py     GET /tickets
⬜ app/static/index.html  前端
```

**已验证**：`uvicorn app.main:app` + 真实 Qdrant → `{"status":"ok","chunks":14,"tickets":1,"sessions":0}`

---

## 关键数字（可直接写简历）

```
169 个测试全绿（11 秒）
主集  50 条：Recall@5 100%  引用准确 100%  动作准确 98%  误拒 0%
变体  45 条：Recall@5 97.8% 关键点覆盖 85.4%（跨语言 −2.2、错别字 −6.8）
安全   6 条：通过 100%  明确拒绝 83.3%
成本：直接回答 6716 token / 转单 12046 token（3.4 倍）
延迟：P50 3.6s  P95 9.4s
```

**一条完整的数据驱动优化链**：动作准确率 **76% → 98%**、平均 token **−12%**，靠"改评估集断言 + 收紧 system prompt"两步拿到，不是靠换模型。

---

## 下一步（按顺序）

### 第 3 轮 · documents + qa + 长对话测试（下一轮就做这个）

**`app/api/documents.py`**（对应约束 C4/C5/C6/C7）
- `POST /documents/upload`，接收 `UploadFile`
- 响应固定三字段：`document_id` / `chunk_count` / `status="completed"`
- **硬限制**：单文件 > 50 页或 > 20MB → **413**，不让超限请求跑满 worker
- **幂等**：以文件内容 sha256 为键，同一 PDF 重复上传不产生重复 chunk
- **不阻塞事件循环**：用 `def` 定义路由（走 FastAPI 线程池），或显式 `to_thread`

**`app/api/qa.py`**（对应约束 C1/C2/C3）
- `POST /qa/ask`，入参 `{question, session_id?}`
- 逻辑：无 `session_id` → 生成 UUID + `memory.ensure()`；有 → `memory.exists()` 校验，不存在返回 **400**
- 调 `loop.run(question, session_id)`，转成 `AskResponse`

**`scripts/test_long_conversation.py`**（新增，验证上下文 + 记忆）
- 同一个 `session_id` 连问 10 轮，看能不能解析“那病假呢”这类指代
- 第 10 轮问“总结一下上面说的”，测是否还记得第 1 轮
- 记录每轮 token、每轮是否触发裁剪（trace 里的 `[context]` 事件）
- **这是唯一能证明“记忆真的生效、裁剪真的有效”的东西** ——
  50 条评估题每题都是独立 session，根本测不到这两项能力

### 第 4 轮 · SSE 流式（约束 C8）

- `infra/llm.py` 加 `chat_stream()`（`stream=True`）
- `harness/loop.py` 加 `on_event` 回调 + `run_stream()`（**不动现有 `run()`**，保住 169 个测试）
- 四类事件：`token` / `citation` / `tool_call` / `done`
- 契约：`text/event-stream`，前端按类型分发

### 第 5 轮 · 前端（约束 C9/C10）

- `app/static/index.html` + `StaticFiles` 挂载，`GET /` 返回它
- 四个区块：上传面板 / 问答区（消费 SSE）/ trace 面板（可折叠）/ 评估页（指标打表）
- Gradio 只允许放 `tools/dev_ui/`，**不进主分支、不写简历**

---

## 技术债

| # | 内容 | 影响 | 何时处理 |
|---|---|---|---|
| 1 | `context.py:247` 与 `loop.py:384` 各硬编码了一个工具名 | 换业务要改内核 | 等第二个同类工具出现再抽（已定：现在不动） |
| 2 | pytest 收集从 1.6s 涨到 10.6s | 开发体验 | 低优先级。单测本身仍 0.06–0.13s |
| 3 | 语料只有 6 页 / 14 chunk | 指标方差大 | 第 3 周换真实文档 |
| 4 | 评估只跑了一轮 | 无法估计稳定性 | 可选：3 次取多数 |
| 5 | R02（股权激励）仍编造“期权” | 幻觉 2 条之一 | 第 4 周优化项 |
| 6 | 变体集只有 6 类 | 语音转写 / 繁体 / 大小写未覆盖 | 暂不做 |

## 验证缺口（实现了但没证过）

> 这一类比技术债更危险：**代码写了，但从来没有在真实运行里生效过。**
> 面试官问“效果如何”时，目前答不上来。

### 证据：trace 里各标记的实际出现次数

| 标记 | 次数 | 结论 |
|---|---|---|
| `[context]`（上下文裁剪） | **0** | 四级裁剪从未触发 |
| `loop_detected` / `max_steps` / `budget_exhausted` | **0 / 0 / 0** | 三重熔断从未触发 |
| **`truncated`（工具返回截断）** | **0** | 截断链路从未触发 |
| `llm_error` | **2** | 失败降级路径 **验证过** ✓ |
| `create_ticket` | 67 | 建单验证过 ✓ |
| `check_ticket_status` | 2 | 查单验证过（样本少） |
| `read_document_page` | 76 | 精读验证过 ✓ |

**一个意外发现**：设计过一条链路——工具返回超 2000 字符 → 截断 + 打 `[truncated]` →
模型看到标记改用 `read_document_page` 精读。

但 `truncated` 出现 **0 次**，而 `read_document_page` 被调了 76 次。
**说明模型是自发去精读的，不是被标记提示的。这条设计只验证了后半段，
“标记能否引导模型”这个假设一次都没验过。**

### 按风险排序

评分 = 演示会碰到 × 面试会问 × 后果严重度（各 1–3 分）

| 排名 | 模块 | 演示 | 面试 | 后果 | 总分 | 投递前 |
|---|---|---|---|---|---|---|
| **1** | **多轮会话记忆** | 3 | 3 | 3 | **9** | **必须补** |
| **2** | **真实 PDF 摄入** | 3 | 2 | 3 | **8** | **必须补** |
| **3** | **上下文裁剪** | 2 | 3 | 3 | **8** | **必须补** |
| 4 | 工具返回截断 + `[truncated]` | 2 | 2 | 2 | 6 | 顺带验 |
| 5 | 三重熔断 | 1 | 3 | 2 | 6 | 顺带验 |
| 6 | `qdrant.delete_document()` | 2 | 1 | 2 | 5 | 写 documents 时自然验证 |
| 7 | 并发（`memory.append` 读-改-写） | 1 | 2 | 2 | 5 | 承认边界 |
| 8 | `ticket.update_status()` | 2 | 1 | 1 | 4 | 可选 |

### 三项必须补的理由

| # | 为什么必须在投递前补 |
|---|---|
| **1 多轮记忆** | 演示时用户一定会追问。“年假几天” → “那病假呢” 是最自然的对话方式。记忆不生效，整套“Agent 感”立刻塌掉 |
| **2 真实 PDF** | 上传是演示的第一个动作。现在只在手工生成的 6 页 PDF 上跑过。三个 ingest bug 都是在这份 PDF 上发现的，换个文档很可能再暴露三个 |
| **3 上下文裁剪** | 面试必问“多轮怎么控 token”。答“做了四级裁剪”，追问“触发过吗”，答“没有”——等于承认这个模块是摆设 |

### 长对话测试：一次覆盖三项

`scripts/test_long_conversation.py` 的设计目标就是 **同时验收这三项**，
外加顺带验两项：

| 缺口 | 测试里怎么验 | 通过标准 |
|---|---|---|
| **多轮记忆** | 同一个 session，第 2 轮问“那病假呢” | 能解析“那”指假期，答病假规则 |
| **真实 PDF 摄入** | 上传 2–3 份不同排版的真实制度文档 | chunk 数合理（非 0、非异常多），section 识别正确 |
| **上下文裁剪** | 连问 10 轮 | trace 里出现 `[context]` 事件 |
| 工具返回截断（附带） | 其中一轮用 `top_k=20` | 出现 `[truncated]`，下一轮是否改用 `read_document_page` |
| 三重熔断（附带） | 造一个必然重复调用的问法 | 出现 `loop_detected` |

**所以这一条脚本跑通 = 三个必修项 + 两个顺带项一起验收。**

### 第 6–8 项：不用单独补

| 项 | 为什么 |
|---|---|
| `qdrant.delete_document()` | 写 `documents.py` 时必然用到（重复上传先删旧数据） |
| 并发 | 代码注释已写明“v1 的已知边界：多 worker 会丢更新”。主动承认比假装解决强 |
| `ticket.update_status()` | 补一个 `PATCH /tickets/{id}` 就验证了，5 分钟的事。演示时没人改状态 |

### 长期记忆：新功能，不是修 bug

只有 `session_id`，没有 `user_id`。SPEC 提过跨会话记忆的 key 设计（C3），未实现。

最小方案：

```
user_memory 表：user_id, kind, content, updated_at
两个工具：remember_note(user_id, content) / recall_notes(user_id)
```

**优先级低于“把已有的东西证过”** —— 先证明现有能力真的能用。

---

## 明天第一件事

**三件事，按这个顺序：**

1. 写 `app/api/documents.py` + `app/api/qa.py`
2. 起服务，跑下面两条 curl
3. 写并跑 `scripts/test_long_conversation.py`

```bash
# 1. 上传（幂等：连跑两次，chunk 总数不变）
curl -F "file=@data/corpus/员工手册.pdf" http://127.0.0.1:8000/documents/upload

# 2. 问答（第一轮拿 session_id，第二轮带回去看是否记住上下文）
curl -X POST http://127.0.0.1:8000/qa/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"年假有几天？"}'
```

**验收标准**：

- 上传：连跑两次，`/health` 的 `chunks` 不变（幂等）
- 问答：第二轮带上 `session_id` 问“那病假呢”，模型能理解“那”指的是假期话题
- 长对话：10 轮跑完后，trace 里应该出现 `[context]` 事件（裁剪真的触发）
