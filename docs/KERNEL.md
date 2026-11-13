# 内核速查（面试前翻这一份）

> 用法：面试前 10 分钟过一遍。四块的「一句话」用来想起来，「追问预案」用来接住第二层，
> 「话术」用来开口。
>
> **块四单独成一节**，因为它有完整的踩坑链条，适合当主讲述——面试时间只够讲一块时讲它。

---

## 四块速查

| # | 块 | 一句话 |
|---|---|---|
| 1 | 工具 Schema 自动生成 | 签名反射 + pydantic 转 schema，但必须**内联 `$ref`**、**剥掉 `title`** |
| 2 | 四级上下文裁剪 | 按「信息损失」排序：先丢可再生的检索结果，后丢历史对话；兜底只掏空内容不删消息 |
| 3 | 三重熔断 | 三种「停」分开记；动作指纹**要排序参数键**，否则静默失效 |
| 4 | 断连收口 | 抛的是 `CancelledError` 不是 `GeneratorExit`；`finally` 里的 `await` 要 `shield` |

**一句话把四块串起来**：

> 内核要解决三件事：模型怎么知道有哪些工具（1）、上下文怎么不爆（2）、怎么保证停得下来（3）；
> 上线后补了第四件：用户中途关页面时怎么不丢现场（4）。

---

## 块 1：工具 Schema 自动生成

### 关键代码

```python
sig = inspect.signature(fn)
hints = typing.get_type_hints(fn)                    # 解析字符串注解
validator = create_model(f"{name}_Args", **fields)   # 交给 pydantic
raw = validator.model_json_schema()
raw = _inline_refs(raw)                              # 展开 $ref
raw = _strip_titles(raw)                             # 剥掉 title
```

### 三个非显然取舍

| 取舍 | 为什么 |
|---|---|
| **先建 pydantic 模型，再转 schema** | `Optional` / `Literal` / `Enum` / 默认值→required 的边界分支太多，pydantic 已经处理完了，自己遍历 `get_origin/get_args` 容易漏 |
| **必须内联 `$ref`** | `Literal` 会生成 `$defs` + `$ref`；**部分网关不支持 `$ref` 会静默忽略**——不报错，但参数定义消失，模型开始瞎填 |
| **剥掉 `title`** | pydantic 自动加 `"title": "Top K"`，对模型零信息量，但每轮请求都带；6 工具 × 3 参数 ≈ 几百 token/轮 |

### 追问预案

| 追问 | 答法要点 |
|---|---|
| 「为什么不手写 schema？」 | 手写和签名必然长期不一致，改了函数忘了改 schema，运行时才炸，且报错指向不清 |
| 「`get_type_hints` 为什么必要？」 | `from __future__ import annotations` 会让注解变成**字符串**，直接读 `param.annotation` 拿到的是 `'str'` 不是 `str` |
| 「无参工具怎么办？」 | 允许。我最初 `raise` 了，加 `list_documents` 时被自己的规则挡住——是我错了，删掉限制并改了测试名 |
| 「参数校验失败怎么办？」 | 不抛异常，返回 `ToolResult(ok=False, error=...)`，**把 pydantic 原文回喂给模型**——这是 Agent 自愈的来源 |
| 「和 LangChain 的 tool 装饰器区别？」 | 核心路径一样（签名→schema）。我只用 pydantic 是为了控制清洗细节（`$ref` 内联、`title` 剥离），这些在封装好的框架里不好插手 |

### 话术

> "工具 Schema 我没手写，是从函数签名反射生成的：`inspect.signature` 拿参数，`get_type_hints`
> 解析注解（`from __future__` 会让注解变字符串，必须用这个才能解），交给 pydantic 动态建模型再转 JSON Schema。
>
> 中间两个坑：pydantic 遇到 `Literal` 会生成 `$ref` 引用，而**部分网关不支持 `$ref` 会静默忽略**——
> 不报错，但参数定义没了，模型开始瞎填，这种 bug 抓包看 payload 才能发现。所以我写了个函数把 ref 全内联展开。
> 另一个是 pydantic 自动加的 `title` 纯属浪费 token，每轮请求都带，我把它删了。
>
> 还有一处是我自己的认知错误：最初认为工具必须有参数，无参就 `raise`。加"列出所有文档"时被挡住了——
> 一个不需要参数的函数完全可以是个好工具。删了限制，测试从 `paramless_tool_rejected` 改成 `paramless_tool_registers`。"

