# 测试规范（ue-prism）

> 面向开发者与 AI agent：每层怎么跑、测什么、当前覆盖到什么程度。
> 原则（AGENTS.md）：默认只读；写操作双钥；错误绝不伪装成 ok；列表带 total/truncated/cap。

## 四层测试体系

| 层 | 依赖 | 命令 | 说明 |
|---|---|---|---|
| **L0 离线套件** | 无引擎，Python>=3.10 | `python -m pytest` | 协议/信封/总线/规则纯逻辑/降级结构契约。改任何层必跑 |
| **L1 桥冒烟** | 编辑器在线 | `python scripts/verify_connection.py`；`python scripts/bus_call.py <bus_dir> ping` | 心跳、往返、UE 版本 |
| **L2 校准夹具** | 任意 UE5 工程装好桥插件（编辑器在线） | `python scripts/calib_fixture/run_fixture.py --project <P> --editor <UnrealEditor.exe>` | 已知真值资产端到端 22 项校验（含写路径 rename/move/copy 往返；copy 走 `migrate_asset` duplicate+uses 闭包，内存态自还原，见下），并落 `matrix.json`（ue_version/API tried 命中/计时）——跨版本跑即得兼容矩阵 |
| **L2/L3 cook·package 全链路** | 任意 UE5 工程 + 本机 UE（**编辑器须关闭**） | `python scripts/verify_cook_chain.py --project <P> [--mode cook\|package]` | cook_package 真实全链路：dry-run 组装→双钥真执行→轮询状态→校验产物落盘→日志错误归因。`--mode cook` 校验 cooked `.umap`（9 项，L2）；`--mode package` 真编 `-build -cook -stage -pak -archive` 并校验归档内 `.exe`/`.pak`（10 项，L3——首编耗时长）。因需编辑器关闭、且不经过桥，与 run_fixture 互斥、单独成脚本 |
| **L3 真实工程抽样** | 客户的真实大工程 | 人工驱动 MCP 工具 + 抽检 | 假阳性率、大闭包耗时、阈值手感 |

## L2 校准夹具：资产真值表

夹具全部由代码生成（无二进制入库），落在目标工程 `/Game/PrismCalib/`，只新增、不动既有内容：

| 靶资产 | 真值 | 校验点 |
|---|---|---|
| `T_BigNoise` | 4096^2 源，无限幅 | `runtime_verdict=runtime_heavy`（~21MB 估算），`texture_size` 保持 error |
| `T_FakeBig` | 同 4K 源，MaxSize=128 | `disk_only_bloat` + `capped_by_max_size`；`asset_size_top` 证据链带双证据降档 |
| `T_CalibCube` | 引擎 TextureCube 副本 | Cube 尺寸可由源内存反推（`source_width>0`；5.4 实证无任何尺寸 API 时的兜底路径） |
| `CalibMesh` | 引擎 StaticMesh(Cube) 副本 | 材质槽=最大 section 数（5.4 从 `get_num_sections` 派生，已知真值 1 槽）；碰撞面数在 5.4 `UStaticMesh` python 无访问器（绑定限制，非漏报） |
| `T_Orphan1..3`、`T_Used1K` | 无引用 | `scan_orphan_assets` 全命中；`CalibMap` 作为 level_root 被正确排除 |
| `CalibMap` | 1500 个平铺 SM_Cube（`PRISM_FIXTURE_ACTORS` 可调） | compose 聚合 world>=1400；`/Engine/BasicShapes/Cube x1500` 进合批机会清单 |
| 写路径靶（复用 `T_CalibCube`/`T_Used1K`） | 改名/移动往返（内存态，自还原，不落盘） | `migrate_asset_rename` 走 `rename_asset` 候选链 + 注册表核对旧消失/新存在/还原；`migrate_asset_move` 实证 5.4 无 `move_asset`，`AttributeMissing` 后走 `rename_asset` 全路径兜底（固化该真机分支） |
| `M_CalibRef` → `T_CalibRef` | 材质硬引用贴图（`TextureSample`→BaseColor） | `get_asset_references` `classify_soft=True`：`T_CalibRef` 的 used_by 中 `M_CalibRef` 判为 `hard=True/soft=False`、`used_by_soft_count=0`（软引用正样本待 L3——5.4 无头 python 无稳定造 SoftObjectProperty 资产的途径） |
| `M_CalibBroken` → `T_CalibBroken`（生成材质后删贴图） | 材质 import 表仍记录已删包 = 悬空硬引用 | `scan_broken_references` 判 `missing`（`get_dependencies` name+opts 仍返回该路径，注册表解析不到）；同批健康靶 `T_CalibRef` 不误报（假阳性负控） |
| `M_CalibStub` → `T_CalibStub`（改名未留桩） | `rename_asset` 后旧路径直接消失（5.4 python 不生成可持久化 ObjectRedirector，日志实证 `VerifyImport: Failed to load package`） | `fix_broken_references` 真机写契约：dry_run 把该靶路由进 `unfixable_missing`（非 `fixable_redirectors`）、confirm 双钥落盘不崩（`executed=True`）。redirector 自动修复正样本无头不可靠生成 → 记 L3（路由逻辑已由 L0 覆盖） |

