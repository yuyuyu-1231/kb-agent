# 术语速查表

> 对话和代码里出现的所有字段名、模块名、概念名，每个配一句话概括。
> 讲代码时第一次出现会在后面加括号说明，这份表用来随时回查。

---

## 一、评估集字段（`data/eval_set.jsonl` 每行一个题目）

| 字段 | 一句话概括 |
|---|---|
| `id` | 题号。前缀区分来源：`D`=直接事实（Direct）、`M`=多跳（Multi-hop）、`R`=应拒答（Refusal）、`H`=人工补充 |
| `type` | 题型枚举，值域 `direct` / `multi_hop` / `refusal` |
| `answerable` | 知识库覆盖程度：`full` 完全可答 / `partial` 部分可答（手册只覆盖一部分）/ `none` 不可答（库里没有） |
| `question` | 问题原文，用户会怎么问就怎么写 |
| `expected_answer` | 标准答案。`partial` 题里必须写明"手册未规定 X" |
| `key_points` | 判分时要覆盖的关键信息，用于**正向验证**（如 `"5 天"`、`"二级以上医院"`） |
| `expected_sources` | 正确答案来自哪几个 chunk，**数组**，每项含 `doc_id / page_no / section_title / chunk_index` |
| `expected_action` | 期望动作：`answer` 直接回答 / `ticket` 说明未找到并转人工 |
| `must_not` | 绝对不能出现的词，用于**反向抓幻觉**（出现即判错） |
| `notes` | 设计备注：为什么这么出题、有什么陷阱 |

---

## 二、单条评估结果（`ItemResult`，每条题跑完的原始记录）

| 字段 | 一句话概括 |
|---|---|
| `recall_hit` | 正确 chunk 有没有进检索 top5（布尔） |
| `reciprocal_rank` | 正确 chunk 排名的倒数：第 1 名 = 1.0，第 5 名 = 0.2，没进 = 0 |
| `retrieved_chunks` | 检索实际返回的 chunk 编号列表（按相关度降序） |
| `cited_pages` | 答案里实际引用了哪些页码 |
| `expected_pages` | 从 `expected_sources` 抽出来的期望页码 |
| `citation_ok` | 引用的页码有没有命中期望页码（布尔） |
| `keypoints_hit` / `keypoints_total` | 命中的关键点数 / 关键点总数 |
| `must_not_hits` | 答案里出现的禁令词列表（空 = 没幻觉） |
| `refused` | **语言上是否表达了"找不到"**（注意：这只是语言信号，不等于"最终判定为拒答"） |
| `stop_reason` | 这次 run 为什么结束，见下方枚举 |
| `steps` | 调了几轮 LLM |
| `tool_sequence` | 这题依次调了哪些工具（如 `["search_knowledge", "search_knowledge", "create_ticket"]`） |
| `action_correct` | 实际 `stop_reason` 是否符合 `expected_action`（布尔） |
| `ticket_id` | 建了单就有单号，没建就是 `None` |
| `latency_ms` / `tokens` | 端到端耗时 / 消耗 token |

### `stop_reason` 枚举（终止原因）

| 值 | 含义 |
|---|---|
| `answered` | 正常回答完毕 |
| `ticket_created` | 建了工单后结束 |
| `max_steps` | 达到最大轮数（6 轮）被强行中断 —— **算无效成本** |
| `budget_exhausted` | token 预算耗尽 |
| `loop_detected` | 检测到重复动作被熔断 —— **算无效成本** |
| `llm_error` | 模型调用失败 |

---

## 三、汇总指标（报告 `summary` 里那些 `L1_` / `L2_` 开头的）

`L1`~`L4` 是四层评估体系，数字越小越靠近数据源。

| 指标 | 一句话概括 |
|---|---|
| `L1_recall_at_5` | 召回率@5：43 条可答题里，正确 chunk 进了 top5 的比例 |
| `L1_mrr` | 平均倒数排名（Mean Reciprocal Rank），衡量正确 chunk 排得多靠前 |
| `L2_citation_precision` | 引用准确率：引用的页码命中期望页码的比例 |
| `L2_refusal_rate` | 应拒答率：5 条库外题里，明确说"找不到"的比例 |
| `L2_false_refusal_rate` | 误拒率：能答的题被错误拒答的比例（只在 `full` 题上算，且要求确实没引用到正确位置） |
| `L2_keypoint_coverage` | 关键点覆盖率：答案覆盖了多少个 `key_points` |
| `L2_hallucination_count` | 幻觉条数：有多少题的答案里出现了 `must_not` 词 |
| `L3_action_accuracy` | 动作准确率：该答就答、该转就转的比例 |
| `L3_avg_steps` | 平均 LLM 调用轮数 |
| `L4_latency_p50` / `p95` | 延迟的 50 分位 / 95 分位（P95 = 95% 的请求比它快） |
| `L4_avg_tokens` | 平均每题消耗 token |
| `L4_wasted_cost_ratio` | 无效成本占比：`max_steps` + `loop_detected` 烧掉的 token 占全部的多少 |
| `L4_cost_by_stop_reason` | 按终止原因分组的成本（回答 vs 转单各花多少） |

---

## 四、代码模块

### 内核层 `harness/`（不依赖 `app` 任何代码）