---

## 块 2：四级上下文裁剪

### 四级顺序（**不是按编号，是按信息损失**）

| 级 | 操作 | 损失 | 排序依据 |
|---|---|---|---|
| 1 | 截断超长工具结果，尾部打 `[truncated]` | **无损** | 只变短，模型知道被截了，可改用精读工具 |
| 2 | 检索结果降级 top5→top3→top1 | **低损** | **检索是唯一可再生的**——丢了可以换 query 重查 |
| 3 | 历史对话滑窗（留最近 3 轮） | **中损** | 丢了就没了，影响指代消解（"那病假呢"） |
| 4 | 兜底：最早几条 observation 换占位符 | **高损** | 最后手段 |

**一句话原则：先丢可再生的，后丢不可再生的。**

### 三个非显然取舍

| 取舍 | 为什么 |
|---|---|
| **不装 tiktoken，按字符估** | 用 DeepSeek，tokenizer 不公开；cl100k 只是近似，中文误差可能 20%。"不准但可校准"好过"精确但用错 tokenizer" |
| **滑窗必须以「轮」为单位** | 按消息条数切会把 `assistant(tool_calls)` 和它的 `tool` 结果切到两个窗口，配对断裂 → 下次请求 400 |
| **兜底只替换 content，不删消息** | 同上：OpenAI 协议要求每个 `tool_call` 都有配对的 tool 消息 |

### 一个血泪 bug：`used_tokens` 混淆

```python
# 错误写法（已删）
used_tokens = max(used_tokens, fit.estimated_tokens)
```

`fit.estimated_tokens` 是**本次请求的上下文大小**，`used_tokens` 是**整个 run 的累计消耗**。

混用后果：`token_budget=50` 时 **steps=0**——光 system prompt 就 400 token，
"上下文 400 > 预算 50" 立刻熔断，**模型一次都没被调用**。

修复：删掉那行。上下文超预算由 ContextManager 裁剪，不该由熔断兜。

### 追问预案

| 追问 | 答法要点 |
|---|---|
| 「token 估不准怎么办？」 | 系数可配（`TOKEN_ESTIMATE_CN`/`OTHER`），评估阶段用 API 返回的 `usage.prompt_tokens` 反推校准；budget 留余量 |
| 「为什么要截断还要打标记？」 | 模型不知道内容是残的，会基于半截信息**自信地答错**；打了标记它才有机会改用精读工具 |
| 「裁剪怎么验证生效？」 | trace 里记 `[context]` 动作。最初出现 **0 次**（做了但没验过），后来用 6000 预算跑 12 轮，每轮都触发 |
| 「为什么滑窗留 3 轮？」 | 经验值。要能覆盖"上一轮问年假、这一轮问'那病假呢'"这种指代；留太多会挤掉检索结果 |

### 话术

> "上下文做了四级裁剪，但**顺序不是按编号，是按信息损失排的**。
>
> 先截断超长工具结果——无损，只要打个 `[truncated]` 标记，模型知道自己看到的是残的，可以改用精读工具。
> 再降级检索结果，因为**检索是唯一可再生的内容**，丢了可以换 query 重查。然后才是历史对话滑窗，那个丢了真没了。
> 最后兜底是把最早的 observation 内容换成占位符。
>
> 一处必须小心：**兜底不能删消息，只能掏空内容**。OpenAI 协议要求每个 `tool_call` 都有配对的 tool 消息，删了直接 400。
> 滑窗也必须按「轮」切，不能按条数切。
>
> token 我**没装 tiktoken**——DeepSeek 的 tokenizer 不公开，拿 cl100k 近似不如自己按字符估、再用真实 usage 校准系数。
>
> 顺带说个我踩的坑：我曾把「上下文大小」和「累计消耗」两个字段混用，导致 `token_budget=50` 时第 0 轮就熔断，模型一次没调。
> 这两个概念必须分开：前者管"这一次塞不塞得下"，后者管"整个 run 花了多少"。"

