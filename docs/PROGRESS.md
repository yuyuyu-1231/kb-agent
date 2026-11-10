# 进度快照

> 最近更新：2026-11-08 晚
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

### 第 3 轮 · documents + qa + 长对话测试 + 工具扩展（进行中）

**进度：**

| 顺序 | 任务 | 状态 |
|---|---|---|
| 1 | `documents.py` + `qa.py` | ✅ **已完成（11/8）** |
| 2 | `test_long_conversation.py` | ✅ **已完成（11/8）** |
| 2.5 | 三重熔断验证（`test_breaker.py`） | ✅ **已完成（11/8）** |
| 3 | 工具扩展 + 重跑主集 | ✅ **已完成（11/8）** |

**第 1 项验收结果（11/8 实测）：**

| 验收项 | 结果 |
|---|---|
| 上传响应三字段 | ✅ `{document_id, chunk_count, status}` |
| 幂等（连传两次） | ✅ 两次都返回 14 chunk，`/health` 不变 |
| `content_hash` 落库 | ✅ `387ea6e8...` |
| 文档列表 | ✅ 1 份、14 chunk、6 页 |
| **会话记忆** | ✅ 第 2 轮问“那病假呢”，答“病假没有转正限制…**与之不同，年休假则明确规定需转正后才能申请**” |
| 无效 session_id | ✅ 400 + `session_id 无效: fake-id-123。不传该字段可新建会话。` |
| 不存在的文档 | ✅ 404 |

> **“第 2 轮”那句是第一次证明多轮记忆真的生效。**
> 之前 50 条评估题每题都是独立 session，这项能力从未被验证过。

**第 2 项验收结果（11/8 实测，12 轮 × 2 组）：**

| 阶段 | 裁剪预算 | 峰值 token | 首次裁剪 | 截断 | 末轮记忆 |
|---|---|---|---|---|---|
| A | 30000（真实） | 27592 | **未触发** | 0 轮 | 记得 |
| B | 6000（模拟） | 16101 | **第 1 轮** | **12 轮全部** | 记得 |

**结论：**

- **裁剪逻辑正确**：预算 6000 时 12 轮每轮都裁，不崩、不丢关键信息
- **真实预算对 12 轮对话绰绰有余**（峰值 27592 < 30000）
- 末轮问“总结假期规则”，两组都答得出 5/10/15 天 →
  **即使历史被裁，模型靠工具重新检索也能兜住**

**一个附带发现**：三份不同排版的语料（双栏 / 带页眉页脚 / 无结构长文）
都正常入库（5 / 6 / 17 个 chunk），但**页眉页脚剔除都是 0 块**。

原因不是 bug：`detect_margin_keys` 要求 `total_pages >= 3` 才启动检测，
而变体每份只有 2 页。**这是正确行为**（页数太少无法判断“重复”），
但意味着：要验证页眉页脚剔除，语料至少要有 3 页。

**第 3 项验收结果（11/8，用剧本化假模型）：**

| 场景 | 构造 | stop_reason | 步数 |
|---|---|---|---|
| A 重复动作 | 一次返回 2 个参数相同的 tool_calls | `loop_detected` | 1 |
| B 轮数超限 | 每轮都调工具、永不收敛 | `max_steps` | 3 |
| C 预算耗尽 | 一次响应烧穿 50 token 预算 | `budget_exhausted` | 1 |

**`_pad_skipped_tool_calls` 第一次被执行**（之前从未跑过）：
补 1 条占位消息，`tool_call_id=c2`，内容 `{"error": "已熔断，本次工具调用未执行"}` ✅

**验证过程中发现一个真 bug（已修）：**

`loop.py` 里有 `used_tokens = max(used_tokens, fit.estimated_tokens)`，
把「当前上下文**大小**」混进了「**累计消耗**」。后果：`token_budget=50` 时，
光 system prompt 就 400 token，于是 **第 0 轮就熔断，steps=0，模型一次都没被调用**。

已删掉该行并加注释。修复后 C 场景 steps 从 0 变成 1，169 个测试仍全过。