| 模块 | 一句话概括 |
|---|---|
| `tools.py` | 工具注册表：从函数签名自动生成 JSON Schema；执行工具，把失败变成能回喂给模型的文本 |
| `context.py` | 上下文裁剪：按**信息损失从小到大**分四级裁到预算内（截断→丢检索→滑窗→兜底） |
| `guard.py` | 三重熔断：轮数上限 / token 预算 / 重复动作指纹 |
| `trace.py` | 轨迹落盘：每步写一行 JSONL，工具返回全文按哈希存到 `artifacts/` |
| `memory.py` | 会话记忆：SQLite 存最近 10 轮对话 |
| `loop.py` | ReAct 主循环：调模型 → 执行工具 → 追加消息 → 直到不再调工具 |

### 适配层 `app/infra/`（把第三方库翻译成项目内部接口）

| 模块 | 一句话概括 |
|---|---|
| `llm.py` | 把 openai SDK 适配成 `LLMClient` 协议（只有一个 `chat()` 方法） |
| `embedding.py` | 封装 fastembed + BGE 模型；query 侧加 instruction，passage 侧不加 |
| `qdrant.py` | 封装 Qdrant：建表、写入（uuid5 幂等）、检索、按页读整页 |

### 业务层 `app/services/`

| 模块 | 一句话概括 |
|---|---|
| `ingest.py` | PDF 摄入：逐页提取 → 剔页眉页脚 → 合并断行 → 识别章节 → 切块 |
| `retrieval.py` | 检索封装：query → 向量 → Qdrant → 降序 chunk 列表；另提供按页读取 |
| `ticket.py` | 工单存储：SQLite 建表、生成 `T-YYYYMMDD-NNNN` 单号、状态流转 |
| `evaluation.py` | 四层指标计算 + 聚合（纯函数，可单测） |

### 工具层 `app/tools/`（把服务翻译成模型能调的契约）

| 模块 | 一句话概括 |
|---|---|
| `knowledge.py` | 注册 `search_knowledge`（带 `citation_aware` 标记）和 `read_document_page` |
| `ticket.py` | 注册 `create_ticket`（类别/优先级用 `Literal` 生成 enum）和 `check_ticket_status` |

---

## 五、数据流里的关键概念

| 概念 | 一句话概括 |
|---|---|
| `chunk` | 文档切片，最小检索单位 |
| `chunk_index` | chunk 在文档里的序号（0–13），**检索和评估都用它做身份** |
| `page_no` | 页码（1–6），引用溯源对外展示的就是它 |
| `section_title` | chunk 所属章节，如 `"3.1  年休假"` |
| `doc_id` | 文档标识（本项目就是文件名去扩展名，如 `"员工手册"`） |
| `payload` | Qdrant 里存在向量旁边的元数据（`doc_id / page_no / section_title / text / chunk_index` 等） |
| `citation_aware` | 工具标记：这个工具返回的是"引用候选"，`loop` 会给每条分配全局稳定编号 |
| `index` | 塞进检索结果里的全局引用编号，模型在答案里写 `[1]` 就是用它 |
| `overlap` | 切块时从上一块尾部回退的字符数（本项目 80），防止句子被切断 |
| `carry_overlap` | `flush` 的参数：切完之后要不要把尾部内容留在缓冲区做重叠 |
| `token 预算` | 一次 run 允许消耗的最大 token（本项目 30000） |

---

## 六、返回结构的 `ok` / `allowed` / `refused` 三个"开关"的区别

这三个长得像，语义完全不同：

| 字段 | 在哪 | 含义 | `True` 代表 |
|---|---|---|---|
| `ToolResult.ok` | 工具返回 | 工具调用成功了吗 | 成功（失败会把错误回喂模型） |
| `GuardDecision.allowed` | 熔断检查 | 能继续吗 | 允许继续（`False` 触发降级） |
| `ItemResult.refused` | 评估结果 | 答案里是否说了"找不到" | 说了（**只是语言信号**，是否算误拒由指标自己组合判断） |

---

## 七、脚本

| 脚本 | 一句话概括 |
|---|---|
| `build_eval_set.py` | 生成 50 条评估集，并用真实 chunk 交叉校验 `chunk_index / page_no / section_title` |
| `eval.py` | 跑 50 条评估，输出 `data/reports/{时间}.json/.md` |
| `rescore.py` | 只改指标算法时**离线重算**，不重跑 Agent（隔离"指标变了"和"模型飘了"） |
| `dump_chunks.py` | 导出 14 个 chunk 全文 → `docs/CORPUS.md`，供人工核对评估集 |
| `trace_chunking.py` | 逐轮追踪分块过程，打印每轮 `buf` 内容（调试分块用） |
| `make_sample_pdf.py` | 生成 6 页测试用员工手册 PDF |
| `smoke_*.py` | 各阶段的冒烟验证脚本（依赖、Function Calling、RAG 底层、摄入） |
| `e2e_*.py` | 端到端验证（假数据版 / 真实 RAG 版 / 工单版） |

---

## 八、两篇综述在项目里的位置

| 综述 | 用在哪 |
|---|---|
| Survey on Evaluation of LLM-based Agents | 提供了评估的五视角分类，并明确指出 cost-efficiency / safety / robustness 三处空白 —— 本项目的 5.4–5.6 节就是填这三个坑 |
| Towards Long-Horizon Agents | 提供了 "外部 Harness Engineering × 内部 Model Optimization" 的二分 —— 用来说明为什么本项目只做 Harness 侧、不做训练 |

**两篇都只用作"维度检查表"，不作为实现方法，也不写进简历。**
