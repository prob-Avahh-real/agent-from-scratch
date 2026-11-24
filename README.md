# Agent From Scratch

从第一性原理出发、零第三方 Agent 框架手写实现的 5 阶段 Python LLM ReAct Agent 工程。项目按可验证的递进关卡拆解大模型智能体的核心机制：从裸 HTTP 调用，到工具调用（Function Calling）、ReAct 思考—行动—观察循环、持久化分层记忆，直至流式 SSE 工具参数拼装与会话安全隔离。

## 阶段演进结构

| 阶段文件 | 核心主题 | 说明 |
|---------|---------|------|
| `01_chat.py` | 最小对话闭环 | 直接请求 `/chat/completions` 接口完成单轮/多轮对话 |
| `02_tool.py` | 工具定义与调度 | JSON Schema 工具声明与安全 AST 四则运算器 |
| `03_react.py` | ReAct 循环 | 多步推理 → 调用工具获取观察 → 收敛输出最终结论 |
| `04_memory.py` | 持久化记忆 | 会话 JSON 落盘，退出重进保留上下文 |
| `05_agent.py` | 生产级 CLI Agent | 流式输出 + 分片 `tool_calls` 重组、分层滚动摘要、多会话管理、Token 成本统计、指数退避重试 |
| `agent_tools.py` | 共享工具与沙箱 | `calculator`、`list_dir`、`read_file`、`write_file`，带工作区路径约束与会话存储拒绝清单 |
| `memory.py` | 分层记忆与计量 | 活跃窗口 (`messages`) + 归档原文 (`archive`) + 滚动摘要 (`summary`)，默认隔离于 `~/.agent-from-scratch/sessions/` |

## 快速开始

### 环境要求
- Python 3.10+
- `DEEPSEEK_API_KEY` 环境变量（兼容 OpenAI Chat Completions 协议接口，可通过 `DEEPSEEK_BASE_URL` 与 `DEEPSEEK_MODEL` 覆盖）

### 安装与运行

```bash
pip install -r requirements.txt

# 运行自动化单元测试（无需联网与 API Key）
python3 -m unittest discover -s tests -v

# 单次指令执行
DEEPSEEK_API_KEY=sk-... python3 05_agent.py "列出当前目录文件并计算 (17*23+5)/6"

# 进入交互式 REPL（支持 /sessions /save /load /summary /archive /stats /export）
DEEPSEEK_API_KEY=sk-... python3 05_agent.py
```

## 测试与验证

```bash
python3 -m unittest discover -s tests
python3 -m py_compile 01_chat.py 02_tool.py 03_react.py 04_memory.py 05_agent.py agent_tools.py memory.py
```