> **这个 bug 说明**：不真跑一遍，“预算耗尽”这条路径的行为就是猜的。
> 写测试的价值不在“证明对”，在“逼出错”。

**第 4 项验收结果（11/8，4 工具 → 6 工具）：**

新增 `list_documents`（列已收录文档）和 `current_time`（补时间幻觉）。

| 运行 | 动作准确 | 关键点覆盖 | 幻觉 | 应拒答 | 平均 token | P95 |
|---|---|---|---|---|---|---|
| 4 工具（11/7） | 98.0% | 92.2% | 2 | 80% | 6716 | 9363ms |
| 6 工具（14:19） | 98.0% | 83.3% | 3 | 80% | 6885 | 8772ms |
| 6 工具（14:24） | 98.0% | 86.5% | 4 | 100% | 7674 | 10349ms |

**结论 1：动作准确率完全没掉**（三次都是 98.0%）。
新工具被真实调用了（`list_documents` 12 次、`current_time` 6 次），
不是“加了没用”，而是“加了没添乱”。

**结论 2（更重要）：其他指标的差异淹没在方差里。**

同为 6 工具的两次运行：
- 关键点覆盖 83.3% vs 86.5%（差 3.2 点）
- 应拒答 80% vs 100%（差 20 点 —— 只有 5 条题，1 条不同就是 20 点）
- 平均 token 6885 vs 7674（差 789）
- 幻觉 3 vs 4（差 1 条）

**所以「4 工具 92.2% → 6 工具 83.3%，掉了 8.8 点」这个结论不成立** ——
6 工具自己的两次就差了 3.2 点。

**方法论结论（对所有后续 A/B 对比都适用）**：

> 50 条样本的评估集，单项指标波动可达 ±5~20 点。
> **以后做任何 A/B 对比（含第 4 周的四项优化），必须跑 3 次取平均，
> 否则得出的“提升/下降”很可能是噪声。**

唯一稳定可比的是动作准确率（98.0% × 3），因为它由离散的分类决策构成，
不像关键点覆盖那样受答案措辞影响。

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

**工具扩展：4 → 6，并记录指标变化**

新增两个工具：

| 工具 | 参数 | 补什么盲区 |
|---|---|---|
| `list_documents` | 无 | “你们有哪些制度？”——现在模型只能盲目检索，答不出“一共 4 章” |
| `current_time` | 无 | “今年还剩几天年假”——模型不知道今年是哪年 |

**必须重跑主集评估并记录对比**：

| 工具数 | 动作准确率 | 平均 token |
|---|---|---|
| 4（基线） | **98.0%** | 6716 |
| 6（待测） | ? | ? |

**判断标准**：

- 掉 ≤ 2 点 → 可接受，保留
- 掉 > 2 点 → 逐个移除（先去掉一个再跑），定位是哪个工具造成了稀释

**这就是 tool selection dilution 的实测数据。** 面试时说“工具多了会稀释选择”谁都会说，
能报出“4→6 个以后动作准确率从 98% 变成 X%”的人很少。

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
| 7 | 跨会话记忆未实现（只有 `session_id`，无 `user_id`） | 换个会话就失忆；“长期记忆”面试只能答没做 | 见下节“长期记忆” |

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

> **11/8 补验**：长对话测试用 6000 预算跑 12 轮，
> `[context]` 和「截断」两项 **每轮都触发**（详见 `LONG_CONVERSATION.md`）。
> **三重熔断仍未验** —— 长对话测试没造出重复调用场景。

**一个意外发现**：设计过一条链路——工具返回超 2000 字符 → 截断 + 打 `[truncated]` →
模型看到标记改用 `read_document_page` 精读。

但 `truncated` 出现 **0 次**，而 `read_document_page` 被调了 76 次。
**说明模型是自发去精读的，不是被标记提示的。这条设计只验证了后半段，
“标记能否引导模型”这个假设一次都没验过。**

### 按风险排序

评分 = 演示会碰到 × 面试会问 × 后果严重度（各 1–3 分）

