# 进度 / Progress

| 阶段 | 内容 | 产物 | 闸门 | 状态 |
|---|---|---|---|---|
| 0 | 环境准备 | `.venv` + 依赖 | 能 import + provider 可访问 | ✅ |
| 1 | 裸调 LLM | `01_chat.py` | 模型正常回复 | ✅ |
| 2 | 工具调用 | `02_tool.py` | LLM 用正确参数请调工具 | ✅ |
| 3 | ReAct 循环 | `03_react.py` | 多步后正确终止 | ✅ |
| 4 | 记忆 | `04_memory.py` | 重启进程后仍记得 | ✅ |
| 5 | 打磨 | `05_agent.py`（streaming / 重试 / REPL / 会话） | 能真的拿去用 | ✅ |

## 结构

| 文件 | 作用 |
|---|---|
| `01_chat.py` … `05_agent.py` | 五个阶段的教学脚本，各自可独立运行 |
| `agent_tools.py` | 共享工具层：实现 + JSON Schema + `execute`（02/03/05 共用） |
| `memory.py` | 共享记忆层：多会话存储 + 摘要压缩 + 旧文件迁移（04/05 共用） |
| `.agent_sessions/` | 运行时产物（会话落盘），已在 `.gitignore` |

## 收尾三件（已完成）

- ✅ **工具层收敛**：`02/03/05` 不再各复制一份，统一 `from agent_tools import TOOLS, execute`。
- ✅ **历史压缩**：超过 `AGENT_MAX_HISTORY`（默认 30）条时，把较老对话交给 LLM 压成 `summary`，只保留最近 `AGENT_KEEP_RECENT`（默认 10）条原文。
- ✅ **多会话**：CLI `--session NAME` / `--list-sessions` / `--reset-all`；REPL `/sessions` `/save [名字]` `/load <名字>` `/new [名字]` `/reset` `/summary` `/history`。

还能做的：
- 压缩目前"只留摘要、丢原文"，可改成保留原文 + 分层摘要。
- 加按会话的累计 token / 成本统计。
- 会话明文落盘，敏感场景要加密或移出工作区。

## 已知坑（别踩第二次）

- `httpx` 不能设 `ALL_PROXY=socks5://...` → `ImportError: socksio`；只用 HTTP_PROXY/HTTPS_PROXY
- OpenRouter 的 key 已失效（`401 User not found`），别用它
- `os.environ["DEEPSEEK_API_KEY"]` 报 `KeyError` = 忘了 export，不是代码错
- `~/.zshrc` 里的 `export` 不会被非交互 shell（脚本 / 一次性命令）继承，跑之前要么 `source ~/.zshrc`，要么显式 export
- 请求 `deepseek-chat` 时，响应里的 `model` 字段会回 `deepseek-flash`，别拿它做断言
- 模型一轮可以返回**多个** `tool_calls`（并行调用）：要 for 循环逐个执行、逐个用 `role=tool` 回填，不能只处理第一个
- 回填 assistant 消息时只留 `role / content / tool_calls`，别把服务端返回的整个 message 原样塞回去（可能夹带 reasoning_content 等字段）
- 工具报错不要抛出中断循环，转成字符串当作"观察"回填，模型会自己纠偏（实测读不存在的文件也能优雅收尾）
- 每轮都要把**完整历史**发回去（模型无状态）；只发最后一条 tool 结果会丢上下文
- 流式下 `tool_calls` 是**分片**到达：同一个 `index` 的 `function.arguments` 一段段发来，必须按 index 累积拼完再 `json.loads`；直接取第一片只会得到半截 JSON
- 流式要 usage 得显式传 `stream_options={"include_usage": true}`，且最后那个数据块的 `choices` 是空数组 —— 别假设 `choices[0]` 一定存在
- 打印正文要覆盖流式和**非流式**两条路径；只在流式分支打印会让 `--no-stream` 静默无输出（本次踩过）
- `stream=True` + `iter_lines()` 时行前缀是 `data:`（可能带空格），收到 `[DONE]` 要显式 break，否则会一直等连接
- 记忆只存 `user/assistant` 文本、不存 `tool` 消息：历史干净，也不会出现悬空的 `tool_call_id`
- `AGENT_MAX_HISTORY` / `AGENT_KEEP_RECENT` / `AGENT_SESSION_DIR` 是在 `memory.py` **import 时**读的环境变量，必须在启动进程前设置
- 摘要失败不拖垮对话：`memory.trim` 吞掉 summarize 的异常，退化成直接丢弃旧消息
- **会话隔离只是"上下文隔离"**：`.agent_sessions/*.json` 在工作目录内，模型用 `read_file` 能读到别的会话。实测在空会话里问暗号，它自己翻文件找到了 `secret.json` 的答案 —— 要真隔离得把会话目录移出工具可读范围
