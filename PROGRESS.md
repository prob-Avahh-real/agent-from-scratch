# 进度 / Progress

| 阶段 | 内容 | 产物 | 闸门 | 状态 |
|---|---|---|---|---|
| 0 | 环境准备 | `.venv` + 依赖 | 能 import + provider 可访问 | ✅ |
| 1 | 裸调 LLM | `01_chat.py` | 模型正常回复 | ✅ |
| 2 | 工具调用 | `02_tool.py` | LLM 用正确参数请调工具 | ✅ |
| 3 | ReAct 循环 | `03_react.py` | 多步后正确终止 | ✅ |
| 4 | 记忆 | `04_memory.py` | 重启进程后仍记得 | ✅ |
| 5 | 打磨 | `05_agent.py` | 能真的拿去用 | ✅ |

## 结构

| 文件 | 作用 |
|---|---|
| `01_chat.py` … `05_agent.py` | 五个阶段的教学脚本，各自可独立运行 |
| `agent_tools.py` | 共享工具层：实现 + JSON Schema + `execute`（02/03/05 共用），含会话目录拒访 |
| `memory.py` | 共享记忆层：多会话 + 分层摘要 + 成本统计 + 隔离 + 旧数据迁移（04/05 共用） |
| `~/.agent-from-scratch/sessions/` | 会话落盘，默认在工作目录**之外**（`AGENT_SESSION_DIR` 可覆盖） |

## 产品层四件（已完成）

- ✅ **分层摘要**：`messages` 活跃窗口 + `archive` 归档原文 + 滚动 `summary`。超阈值只把最老一批挪进 archive 并刷新摘要，**原文永不丢**。
- ✅ **成本统计**：按会话累计输入/输出/缓存命中 token 与估算成本；每轮尾行显示，`--stats` 或 `/stats` 看汇总。
- ✅ **会话隔离**：会话目录默认在工作目录之外，`agent_tools` 再加拒绝清单兜底 —— 工具读写会话目录直接返回 ERROR，`list_dir` 也不显示它。
- ✅ **更多命令**：REPL `/rename` `/delete` `/export` `/stats` `/archive` `/new` `/load` `/save` `/sessions` `/reset` `/summary` `/history`；CLI `--stats` `--archive` `--export` `--list-sessions` `--reset-all`。

还能做的（未做）：
- 摘要目前是单层，可做多级摘要（越老越粗）。
- 价目默认值只是示例，需按真实账单校准。
- 会话文件仍是明文（已隔离，未加密）。

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
- `AGENT_MAX_HISTORY` / `AGENT_KEEP_RECENT` / `AGENT_MAX_ARCHIVE` / `AGENT_SESSION_DIR` 都是在 `memory.py` **import 时**读的环境变量，必须在启动进程前设置
- 摘要失败不拖垮对话：`memory.trim` 吞掉 summarize 的异常，而且原文早已进 `archive`，不会丢
- `AGENT_PRICE_*` 默认价目只是**示例**（命中 0.5 / 未命中 2.0 / 输出 8.0 元每百万），务必按自己账单核对
- **会话隔离已是真隔离**：会话目录默认在工作目录外，工具层另加拒绝清单。实测 `read_file` / `write_file` 访问会话目录返回 ERROR，`list_dir` 不显示它；把 `AGENT_SESSION_DIR` 设进工作区也一样被拒（这正是隔离测试的用法）
- 中文 argv 的坑：测试脚本里 `$变量` 紧跟多字节字符（如 `"$m，..."`）时字节会被吞，应直接用字面量；`05_agent.py` 里加了 `fix_argv` 把孤立代理还原，避免 print 时崩
- 瞬时网络故障会真实发生（实测 02 因 SSL EOF 崩过一次、05 的读超时被重试自愈）；教学脚本 02/03 没有重试，生产用 05