---

## 块 3：三重熔断

### 三种「停」必须分开记

| StopReason | 说明什么 | 该改什么 |
|---|---|---|
| `max_steps` | 任务太复杂，或 prompt 让它过度检索 | 调 prompt / 提高上限 |
| `budget_exhausted` | 上下文膨胀太快 | 改裁剪策略 |
| `loop_detected` | 模型陷在错误推理里 | 加约束 / 换模型 |

混成一个 `STOPPED` 的话，评估时只知道"10% 请求被熔断"，**不知道改哪里**。

### 三个非显然取舍

| 取舍 | 为什么 |
|---|---|
| **动作指纹要 `sort_keys=True`** | JSON 键顺序不固定，`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 序列化后不同 → 同一调用被当两个动作 → **重复检测永不触发，且不报错** |
| **第二次就熔断，不给重试** | 工具是确定性的，同样输入必然同样输出。重复调用只可能因为模型没读到上次 observation、或陷在错误循环 |
| **熔断不抛异常** | 返回 `GuardDecision(allowed=False)`，调用方走降级（转工单）而不是 500。和 `ToolResult(ok=False)` 同一哲学 |

### 一个误归因的坑：`CANCELLED` 曾经不存在

最早没有 `CANCELLED` 这个 StopReason，断连时 trace 里记的是 **`max_steps`**（因为那是初始值）。

后果：排查"为什么这次请求停了"时，会去查"是不是 max_steps 设小了"，而真实原因是用户关了页面。
**误归因比没有信息更糟。**

### 追问预案

| 追问 | 答法要点 |
|---|---|
| 「指纹为什么用哈希不存原文？」 | 参数里可能带整页文档内容，存原文占内存；截断到 16 位十六进制（64 bit），碰撞概率 2⁻⁶⁴ |
| 「为什么 `repeat_threshold` 最少是 2？」 | 第 1 次必须允许（否则任何调用都熔断）。2 的语义是"第 2 次相同就停" |
| 「熔断后用户体验是什么？」 | 走降级文案 + 转工单。不是报错，用户看到"需要人工跟进"而不是 500 |
| 「怎么验证熔断真能触发？」 | 写 `test_breaker.py` 造三种必然触发的场景。trace 里这三个标记**曾经全是 0 次**，说明从来没触发过 |

### 话术

> "Agent 循环最大的风险不是答错，是停不下来。三重熔断分别覆盖：轮数上限、token 预算、动作重复。
>
> **三个原因分开记**，因为它们指向不同改进方向——`max_steps` 说明任务太复杂或 prompt 让它过度检索，
> `loop_detected` 说明模型陷在错误推理里。混成一个是没法指导优化的。
>
> 重复检测用**动作指纹**：工具名 + 排序后的参数 JSON 做 sha256。**排序这个细节很关键**——
> JSON 键顺序不固定，不排序的话同一组参数会算成两个动作，重复检测永远不触发，而且不报错，是静默失效。
>
> 阈值设第二次就熔断，不给重试机会。工具是确定性的，同样输入必然同样输出，重复调用只可能是模型没看上次结果或陷在循环里。
>
> 熔断**不抛异常**，返回 `GuardDecision`，让调用方转工单而不是 500。"

---

## 块 4：断连收口（**主讲述**）

### 为什么这块最值钱

它是唯一一块有**完整的「错误假设 → 实测打脸 → 定位根因 → 修复 → 再验证」链条**的。
而且涉及 `CancelledError` 和 `asyncio.shield`——多数人不知道的细节。

下面按时间线写，每一步都带证据。

---

### 第 0 步：问题是什么

流式模式下收口（`tracer.finish` + 写会话记忆）原本在循环**之后**：

```python
async for event in ...:
    ...
tracer.finish(...)      # ← 循环后面
self._remember(...)
```

用户关页面 → generator 被关闭 → 从 `yield` 处抛出 → **跳过循环之后的所有代码**。

后果：**trace 残缺 + 这轮对话不进历史**。

修法思路：把收口放进 `finally`。

---

### 第 1 步：错误假设 —— 「断连抛 `GeneratorExit`」

我的推理是：generator 被关闭时抛 `GeneratorExit`，所以：

```python
except GeneratorExit:
    cancelled = True
    raise