| 排名 | 模块 | 演示 | 面试 | 后果 | 总分 | 状态 |
|---|---|---|---|---|---|---|
| **1** | **多轮会话记忆** | 3 | 3 | 3 | **9** | ✅ 已验（11/8） |
| **2** | **真实 PDF 摄入** | 3 | 2 | 3 | **8** | ✅ 已验 3 种排版 |
| **3** | **上下文裁剪** | 2 | 3 | 3 | **8** | ✅ 已验（6000 预算） |
| 4 | 工具返回截断 + `[truncated]` | 2 | 2 | 2 | 6 | ✅ 已验（12 轮全触发） |
| 5 | 三重熔断 | 1 | 3 | 2 | 6 | ✅ 已验（11/8） |
| 6 | `qdrant.delete_document()` | 2 | 1 | 2 | 5 | ✅ 上传已用到 |
| 7 | 并发（`memory.append` 读-改-写） | 1 | 2 | 2 | 5 | 承认边界 |
| 8 | `ticket.update_status()` | 2 | 1 | 1 | 4 | 可选 |

### 三项必须补的理由

| # | 为什么必须在投递前补 |
|---|---|
| **1 多轮记忆** | 演示时用户一定会追问。“年假几天” → “那病假呢” 是最自然的对话方式。记忆不生效，整套“Agent 感”立刻塌掉 |
| **2 真实 PDF** | 上传是演示的第一个动作。现在只在手工生成的 6 页 PDF 上跑过。三个 ingest bug 都是在这份 PDF 上发现的，换个文档很可能再暴露三个 |
| **3 上下文裁剪** | 面试必问“多轮怎么控 token”。答“做了四级裁剪”，追问“触发过吗”，答“没有”——等于承认这个模块是摆设 |

### 长对话测试：已完成（11/8）

`scripts/test_long_conversation.py` 跑完了，结果见本文件“第 2 项验收结果”。

### 熔断验证：已完成（11/8）

`scripts/test_breaker.py` 跑完了，结果见本文件“第 3 项验收结果”。
**顺带修了一个 `used_tokens` 混淆的 bug。**

### 验证缺口：已清零

截至 11/8，风险排序表里 8 项中：

- ✅ 6 项已验
- ⬜ 2 项是“承认边界”（并发）或“可选”（`ticket.update_status`）

**所有“必须补”的项目已全部完成。**

### 第 6–8 项：不用单独补

| 项 | 为什么 |
|---|---|
| `qdrant.delete_document()` | 写 `documents.py` 时必然用到（重复上传先删旧数据） |
| 并发 | 代码注释已写明“v1 的已知边界：多 worker 会丢更新”。主动承认比假装解决强 |
| `ticket.update_status()` | 补一个 `PATCH /tickets/{id}` 就验证了，5 分钟的事。演示时没人改状态 |

### 长期记忆：新功能，不是修 bug

只有 `session_id`，没有 `user_id`。SPEC 提过跨会话记忆的 key 设计（C3），未实现。

#### 业界参考：Anthropic 的 Agent Memory 四层（2026-11-06）

来源：`The components of agent memory`，Anthropic Engineering，2026-11-06（Lance Martin）
<https://www.anthropic.com/research/components-of-agent-memory>

它把“记忆”的边界划得很窄：**只算跨会话保留的知识**；改权重、单次会话内的
上下文学习、把旧轮次压成摘要（compaction）**都不算**。

| 层 | 存什么 | 特点 |
|---|---|---|
| Transcripts 原始记录 | 完整对话、工具调用、执行结果 | **唯一的事实源**，其余层都是它的缓存 |
| Memory Notes 笔记 | Claude 自己写的经验、错误原因、规则 | 会话中随手写，**写的时候就可能写错** |
| Memory Representations 表征 | Wiki、索引、交叉链接 | 从日志和笔记算出来的结构化知识 |
| Injected Context 注入上下文 | 少量高价值索引 | 每次会话开头加载，**必须很短**（Claude Code：`MEMORY.md` 前 200 行 / 25KB） |

