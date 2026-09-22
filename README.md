# ue-prism

> 把 Unreal 项目像棱镜一样分光解析——日志、性能、资产引用，交给 AI agent 逐条检验。

**ue-prism** 是一个面向 Unreal Engine 的 MCP（Model Context Protocol）诊断服务器：让 AI agent 读取并分析**编辑器 / cook / package 日志**（报错归因）、**场景与资产开销**（性能优化建议）、**资产引用链**（迁移决策），所有结果结构化返回、机器可读。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![UE](https://img.shields.io/badge/UE-5.0%20%E2%80%93%205.8%2B-blue)
![PyPI](https://img.shields.io/pypi/v/ue-prism)
![状态](https://img.shields.io/badge/状态-v1.1-brightgreen)

## 能做什么

- **日志诊断**：聚合 Error/Warning，把 cook/package 报错归因到具体资产。
- **性能审计**：静态规则报告（资产体积 / 纹理尺寸 / 网格 LOD / 光源重复等）+ 批量度量。
- **引用链分析**：递归某资产的 `uses`（迁移要带走的依赖闭包）/ `used_by`（谁依赖它），按类聚合数量与体量。
- **迁移套件**：资产改名 / 移动 / 同工程复制，默认 dry-run 预览，写操作需双钥确认。
- **零配置多工程**：编辑器侧自动注册，agent 用 `list_projects` 发现、按 `project=` 选择，MCP 配置里不写任何工程路径。

## 怎么用

**前置**：Unreal Engine 5.0+，启用 Python Editor Scripting；本机 Python ≥3.10。

```bash
# 1. 安装
pip install ue-prism

# 2. 给某个 UE 工程装上 bridge 插件（免编译），然后重启该工程编辑器
prism plugin-install --project "D:/Path/To/YourProject"

# 3. 把 MCP 注册项写进你的 agent 客户端配置（幂等，零工程路径）
prism setup --client codex      # 可选 codex / opencode / cursor / claude / claude-code / all
```

装好后在 agent 里直接问：

- 「**列出当前连接的 UE 工程，各自加载了哪些地图？**」
- 「**把最近编辑器日志里的 Error/Warning 归类，指出可疑原因。**」
- 「**分析 /Game/.../Foo 的依赖闭包，迁移要带走哪些资产、总共多大？**」

> 配置写错了想撤销：`prism setup --client <客户端> --uninstall`；只想预览不实际改：加 `--dry-run`。

## 工作原理（一句话）

agent ⇄ stdio ⇄ **prism server**（纯 Python，不碰引擎）⇄ **文件总线** ⇄ **bridge**（编辑器内 Python，主线程轮询）⇄ **domain**（只用 `unreal` API + 标准库）。三层解耦，覆盖 UE 5.0–5.8+，编辑器崩溃不拖累外部进程。

## 运行要求

- Unreal Engine 5.0+，启用 Python Editor Scripting（launcher 安装自带）。
- server 侧：Python ≥3.10 + MCP Python SDK（锁 2.x），无引擎依赖。
- 编辑器侧：UE 内嵌 Python（随版本 3.7–3.11）。

## 文档

安装与使用见 [docs/SETUP.md](docs/SETUP.md)。

## License

MIT © [Za0Shu1](https://github.com/Za0Shu1)

---

Unreal® 与 Unreal Engine® 为 Epic Games, Inc. 注册商标。本项目与 Epic Games 无关联，亦未获其背书。