finally:
    tracer.finish(...)
    await self._remember(...)
```

**跑测试，直接失败**：

```
trace 行数 948 → 949     ← finally 确实执行了
stop_reason = max_steps  ← 初始值！
会话记忆 0 条
```

---

### 第 2 步：从「初始值」反推 —— 根本没进 except

**关键推理**：`max_steps` 是 `stop_reason` 的**初始值**。它没被改过，说明循环**从来没走到任何一个 `break` 分支**。

也就是说：循环是被异常打断的，但**那个异常没被我的 `except` 捕获**。

那唯一可能：**抛的不是 `GeneratorExit`**。

---

### 第 3 步：根因一 —— 真实断连抛的是 `CancelledError`

| 场景 | 抛什么 |
|---|---|
| 主动 `agen.aclose()` | `GeneratorExit` |
| **Starlette 取消 task（HTTP 断连走这条）** | **`asyncio.CancelledError`** |

**两者是完全不同的类型**，只捕前者会漏掉**所有真实断连**。

`GeneratorExit` 只有在你主动关 generator 时出现——**真实用户在浏览器点停止，永远不会触发它**。

**修复一**：

```python
except (GeneratorExit, asyncio.CancelledError):
    cancelled = True
    raise          # 两者都必须重新抛
```

**为什么必须重新抛**：

- 吞掉 `CancelledError` → asyncio 认为任务"拒绝被取消"，**从此再也取消不掉**，事件循环一直等它
- 吞掉 `GeneratorExit` → generator 无法正确关闭，`aclose()` 会挂起

---

### 第 4 步：再测 —— 还是没写进记忆

`stop_reason` 现在对了（`cancelled`），但**会话记忆还是 0 条**。

---

### 第 5 步：根因二 —— cancel 之后，`finally` 里的 `await` 立即失败

**这是最反直觉的一条**：即使你捕到了 `CancelledError`、进了 `finally`，
里面**任何 `await` 都会立即再抛一次 `CancelledError`**——因为 task 已经被标记为取消状态。

所以 `await self._remember(...)` 根本走不到真正的 `to_thread` 调用。

**修复二**：

```python
try:
    await asyncio.shield(self._remember(session_id, question, content))
except asyncio.CancelledError:
    # shield 在外层被取消时立即抛 CancelledError，但内部协程仍在后台跑。
    # 这里吃掉它，让 finally 能继续往下走（构造 result）。
    pass
except Exception:
    # aclose() 时事件循环可能正在关闭，await 会抛 RuntimeError。
    pass
```

**`asyncio.shield` 的作用**：把内部协程"隔离"出来，外层被取消时它仍在后台跑完。
代价是**这里不再等它完成**（等不了），只是保证它不会被取消掉。

---

### 第 6 步：再测 —— 记忆有了，但内容是空的

记忆 2 条，`stop_reason=cancelled`，但 `assistant` 内容是**空串**。

而我的降级文案写的是"**已保留已生成的部分**"——**这句话是假的**。

---

### 第 7 步：根因三 —— `answer` 只在循环结束时赋值

断连时循环没跑完，`answer` 还是空串。

**修复三**：在**循环外**累积已生成的文本：

```python
partial_text = ""                      # 循环外
...
    if delta.content:
        text_parts.append(delta.content)
        partial_text += delta.content  # 循环内累积
        yield AgentEvent("token", ...)
...
finally:
    if cancelled and partial_text:
        answer = partial_text          # 用累积的，不是 answer
```

**再测，记忆里存的是**：

```
assistant: 我先
```

**这 2 个字就是证据**：模型刚生成了 2 个字就被打断了，而记忆里确实留下了这 2 个字。
"保留已生成部分"这句话**从文案变成了事实**。

---

### 第 8 步：最后一个问题 —— 半截答案会带偏下一轮

记忆里存着 `assistant: "我先"`，下一轮模型看到会以为**上一轮还没说完**，可能接着往下写。

**修复四**：打标记

```python
CANCELLED_MEMORY_NOTE = "\n\n[本轮回答因连接中断而未完成，以上内容不完整]"
```

实测验证：断连后第二轮问住宿标准，模型**答的是住宿**，开头 60 字内没有"继续/接着/刚才"。

**为什么不断言"太短就不存"**：

- 不存 user → 用户下句说"刚才那个"，模型一片空白，体验更差
- 只存 user 不存 assistant → 部分模型的 messages 校验对连续 user 会告警

---

### 最终代码骨架

```python
cancelled = False
partial_text = ""
try:
    while step < max_steps:
        ...
        yield AgentEvent("token", ...)