**Dreaming（做梦）**：会话之间的离线流程 —— 回头翻 transcripts，按“上层对下层对账”
的原则改错笔记、合并重复、搭 wiki、把最要紧的提到注入层。
一句话原则：**原始日志是事实，其余都是缓存，缓存要定期对账。**

反直觉的实测结论：写 **413 份**笔记的模型在宝可梦任务上只拿 1 枚徽章，
写 **9 份**的拿 4 枚 —— 记忆的关键不是存多少，是筛得准不准、能不能纠错。

**这条数据的可信度**：Anthropic 自测，宝可梦每模型只跑一次，wiki 实验仅 24–25 题，
无第三方复现。另外网传“87% vs 73% 是 dreaming 带来的提升”是错的，那组数字
是部分并购题上“wiki 检索 vs 纯搜索”的对比。

#### 本项目现状：四层一层都没有

| Anthropic 的层 | 本项目 | 说明 |
|---|---|---|
| Transcripts | ⚠️ 部分 | `data/traces/*.jsonl` 有完整记录，但设计用途是调试回放，不是记忆的取证源 |
| Memory Notes | ❌ | 无 |
| Representations | ❌ | 无，且原文自测显示单文档场景无明显收益 |
| Injected Context | ❌ | 无，新会话不带任何历史 |

**所以：任何文档/简历里都不能写“参考 Anthropic 记忆架构实现了 X”。**
本节的用途是记录业界参照系与差距，不代表实现。

#### 如果要做的顺序（按收益排序）

1. 先让 transcripts 可检索（把已有 trace 变成搜索入口），成本最低
2. 再加 `memory_notes`：会话结束时一次 LLM 调用抽 0–3 条，带 `source_trace_id` 可回溯
3. 注入：新会话按重要性取 top 3–5 拼进 system prompt
4. 最后才考虑结构化表征（wiki）—— 原文数据不支持单文档场景做这一层

**优先级低于“把已有的东西证过”** —— 先证明现有能力真的能用。

---

## 第 4 轮：SSE 流式（已完成 11/8）

### 一个架构决定：`run()` 必须是 `run_stream()` 的收集器

最初打算“不动 `run`，新加 `run_stream`”。问题是：
API 上线跑 `run_stream`，如果测试只覆盖 `run`，
那 174 个测试守的就是一段**永远不会执行的代码**。

改成：`run_stream` 是唯一实现，`run()` 消费它取 `done` 事件。

**逻辑一行不重复，测试覆盖的就是上线路径。**

### 实测事件流（单问题）

```
tool_call  × 2
citation   × 2
token      × 194     ← 真流式，不是跑完再切片
done       × 1
```

### 中途断开：修了一个真 bug

原本收口（`tracer.finish` / 写记忆）在循环**之后**。客户端断开时会跳过，
导致 trace 残缺、对话不进历史。

修复过程中发现两件只有真测才能发现的事：

| # | 发现 | 后果 |
|---|---|---|
| 1 | **实际断连抛的是 `asyncio.CancelledError`，不是 `GeneratorExit`** | 只捕 `GeneratorExit` 会漏——trace 里的 `stop_reason` 会是初始值 `max_steps`（误归因） |
| 2 | **task 被 cancel 后，`finally` 里的 `await` 会立即再抛 `CancelledError`** | `_remember` 里的 `await to_thread` 根本跑不完，记忆依旧丢 |

**修法**：

- `except (GeneratorExit, asyncio.CancelledError)`，**两者都重新抛**
- `await asyncio.shield(self._remember(...))` —— 让内部协程在后台继续
- 新增 `StopReason.CANCELLED`，不把断连归到 `max_steps`
- `partial_text` 在循环外累积，保留已生成的部分

**实测结果：**

```
断开前 trace 行数   948
断开后 trace 行数   949
stop_reason         cancelled        ✅
会话记忆            2 条              ✅
  user:      试用期员工能休年假吗？
  assistant: 我先                        ← 模型刚生成的 2 个字
```

**最后那个“我先”很关键**：说明“保留已生成部分”是真的做了，
而不是只写一句降级文案。