执行细节（5.4 真机实证，跨版本脚本已内置兜底）：
- **commandlet 只能做资产**（import/duplicate 贴图）；`new_level`、关卡编辑、TextureCube 复制在
  `-run=pythonscript` 下会崩，必须走 GUI 编辑器 StartupScripts（run_fixture 自动注入并还原 ini）。
- 编辑器刚载入的纹理，`blueprint_get_size_x`/`memory_size` 可能是 **32x32/4KB 占位**（资源未上传，
  bus 在主线程 tick 内处理、sleep 无效）；热态直读又可能是 **MaxSize 限幅后的有效尺寸**。
  因此度量层固定三分口径：`width`（现状）、`source_width`（源反推，规则判据）、`effective_width`（限幅后）。

## L2/L3 cook·package 全链路（`scripts/verify_cook_chain.py`）

cook 由 server 侧调 UAT（`RunUAT.bat BuildCookRun`）驱动，**不经过编辑器桥**，且要求编辑器关闭（避免与 headless cook 争用工程锁）。故它不进 `run_fixture`（后者会重开编辑器），单独成脚本、单独跑。机器绝对路径只在脚本 stdout 回显，不入库。

链路（5.4 真机实证，夹具工程）：
1. 前置体检：`.uproject` 存在 + 桥非活跃（编辑器确已关）。
2. `dry_run` 组装：命令含 `BuildCookRun -cook -skipstage +maps="/Game/PrismCalib/CalibMap"`，`runuat_found`/`engine_root`（注册表定位）就绪。
3. 双钥真执行（`dry_run=False` 且 `confirm=True`）→ 返回 `task_id`。
4. 轮询 `get_cook_status` 收敛到 `succeeded`（exit_code 0）。
5. 校验 cooked 产物落盘：`Saved/Cooked/<平台>/<工程>/Content/PrismCalib/CalibMap.umap` 存在。
6. `attribute_cook_errors` 归因：0 个 Error 组（真机 695 包 / 0 error / 13 warning；13 条 warning 系夹具故意造的坏材质 `M_CalibBroken`/`M_CalibStub`，符合预期）。

- `--mode cook`：9 项校验全 PASS 退出 0；`--dry` 只做组装冒烟（不启动真实执行），`--help` 见参数。
- `--mode package`（真机 L3，UE5.4 夹具实证）：追加 `-build`（首次编译 Game 目标）+ cook/stage/pak/archive，产物校验改为在归档目录（`--output-dir`，缺省 `<project>/Archived`）递归命中 `<工程>.exe` 与 `*-Windows.pak`；`-archivedirectory` 仅在传 `--output-dir` 时进命令、dry-run 会校验之。实跑 `BUILD SUCCESSFUL`/exit 0/归档 exe+~9.6MB pak/0 Error 组，10 项全 PASS；`--configuration` 可切 Development/Shipping，两配置各 10/10（Shipping 编译更重、耗时略长）。
- 坑（已内化进脚本与工具描述）：`+maps` 写错地图名会被 BuildCookRun 静默忽略（cook 成功却什么都没 cook），先核对真实 map 路径；UE cook 宽松——缺盘资产只记 Warning 并从 cooked map 静默丢弃，`BUILD SUCCESSFUL` 不等于内容完整，须结合 `attribute_cook_errors`/`read_editor_log` 的 Warning 级判读。package 走全量构建，`map`/`iterate` 被忽略（地图由构建期决定）。

## 引擎兼容矩阵（实测）

同一套 `run_fixture`（22 项：域层 `unreal` API + 规则）与 `verify_cook_chain`（cook 9 项 / package 10 项）在不同引擎版本各跑一次即成矩阵。当前实测：

