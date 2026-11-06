"""工具层：把服务能力包装成 Agent 可调用的函数。

这一层的唯一职责是**把 Python 能力翻译成模型能理解的契约**：
- 函数签名 → JSON Schema（由 harness.tools.ToolRegistry 自动生成）
- docstring → 工具描述与参数说明
- 返回值 → 模型可读的 JSON

不在这里写业务逻辑。业务在 app/services/，存储适配在 app/infra/。
"""