> **这一条在面试里很值钱**：多数人只知道“客户端断开要 finally 收口”，
> 不知道断连抛的是 `CancelledError` 而不是 `GeneratorExit`，
> 更不知道 cancel 之后 `finally` 里的 `await` 会立即失败。

---

## 第 5 轮：前端（已完成 11/9）

`app/static/index.html`，703 行单文件，零构建。`GET /` 返回它。

### 实测结果（Playwright + 系统 Edge）

| 检查项 | 结果 |
|---|---|
| 页面加载 | ✅ |
| **流式逐字增长** | ✅ 采样到 6–8 个不同长度值（21 → 71 → 138 → … → 368） |
| tool_call 累积渲染 | ✅ 4 条 |
| 多轮会话 | ✅ session_id 保存，第二轮带上上下文 |
| 停止按钮（前端断连） | ✅ 页面不崩、显示中断提示 |
| JS 错误 | ✅ 无 |
| **citation 渲染** | ❌ **未验到**（见下） |

### citation 未验的原因：环境，不是代码

Qdrant 在这台机器上**反复崩溃**：

```
Docker Desktop 启动 → 吃 3.8 GB（可用内存 5379 → 1576 MB）
→ Qdrant 容器起来 → 几十秒后 daemon 退出（WSL 发行版 Stopped）
```

服务端因此一直报 `degraded`，检索全部失败，**没有引用可渲染**。

**但降级路径反而被验证了**：Qdrant 不可用时，Agent 会
（1）尝试 3 次检索、（2）如实说“检索服务故障，不是手册没有”、
（3）自动建 P1 故障工单。**这比正常路径更能说明系统是抗造的。**

### 三个实现细节

1. **答案区流式用 `insertAdjacentText`**，不重建 DOM
   （每个 token 都 `innerHTML = ...` 会重排整棵树）
2. **`done` 后做一次极简 Markdown 渲染**（加粗/列表/换行）
   流式期间纯文本，因为半截的 `**` 渲染出来是脏的
3. **SSE 解析要留不完整的尾巴**

```javascript
const parts = buf.split("\n\n");
buf = parts.pop();          // 最后一段可能不完整
```

**不这么做**：事件被网络切成两块时 `JSON.parse` 抛异常，表现为“偶尔丢字”。

### 顺手修的两个真 bug

| bug | 后果 | 修法 |
|---|---|---|
| `lifespan` 里 `ensure_collection()` 硬失败 | Qdrant 不在 → **整个服务起不来** → 前端连页面都打不开 | 降级启动，`/health` 报 degraded |
| 健康检查缓存“启动时失败” | Docker 晚起 3 秒，`/health` 就**永远** degraded | 每次真实探，加 3 秒超时 |

### 演示前必须做的一件事

**先确认 Docker 稳定再起服务**：

```powershell
docker ps                                    # daemon 活着
docker start kb-qdrant
# 连续 3 次成功才算稳定
1..3 | % { Invoke-RestMethod http://127.0.0.1:6333/healthz; Start-Sleep 2 }
```

**内存不够时**：先停 Python 服务（BGE 占几百 MB）再起 Docker，
反过来内存会不够。

### 真实 HTTP 断连补验（11/10）

单元测试用 `gen.aclose()` 验过中断标记，但那是 `GeneratorExit`。
真实浏览器断连走 `CancelledError`。两者共用同一个 except 分支，
但**推理不等于证据**，所以用 Playwright 跑了一遍真实断连。

**四项全过：**

| 检查项 | 结果 |
|---|---|
| trace 的 stop_reason | ✅ `cancelled` |
| 记忆带中断标记 | ✅ |
| 第二轮答对题（问住宿 → 答住宿） | ✅ |
| **第二轮没接着半截内容写** | ✅ 开头无“继续/接着/刚才” |

**记忆里的实际内容**：

```
user:      试用期员工能休年假吗？
assistant: I'll search the knowledge base for relevant policies.

[本轮回答因连接中断而未完成，以上内容不完整]
```

**一个新发现**：断在**工具调用阶段**时，`partial_text` 累积的是
模型的“旁白”（“I'll search…”），不是最终答案。