| 引擎 | L2 夹具 run_fixture | cook 全链路 | package 全链路 |
|---|---|---|---|
| UE 5.4.4 | 22/22 | 9/9（695 包/0 error） | Development + Shipping 各 10/10 |
| UE 5.3.2 | 22/22 | 9/9 | Development + Shipping 各 10/10 |

- 域层跨版本靠多签名 `try` 兜底，`tried` 实测两版一致：`used_by`/`get_dependencies`=`name+opts`；纹理尺寸无直读 API → `blueprint_get_memory_size` + 源盘反推；5.3/5.4 `EditorAssetLibrary.move_asset` 皆 `AttributeMissing` → 走 `rename_asset` 兜底。
- 夹具生成器坑（已修）：**UE 5.3 的 Interchange 导入忽略 `AssetImportTask.destination_name`**（包名由源文件名决定），复用同一 PNG 造改名基准贴图（`T_CalibRef/Broken/Stub`）在 5.3 建不出来，连带 5 项 refs/copy 校验失败。修法是这些改名基准改走 `EditorAssetLibrary.duplicate_asset`（显式目标名，跨版本稳）→ 5.3/5.4 均回到 22/22。
- package 的 C++ 宿主坑（已内化）：`-build` 带 `-nocompileeditor`，cook 阶段用编辑器进程加载工程模块——给 C++ 工程跑 `--mode package` 前须先各编一次 **Editor 与 Game 两个 target**，否则 cook 报 `The game module '<X>' could not be found` 直接失败（蓝图工程无此问题，但蓝图工程又没有 Game target 可 `-build`，故 package 测不了）。本轮 5.3 package 用一次性 C++ 宿主验证通过。
- 本机覆盖状态：**5.3.2 与 5.4.4 已全绿**（夹具 22/22 + cook 9/9 + package Dev/Shipping 各 10/10）。更多版本（5.5/5.8 等）在其他机器补跑。

## 工具覆盖矩阵（当前 21 个 MCP 工具）

