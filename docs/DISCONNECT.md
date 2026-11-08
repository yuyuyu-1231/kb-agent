# 中途断开测试

- session_id: `disc-fff35290`
- 方法：读到第一个 token 就主动断开连接

## 一、断开动作

- 读到第一个 token：是
- 已主动关闭连接

## 二、trace

- 断开前行数：948
- 断开后行数：949
- 最后一条 finish 的 stop_reason：`cancelled`
- extra：`None`

## 三、会话记忆

- 消息条数：2
  - `user`: 试用期员工能休年假吗？
  - `assistant`: 我先

## 四、判定

- ✅ trace 写入，且 stop_reason = `cancelled`
- ✅ 会话记忆写入（应含 user + assistant 两条）

**结论**：修复生效 —— 断开后 trace 和记忆都写了。

## 五、这说明什么

`finally` 在 `GeneratorExit` 时也会执行，这是 Python 语言保证的。
所以把「收口」而不是「发 done 事件」放进 finally 是安全的：

- **收口**（tracer.finish / 写记忆）应该在 finally —— 三种结局都要做
- **发 done 事件**应该在正常路径 —— 客户端都断了，再 yield 没人收
