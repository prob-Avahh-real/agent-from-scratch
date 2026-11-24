# agent-from-scratch

从第一性原理（零框架、纯 Python + httpx）分 5 阶段手写 LLM ReAct Agent 的教学与实验工程（`01_chat.py` → `05_agent.py`）。

## 用途与 Target 判定

| 平台 | 需要? | 方式 |
|------|-------|------|
| 移动端 | 不适用 | CLI 教学与命令行 REPL 脚本 |
| 智能硬件 | 不适用 | 纯后端/终端推理实验，非穿戴产品 |

## 命令

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests
python3 01_chat.py
python3 05_agent.py --list-sessions
python3 05_agent.py --stats
```

## Harness / Loop（助手）

- 遵循根 `/Users/skat/AGENTS.md` Harness + Loop 规则
- Guide = 本文件与 `PROGRESS.md`；Sensor = `python3 -m unittest discover -s tests` 与各阶段脚本语法/会话隔离校验
- 同类失误第二次 → 沉淀规则或单测（棘轮）

## 验证环 / Done

```bash
python3 -m unittest discover -s tests
python3 -m py_compile 01_chat.py 02_tool.py 03_react.py 04_memory.py 05_agent.py agent_tools.py memory.py
```

**Done 示例**：全部阶段脚本语法通过，会话目录默认隔离在工作区外（`~/.agent-from-scratch/sessions/`）。
