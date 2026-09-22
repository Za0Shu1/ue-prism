# ue-prism 使用指南

三步：**装 server → 让 UE 工程连上 → 配 agent**。
前置：Unreal Engine 5.0+（启用插件 **Editor Scripting → Python Editor Scripting**）、本机 Python ≥3.10。
路径一律用正斜杠 `/`。

## 1. 安装

```bash
pip install ue-prism
```

> 开发/源码：`pip install -e .`

## 2. 让 UE 工程连上

用插件安装器把 bridge 装进工程（免编译、纯 Python、跨 UE 5.0–5.8），再重启编辑器：

```bash
prism plugin-install --project "D:/Path/To/YourProject" --yes
```

重启后 Output Log 出现 `[prism] bridge started on <项目>/Saved/Prism` 即成功。

> 临时/调试可手动起：在编辑器 Output Log 右上 `[PY]` 控制台执行
> `import prism.bridge as b; b.start(r"<项目>/Saved/Prism")`

## 3. 配 agent

把 MCP 注册项写进你的客户端配置（幂等、零工程路径，server 自动发现活跃工程）：

```bash
prism setup --client codex        # codex / opencode / cursor / claude / claude-code / all
```

预览加 `--dry-run`，卸载加 `--uninstall`。

## 4. 验证

在 agent 里问一句：**「列出当前连接的 UE 工程，各自加载了哪些地图？」**
能返回工程名与地图，即打通。

## 多工程

同时开多个工程时，agent 先 `list_projects` 看谁在线；调用工具带 `project=<工程名>` 指定（可用 name、文件夹名或路径子串）。只有一个活跃工程时会自动连。

## 常见问题

- **看不到工具 / 启动失败**：多半 `mcp` 没装进当前 Python，或 MCP SDK 版本不符。终端自检 `python -m prism.server --help`。
- **`BRIDGE_UNREACHABLE`**：编辑器已关或主线程长时间卡住（大编译/模态框）；恢复后重试。
- **`AMBIGUOUS_PROJECT`**：多个活跃工程未点名，调用时加 `project=`。
- **想钉死默认工程**：给 server 传 `--project-dir` 或设 `PRISM_PROJECT_DIR`；显式 `project=` 仍可覆盖。

> cook / 迁移改名等**写操作**默认 dry-run 预览，需显式确认才落盘。完整安装细节、cook 双钥、跨版本与发布见仓库开发文档。
