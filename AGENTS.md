# AGENTS.md — ue-prism 仓库工作约定

面向在此仓库工作的 AI agent。动手前先读 `README.md`、`docs/SETUP.md`（安装使用）与 `docs/TESTING.md`（测试体系与覆盖矩阵）。

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

## 测试（规范全文见 docs/TESTING.md）
四层体系，按需执行：
- **L0 离线**：`python -m pytest`（21 个文件 / 178 用例：总线、信封、domain 分发、规则纯逻辑、降级结构契约）。改动上述任何一层后必须跑。
- **L1 桥冒烟**：编辑器在线时 `python scripts/verify_connection.py` 或 `scripts/bus_call.py <bus_dir> ping`。
- **L2 校准夹具**：`python scripts/calib_fixture/run_fixture.py --project <工程> --editor <UnrealEditor.exe>` 一键在任意 UE5 工程/版本重建已知真值（真大/假大/Cube/孤儿/合批靶）并跑 11 项真值校验，落 `matrix.json`（ue_version / API tried 命中 / 计时）——跨版本各跑一次即得兼容矩阵。
- **L3 真实工程抽检**：大工程（用户提供）验证假阳性率、大闭包耗时、阈值手感。
- 不要为了让测试通过而删除 domain 里的 `unreal` 分支——无引擎时靠惰性 import 降级返回。
- domain 内 sleep/轮询等待编辑器状态**无效**（总线在主线程 tick 内同步处理）；冷热态敏感的度量要固化字段语义（如 `source_width`/`effective_width`），不随编辑器状态漂移。

## 测试随功能走（强制）
- **每个工具/功能必须自带测试用例与测试方案**，作为提交的一部分：新 domain 函数 → 无引擎降级用例（结构键常驻）；新写操作 → 双钥/dry_run/回滚用例；新规则 → 纯逻辑用例 + 报告纪律断言（`total/truncated/cap`）。
- **真机 API 路径必须进夹具**：给 `scripts/calib_fixture/` 加已知真值靶，并在 `docs/TESTING.md` 的资产真值表与工具覆盖矩阵登记；让用户自己准备测试用例/测试数据 = 缺陷。
- 提交/合并前：L0 全绿；改动触碰 unreal 真机路径 → 至少一个引擎版本 L2 全 PASS。

## 发布纪律（不可擅自发版）
- **发布 = 打 tag / 推 tag / 触发 PyPI 上传 / 建 GitHub Release**，全部是对外不可逆动作（PyPI 不能删版本重发）。
- **只有用户在当前对话里明确授权“发布 vX.Y.Z”时才允许执行**。完成 TODO、测试全绿、用户说“收尾/推进”等，**一律不构成本次发布授权**。
- 攒够增量后应**停下来问**，得到明确的发版指令（含版本号）再动；不确定就当没授权，只提交代码不 tag。
- 版本号只在获得发版授权、真正打 tag 时才 bump 到 `pyproject.toml` + `prism/__init__.py`，不提前改。

## 依赖与打包
- `pyproject.toml` 锁 `mcp>=2,<3`。改 `server.py` 前核对所 pin 版本的 MCP SDK 导入路径与 tool 注册 API。
- 安装：`pip install -e .`（dev）；server 侧无引擎依赖。

## 文档语言

- 代码注释里的 `DESIGN_vX.Y §Z` 是内部设计稿编号，故意不入库；引用时**不带** `docs/` 前缀（仓库内只有 SETUP/TESTING 两篇）。

- README/文档中文主体，代码/命令/标识符保留英文。

## 非目标
不做视觉/审美迭代、深度蓝图图编辑、C++ 生成/编译回路。