这比断在正文中间更有信息量——下一轮模型能看出
“上一轮刚开始检索就断了”。

**测试脚本的一个坑**：停止后前端拿不到 `session_id`——
它只在 `done` 事件里返回，而结果是“答到一半就停”，`done` 永远不会到。
所以脚本改成从 SQLite 直接查最新 session（记忆不依赖 Qdrant，降级时也能验）。

---

## 部署（已完成 11/10）

### 交付物

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 应用镜像：非 root、apt/pip/HF 全走国内源、模型预热、healthcheck |
| `.dockerignore` | 排除 `.venv` / `.env` / `data` / `docs` |
| `docker-compose.yml` | app + qdrant，named volume 持久化 |
| `app/main.py::_auto_ingest` | 首次启动自动入库（幂等） |

### 验收结果（清空 volume 后从头跑一遍）

| 检查项 | 结果 |
|---|---|
| 镜像构建 | ✅ `kb-agent-app:latest` 1.59GB |
| `kb-qdrant` | ✅ `Up (healthy)` |
| `kb-agent` | ✅ `Up (healthy)` |
| 自动入库 | ✅ `[AUTO_INGEST] 员工手册.pdf: 14 chunks` |
| `/health` | ✅ `{"status":"ok","chunks":14,...}` |
| 启动耗时 | ✅ 约 10 秒，日志里没有 `Fetching`（模型来自镜像内缓存） |

复现命令：

```bash
docker compose down -v      # 清空 Qdrant volume，让 AUTO_INGEST 真的跑一遍
docker compose up -d --build
docker compose logs app | grep AUTO_INGEST
curl http://localhost:8000/health
```

### 跑通之前踩的三个坑

三条都不是「配置写错」，而是**只有真的在容器里跑才会暴露**的问题：

#### 坑 1：apt 连不上 deb.debian.org，构建卡满 5 分钟后 exit 100

同一台机器、同一时刻在容器里实测三个源：

| 源 | 结果 |
|---|---|
| `deb.debian.org` | 200，但单请求 **16.6 秒**，Packages 索引直接超时 |
| `mirrors.tuna.tsinghua.edu.cn` | **403 Forbidden**（0.4 秒返回） |
| `mirrors.aliyun.com` | **200，0.11 秒** |

修法：构建前换阿里云源。注意 Debian 12+ 把源写在
`/etc/apt/sources.list.d/debian.sources`，老版本是 `/etc/apt/sources.list`，两处都要兜：

```dockerfile
RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        [ -f "$f" ] && sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' "$f"; \
    done; \
    apt-get update && apt-get install ...
```

#### 坑 2：hf-mirror 不支持 Xet，下载会「静默卡住」

新版 `huggingface_hub` 默认走 Xet 传输协议，`hf-mirror.com` 不支持。
失败方式极其坑人：**不报错、不超时、进度条停在 `3/5` 十分钟不动**。

修法：`HF_HUB_DISABLE_XET=1` 退回普通 HTTP。慢（实测 ~90KB/s，95MB 要 17 分钟），
但能跑完，而且只跑一次——烤进镜像层后重建不再下载。

#### 坑 3：模型烤进镜像了，运行期却当成空缓存重下 95MB（最隐蔽）

两个原因叠加，缺一不可：

**① `Embedder.from_settings` 硬编码了 Windows 缓存路径**

```python
# 改之前
cache_dir=Path(r"E:\DevCaches\fastembed"),
```

在 Linux 里这**不是一个路径**，而是一个带反斜杠的普通目录名，
相对 `/app` 解析成 `/app/E:\DevCaches\fastembed` —— 永远不存在。
fastembed 于是每次都判定「没有缓存」，重新下载 95MB，
`/health` 卡在启动期二十多分钟。

```python
# 改之后：容器读环境变量，本地开发仍走 E 盘
cache_dir=Path(os.getenv("FASTEMBED_CACHE_PATH") or r"E:\DevCaches\fastembed"),
```

**② fastembed 默认缓存目录的属主不对**