except (GeneratorExit, asyncio.CancelledError):
    cancelled = True
    raise
finally:
    if cancelled:
        stop_reason = StopReason.CANCELLED
        if partial_text:
            answer = partial_text
    if not answer:
        answer = _DEGRADE_TEXT.get(stop_reason, "处理未完成。")

    tracer.finish(stop_reason=stop_reason.value, answer=answer, ...)

    if stop_reason in (ANSWERED, TICKET_CREATED, CANCELLED):
        content = answer + (CANCELLED_MEMORY_NOTE if cancelled else "")
        await asyncio.shield(self._remember(session_id, question, content))
```

---

### 主讲述话术（60 秒版，可直接背）

> "流式模式下有个容易漏的路径：用户关页面。如果收口代码在循环之后，那 trace 和会话记忆就全丢了。
> 我把收口放进了 `finally`。
>
> 但真正踩坑的是**异常类型**。我一开始只捕 `GeneratorExit`——那是 generator 被 `aclose()` 时抛的。
> 跑测试发现 trace 确实写了，但 `stop_reason` 是初始值 `max_steps`，记忆 0 条。
> **`max_steps` 是初始值**，说明循环根本没走到任何 `break`，它是被异常打断的，但没进我的 `except`。
>
> 查下来：**Starlette 在 HTTP 断连时是取消 task，抛的 `asyncio.CancelledError`**，和 `GeneratorExit` 是完全不同的类型。
> 后者只有主动 `aclose()` 才会出现——**真实用户在浏览器点停止，永远不会触发它**。
>
> 第二个坑更反直觉：**即使捕到了，`finally` 里任何 `await` 都会立即再抛 `CancelledError`**，
> 因为 task 已经被标记取消了。所以 `await self._remember(...)` 根本走不到真正的写盘。
> 要用 `asyncio.shield` 把内部协程隔离出来，让它在后台跑完。
>
> 第三个细节：**半截答案要打标记**。断连时记忆里存的是 `assistant: "我先"`；
> 不加标记的话，下一轮模型会以为上一轮没说完，接着往下写。加了 `[本轮回答因连接中断而未完成]` 之后，
> 实测第二轮问新问题，模型答的是新问题。"

### 追问预案

| 追问 | 答法要点 |
|---|---|
| 「断连了还写 trace 有意义吗？」 | 三个：**调试**（流式 bug 依赖网络时序，没 trace 连"发生过"都证明不了）、**成本**（用户跑了钱花了，trace 里记 tokens）、**体验**（下次问"刚才那个"至少知道问到哪） |
| 「`shield` 之后你怎么知道记忆真写进去了？」 | 实测查 SQLite：断连后记忆 2 条，`assistant` 内容是 `"我先"`——那 2 个字是模型断前刚生成的 |
| 「为什么不用 `try/except` 包住整个循环？」 | 可以，但 `except` 只负责标记，收口必须在 `finally`——因为正常结束也要收口，三种路径只能有一个统一的收口点 |
| 「`CancelledError` 为什么不能被吞？」 | Python 3.8+ 它是 `BaseException` 子类；吞掉会让 asyncio 认为任务拒绝取消，事件循环一直等它 |
| 「这个 bug 为什么单元测试没发现？」 | 单元测试用 `gen.aclose()` 触发的是 `GeneratorExit`，走的是另一条路径。真实 HTTP 断连才走 `CancelledError`——所以必须用 Playwright 做真断连验证 |

---

## 附：限流的三项实测（可当第五个讲述点）

> 复现：`python scripts/verify_rate_limit.py`

### 实测数字（2026-11-13，额度 20/分钟）

| # | 测什么 | 结果 |
|---|---|---|
| ① | 同一 IP 第 21 次 | **429**；前 20 次全 200 |
| ② | 不同 IP 是否独立 | **是**：A 打满后 B 的第 1 次仍 200，B 第 21 次才 429 |
| ③ | `--workers 2` | 打 100 次：**1 worker 过 20，2 workers 过 40**（**精确 2 倍**） |

### 429 响应的真实形状

```
status : 429
headers: content-length: 63
         content-type: application/json