| 工具 | L0 离线用例 | L2 夹具 checks | 仍欠（L3 真实工程） |
|---|---|---|---|
| `ping` | test_bus | ping.bridge_alive | - |
| `list_projects` / `list_perf_rules` | test_registry / test_rules | -（随 report 间接） | - |
| `describe_asset` / `get_asset_references`（classify_soft 硬/软区分） | test_assets(+2 用例:classify_soft 降级稳定键/_merge_detail 并集) / test_pr05_contract | refs.classify_soft_hard（硬引用真值靶 `M_CalibRef`→`T_CalibRef`） | 大引用面性能；软引用正样本抽检 |
| `scan_broken_references`（悬空/坏引用检测） | test_assets(+2 用例:降级结构键常驻/_dep_resolution_status 三态) | refs.broken_missing_target（已知 missing 靶 M_CalibBroken→T_CalibBroken + 健康靶负控） | 真实工程重定向桩(redirector)修复闭环、软引用漏检面（L3） |
| `fix_broken_references`（写·双钥） | test_migrate(+4 用例:双钥守卫/无引擎降级[读+写]/dry_run fixable-missing 路由) | refs.fix_plan_routing（真机路由 stub 靶进 missing） / refs.fix_write_honest（confirm 落盘 executed 不崩、诚实回报） | redirector 桩自动修复正样本（5.4 python 不留桩 → L3）；missing 需人工 VCS 还原/重指向 |
| `get_asset_chain`（环/god/影响半径） | test_assets(+SCC 纯逻辑) / test_buscall | -（小图 smoke） | 真实大工程的大闭包耗时、god_min_refs 阈值手感 |
| `scan_orphan_assets` | test_orphan_degrade | orphan.targets_found / map_not_orphaned | 真实工程假阳性抽检 >=20 条 |
| `get_asset_metrics`（真大/假大+材质槽） | test_metrics_degrade / test_metrics_runtime | metrics.cube_measurable / real_4k_is_runtime_heavy / capped_4k_is_disk_only_bloat / mesh_material_slots | 真实美术资产压缩格式下 est 口径偏差；碰撞面数 5.4 `UStaticMesh` python 无访问器（绑定限制，见资产表 CalibMesh 行）|
| `list_level_actors`（compose） | test_actors_compose | compose.world_ge_1400 / cube_batching_opportunity | 真实 WP 关卡全载成本 |
| `read_editor_log`（P2 指纹归一+资产归因+按会话切分） / `attribute_cook_errors` | test_logscan(+3 会话用例:多会话拆分/每会话计数/单会话不标多) / test_attribute | -（纯离线，真实工程日志直读验证 distinct 194->10、LogPackageName 196 合 1 组归因 195 资产） | 与真实 cook 日志联动的归因抽检 |
| `scan_folder_assets`（目录级预算 by_dir） | test_folderscan(+5 用例:预算/深度/cap-truncated/子树基) | -（纯离线磁盘，无引擎 API，不需夹具靶；report 间接） | 真实大工程目录占比手感 |
| `get_perf_report`（7 规则+降档+ROI 注解+vs-上次 diff+可配采样/cap） | test_rules(+4 用例:annotate_roi 纯逻辑/run_report 带 roi_summary/report diff vs previous/snapshot disabled) / test_rules_runtime | report.fake_big_downgrade_evidence / real_4k_stays_error / texture_evidence_upgraded / cap_truncates_and_flags_hidden_errors / sampling_fields | sample_size/cap 可配（默认 top-80/cap=50）；大工程须显式调大 sample_size 或缩 scope。5.4 真机实测 sample_size 80→200 使 total 245→356（暴露 73 个被旧硬编码藏起的 error）|
| `cook_package` / `get_cook_status`（cook+package） | test_cook(含 `test_package_command_template` package 模板)/ test_tasks | `--mode cook` 真机 9 项：uproject/编辑器关闭前置/dry 组装(+maps 定位)/双钥启动/状态收敛/succeeded(exit0)/cooked `CalibMap.umap` 落盘/`attribute_cook_errors` 0 错误组（UE5.4 实跑 BUILD SUCCESSFUL，695 包/0 error）。`--mode package` 真机 10 项：另走 `-build -cook -stage -pak -archive` 全链路，校验归档内 `.exe`+`.pak`（UE5.4 实跑 BUILD SUCCESSFUL/exit 0，exe+9.6MB pak 落盘/0 error 组；`--configuration Shipping` 另跑亦 10/10） | 跨版本 cook/package 兼容（5.3/5.8 各跑一次归档 matrix.json）；真实大工程 package 抽样 |
| `preview_asset_migration` | test_migration_preview | - | - |
| `migrate_asset_rename` / `_move` / `_asset`（写） | test_migrate / test_migrate_engine_shapes（双钥/dry_run/回滚/属性缺失形状） | migrate.rename_applies_redirect / rename_restored / move_applies / move_restored / copy_dryrun_plan / copy_applies（真机 rename_asset + 5.4 move→rename 兜底，内存态自还原；copy 走 `migrate_asset` duplicate 带走 uses 闭包 2 包 + 目标目录自动建 `make_directory`，内存态不落盘） | 真实工程提交后引用复核；`fix_up_redirectors` 5.4 python 未绑定（诚实报告未确认清理，编辑器手动兜底） |
| 基建（bus/envelope/cli/pluginpack/resolve） | test_bus / test_buscall / test_cli / test_pluginpack / test_resolve | - | - |

## 新功能测试纪律

0. **公开仓库纪律**：入库文件（`docs/`、`README`、代码注释）不得出现客户工程名、真实机器路径、内部设计稿内容；逐日排障/手动测试记录一律放 `_internal/`（已 gitignore），不随仓库分发。

1. 新 domain 函数：必须带无引擎降级用例（结构键常驻），命名前缀 `get_/list_/scan_/describe_/read_`。
2. 新写操作：默认 dry_run + 双钥用例 + 回滚路径用例（范式见 test_migrate.py）。
3. 触碰 unreal 真机 API 路径：给夹具加一个已知真值靶（能反推的先反推），并在本文档真值表登记。
4. 规则改动：test_rules*.py 用例必更新；报告字段变更需带 `total/truncated/cap` 断言。
5. 提交前 `python -m pytest` 全绿；跨版本改动在 L2 各引擎版本各跑一次并归档 matrix.json。

## 已知限制

- 夹具不测渲染性能（无材质/光照变体），它校准的是**工具判定逻辑与 API 命中率**。
- `probe_asset_api` 是校准辅助工具（未列入对外承诺），随夹具排障使用。
- 跨版本资产拷贝**只向后兼容**（旧版引擎打不开新序列化）——这正是夹具选择"代码现场生成"而非入库二进制的原因。