fastembed 默认缓存到 `tempfile.gettempdir()/fastembed_cache`。构建期是 root，
运行期是 appuser —— 缓存目录不可写，实测 `touch` 直接 Permission denied，
拿不到锁文件同样判定缓存无效。

修法（Dockerfile，接在模型预热之后）：

```dockerfile
RUN mv /tmp/fastembed_cache /models/fastembed_cache
ENV FASTEMBED_CACHE_PATH=/models/fastembed_cache
```

`mv` 是同层内重命名，不产生第二份；`/models` 本来就在
`chown -R appuser:appuser /app /models` 里，属主一并解决。

**为什么 `ENV` 放在预热之后**：Docker 的层缓存从第一处改动往后全部失效。
放进顶部那个 `ENV` 块，预热层立刻作废，又得重下 95MB。放在预热之后，
预热层能命中缓存（实测重建只花几十秒）。

### 本机开发怎么跑（不起容器）

```bash
# 只起 Qdrant（几百 MB，能撑住）
docker compose up -d qdrant

# 应用用本地 venv 跑（Linux/macOS）
AUTO_INGEST=true python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**Windows PowerShell**：

```powershell
docker compose up -d qdrant
$env:AUTO_INGEST="true"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**这是本地开发/调试的快捷方式**，用来快速改代码；验收一律以容器为准（容器里才有
非 root、挂载、服务名解析这些差异 —— 坑 3 就是只在容器里才暴露）。

### 关键配置点（以后改的时候不要踩回去）

| 点 | 不做会怎样 |
|---|---|
| `QDRANT_URL` 用服务名 `qdrant` | 容器里的 localhost 指自己，连不上 |
| `libgl1` / `libglib2.0-0` | PyMuPDF 报 `ImportError: libGL.so.1`，表现像“本地能跑容器不行” |
| 构建期预热模型 | 首个用户等 20-30 秒下载 |
| `HF_ENDPOINT` 镜像站 | 构建时 huggingface.co 拉不动 |
| `.env` 走 `env_file` | API key 会被烤进镜像层 |
| `.dockerignore` 排除 `.venv` | 构建上下文多几百 MB，慢十倍 |
| apt 换阿里云源 | deb.debian.org 单请求 16.6s，Packages 超时，构建 exit 100 |
| `HF_HUB_DISABLE_XET=1` | hf-mirror 不支持 Xet，进度条停在 3/5，不报错也不前进 |
| `FASTEMBED_CACHE_PATH` + 缓存搬 `/models` | 每次启动重下 95MB，`/health` 卡启动期二十多分钟 |

---

## 下一步

**后端、前端、本机构建镜像全部完成（11/10）。**

| 顺序 | 任务 | 说明 |
|---|---|---|
| 1 | 部署到公网 | 本机已验证，还差一台服务器 + 公网地址，让面试官能点开 |
| 2 | 评估集扩样（建议） | 50 条方差太大，A/B 结论不可靠 |

### 前端 SSE 解析要点（已完成，留作参考）

四个区块（约束 C9/C10）：

| 区块 | 对接 | 要点 |
|---|---|---|
| 上传面板 | `POST /documents/upload` | 显示 `chunk_count`；幂等重传应返回相同结果 |
| 问答区 | `POST /qa/ask/stream` | **`fetch + ReadableStream` 手工解析 SSE**（不能用 `EventSource`，因为我们用 POST） |
| trace 面板 | 从 SSE 的 `tool_call` 事件累积 | 可折叠列表，显示工具名 + 参数 |
| 评估页 | `GET /documents` + 静态报告 | 指标打表 |

**前端解析 SSE 的关键**（网络分片不保证按事件边界到达）：

```javascript
let buf = "";
while (true) {
  const {done, value} = await reader.read();
  if (done) break;
  buf += decoder.decode(value, {stream: true});
  const parts = buf.split("\n\n");
  buf = parts.pop();           // ← 最后一段可能不完整，留着
  for (const part of parts) { /* 解析 event: / data: */ }
}
```

**不这么做会怎样**：一个事件被网络切成两块，`JSON.parse` 直接抛异常，表现为“偶尔丢字”。