body   : {"detail":"请求过于频繁：每 IP 每分钟最多 20 次"}
```

**没有 `Retry-After`**。这是个真实缺陷：客户端只知道“被限了”，不知道多久后能重试，
只能无脑退避。要补的话一行就够（`JSONResponse(..., headers={"Retry-After": "60"})`），
但补了必须同步改测试（`test_rate_limit_429_body_and_headers` 故意锁住了“现在没有”）。

### 两个非显然的细节

**① 白名单只免口令，不免限流**

`/health` 和 `/` 不需要口令，但**计入限额**。理由：否则 `/health` 就是一个免费的压测入口。

代价（要主动说）：前端每 15 秒轮询 `/health` 一次 → 每分钟吃掉 4 次额度。
额度 20 时，用户实际可用 16 次。这是**用体验换防刷**的取舍。

**② 探活不能用 HTTP**

写验证脚本时踩的：用 `/health` 做“服务就绪”探活，它会吃掉 1 次配额，
导致「1 worker 只过 19 次」的假失败。改成只探 TCP 端口（`socket.create_connection`）。

### 面试话术（60 秒）

> “限流我做的是**进程内存的滑动窗口**，实测：同一 IP 第 21 次就 429，不同 IP 各算各的。
>
> 但我也实测了它的边界：`uvicorn --workers 2` 时，打 100 次请求，
> **1 worker 过 20 次，2 workers 过 40 次**——精确翻倍，因为每个进程各一份计数。
>
> 所以我在启动时加了一条检测：`WEB_CONCURRENCY > 1` 就打印警告，
> 提醒“额度会变成 N 倍”。**我不把它当 bug 修**，因为当前是单容器单 worker，
> 进程内计数就是精确的；为一个不存在的场景引入 Redis 不划算。
> 真要多实例，得用 Redis 的 `INCR` + `EXPIRE`——**不是 MySQL**，
> 限流是每请求路径，MySQL 的连接池会先被打满。
>
> 还有个细节：`/health` 在白名单里，但我没给它免限流——只免口令。
> 否则它就是个免费压测口。代价是前端 15 秒轮询会吃掉每分钟 4 次额度，
> 这是有意的取舍。”

### 追问预案

| 追问 | 答法 |
|---|---|
| “为什么不用令牌桶？” | 滑动窗口实现更简单（一个 deque），而且能精确回答“最近 60 秒内多少次”；令牌桶更适合“允许突发”的场景，我这里不需要突发 |
| “为什么先限流后鉴权？” | 否则猜口令不消耗配额，等于免费重试。测试 `test_rate_limit_applies_before_auth` 守住了这个顺序 |
| “限流按什么维度？” | 按 `scope["client"][0]` 的 IP。**潜在问题**：前面加 nginx 后，所有人都会算成 nginx 的 IP，得改读 `X-Forwarded-For`——这是部署时容易忘的一步 |
| “429 应该带 Retry-After 吗？” | 应该。现在没带，因为当前只有前端一个消费者，它会自己退避。对外 API 就该带 |
| “内存里的 deque 会不会泄？” | 有上限清理：`_hits` 超过 2048 个 key 时清理空队列。但**不会清理“仍有记录”的 IP**——高基数下还是会涨 |

---

## 附：如果时间只够 30 秒

只讲块四的结论 + 一个数字：

> "流式下客户端断连，服务端要做收口——写 trace 和会话记忆。
> 我最初捕的是 `GeneratorExit`，实测发现真实断连抛的是 `asyncio.CancelledError`，两个是不同类型，一次都没捕到。
> 修完之后又发现 `finally` 里的 `await` 会立即失败，得用 `asyncio.shield` 隔离。
> 最后实测记忆里留下了模型断前刚生成的 2 个字。"
