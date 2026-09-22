# AGENTS.md — ue-prism 仓库工作约定

面向在此仓库工作的 AI agent。动手前先读 `README.md` 与 `docs/ARCHITECTURE.md`。

## 三层架构与依赖铁律
- `prism/server.py`（协议层）：可 import `mcp`；**绝不 import `unreal`**。把 MCP 调用转成总线请求。
- `prism/bridge.py`（传输层）：文件总线适配器，只把调用分发到 `domain.FUNCTIONS` 白名单；换引擎/换传输只动这层。
- `prism/domain/*.py`（领域层）：只用 `unreal` + 标准库；**绝不 import mcp/网络/server**；函数须 `@register` 进 `FUNCTIONS`。可整体搬进无头 commandlet。

依赖方向单向：`server -> (bus 文件协议) -> bridge -> domain`。`server` 与 `domain` 之间**不得**直接互相 import，只经总线信封通信。

## 关键约束
- **默认只读**。写操作命名 `migrate_`/`fix_`，且默认 `dry_run` + 显式确认（`dry_run=False` 且 `confirm=True` 双钥才落盘）。
- 读函数命名前缀：`get_` / `list_` / `scan_` / `describe_` / `read_`。
- 所有返回一律走 `envelope.make_ok` / `envelope.make_err`；**错误绝不伪装成 ok**。列表结果携带 `total / truncated / cap`。
- 领域函数异常在 `bridge.handler` 兜底成结构化错误，不裸崩。
- `unreal` API 跨 5.0–5.8 有签名差异（尤其 `AssetRegistry`）：多签名 `try` 兜底，取不到留默认值，失败报 `UE_API_MISMATCH`。

## Python 版本子集（易踩坑）
- domain 层运行在 **UE 内嵌 Python**，版本随引擎在 3.7–3.11 间浮动 → 严格守 **Python 3.7 语法子集**：不要用 walrus `:="、`match` 语句、`tomllib`、`zoneinfo`、3.8/3.9/3.10 才引入的标准库或 typing 特性。f-string、生成器、`pathlib` 基本用法可用。
- server 侧 Python ≥3.10，可放心用新语法。

## 文件总线（`prism/bus.py`）
- `bus_dir` 下 `cmd_<id>.json` 与 `res_<id>.json`，`id` 唯一 → 天然支持多客户端并发。
- 一律**原子写**（`.tmp` + `os.replace`），禁止半截文件被读到。
- 生产轮询挂在 **UE 主线程 slate post-tick**（`bridge.start`）；UE 5.6+ 禁止在非 game thread 调 `unreal`，**不要改成后台线程**。每 tick 只处理一条命令（`serve_once`），控制帧内开销。
- 无引擎的总线测试才用 `bus.run_forever`（线程）。

## 测试
- 无引擎：`python -m pytest`（`tests/test_bus.py` 覆盖 ping 往返 / `UNKNOWN_FN` / `BRIDGE_TIMEOUT`）。改动总线、信封、domain 分发后必须跑。
- 真机：UE 5.x 启用 Python Editor Scripting，按 README 快速开始手动验 `ping`。
- 不要为了让测试通过而删除 domain 里的 `unreal` 分支——无引擎时靠惰性 import 降级返回。

## 依赖与打包
- `pyproject.toml` 锁 `mcp>=2,<3`。改 `server.py` 前核对所 pin 版本的 MCP SDK 导入路径与 tool 注册 API。
- 安装：`pip install -e .`（dev）；server 侧无引擎依赖。

## 文档语言
README/文档中文主体，代码/命令/标识符保留英文。

## 非目标
不做视觉/审美迭代、深度蓝图图编辑、C++ 生成/编译回路。
