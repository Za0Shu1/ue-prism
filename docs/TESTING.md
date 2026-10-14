# 测试规范（ue-prism）

> 面向开发者与 AI agent：每层怎么跑、测什么、当前覆盖到什么程度。
> 原则（AGENTS.md）：默认只读；写操作双钥；错误绝不伪装成 ok；列表带 total/truncated/cap。

## 四层测试体系

| 层 | 依赖 | 命令 | 说明 |
|---|---|---|---|
| **L0 离线套件** | 无引擎，Python>=3.10 | `python -m pytest` | 协议/信封/总线/规则纯逻辑/降级结构契约。改任何层必跑 |
| **L1 桥冒烟** | 编辑器在线 | `python scripts/verify_connection.py`；`python scripts/bus_call.py <bus_dir> ping` | 心跳、往返、UE 版本 |
| **L2 校准夹具** | 任意 UE5 工程装好桥插件（编辑器在线） | `python scripts/calib_fixture/run_fixture.py --project <P> --editor <UnrealEditor.exe>` | 已知真值资产端到端 50 项校验（含写路径 rename/move/copy 往返 + v1.5 优化族 28 项属性读写往返，均自还原，见下），并落 `matrix.json`（ue_version/API tried 命中/计时）——跨版本跑即得兼容矩阵 |
| **L2/L3 cook·package 全链路** | 任意 UE5 工程 + 本机 UE（**编辑器须关闭**） | `python scripts/verify_cook_chain.py --project <P> [--mode cook\|package]` | cook_package 真实全链路：dry-run 组装→双钥真执行→轮询状态→校验产物落盘→日志错误归因。`--mode cook` 校验 cooked `.umap`（9 项，L2）；`--mode package` 真编 `-build -cook -stage -pak -archive` 并校验归档内 `.exe`/`.pak`（10 项，L3——首编耗时长）。因需编辑器关闭、且不经过桥，与 run_fixture 互斥、单独成脚本 |
| **L3 真实工程抽样** | 客户的真实大工程 | 人工驱动 MCP 工具 + 抽检 | 假阳性率、大闭包耗时、阈值手感 |

## L2 校准夹具：资产真值表

夹具全部由代码生成（无二进制入库），落在目标工程 `/Game/PrismCalib/`，只新增、不动既有内容：

| 靶资产 | 真值 | 校验点 |
|---|---|---|
| `T_BigNoise` | 4096^2 源，显式 `MaxTextureSize=0`（不限幅） | `runtime_verdict=runtime_heavy`（~21MB 估算），`texture_size` 保持 error；v1.5 兼作钳尺寸写靶（钳到 2048→回读→还原 0） |
| `T_FakeBig` | 同 4K 源，MaxSize=128 | `disk_only_bloat` + `capped_by_max_size`；`asset_size_top` 证据链带双证据降档 |
| `T_CalibCube` | 引擎 TextureCube 副本 | Cube 尺寸可由源内存反推（`source_width>0`；5.4 实证无任何尺寸 API 时的兜底路径）；v1.5 兼作 VT 负控（Cube 不支持流式虚拟纹理 → 必进 `skipped`/`vt_blocked`） |
| `T_CalibIcon` | `T_Used1K` 副本 + `TC_EDITORICON` | v1.5 禁流送格式真值：`format_class=no_stream`→`pool_mb=0`/`permanent_mb>0`（恒常驻池外），**不得**被当成 4B/px 未压缩虚报收益 |
| `T_CalibHdr` | 引擎 TextureCube 副本 + `TC_HDR`（GUI StartupScripts 里设并回读） | 高位深真值：5.4 实测 `memory_bytes/est_runtime_bytes ≈ 8.5`（TC_HDR **不做块压缩**，按 8B/px 常驻）→ `format_class=high_bpp`、`runtime_mb==实测`（**不得再乘倍率**）、`actions` 不含 `set_compression`（改格式丢 HDR 动态范围，属美术决策）、`metrics` 与 `scan` 两本账同值 |
| `M_CalibTranslucent` + `CalibMeshTranslucent` | 引擎 Cube 副本挂 `BLEND_Translucent` 材质，Nanite 生成时刻意不开 | v1.5 Nanite 冲突真值链：`blocked_translucent` →（`skip_translucent=True`）必须拦截 →（强开双钥）`conflict_translucent` →（`fix_nanite_translucent`）关回原状。跨版本签名命中记进 `matrix.json` |
| `M_CalibInst`(MIC，父 `M_CalibRef`) + `CalibMeshInstance` | 5.4 探针实证：`MaterialInstanceConstant` **直读 `used_with_nanite` 必抛异常**，开关只在父 `UMaterial` 上（`get_base_material()` 够得着） | `scan_nanite_candidates` 材质清单必须给**非 null** 的 `used_with_nanite`，`usage_api` 记是否经 `get_base_material()`（旧实现在真实工程 5/5 材质恒 null） |
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
- 运行时规模一本账（`streaming.runtime_ledger`，POOL_MODEL `v2-format-aware-runtime`）：引擎实测
  `memory_bytes` 只要不低于 BC 下限就**直接采信**（它已含真实格式，再乘倍率会把 192MB 报成 1.5GB）；
  实测还是占位（未上传）才按"BC 近似 x 格式倍率"兜底（未压缩 4x / `TC_HDR` 8x）。domain 的
  `runtime_verdict` 与 `scan_texture_streaming` 同源，两个工具不会打架。
- 材质槽取回的是 `MaterialInterface`：`blend_mode` 在接口上可读，`used_with_nanite` 只在基类材质上，
  读不到必须 `base_material()` 解包再试一次（仍读不到才留 null，不猜值）。

### v1.5 优化族真值校验（`_opt_texture_checks` / `_opt_nanite_checks` / `_opt_instancing_checks`）

这三组校验直接打 `scan_texture_streaming` / `opt_*` / `fix_nanite_translucent` / `analyze_instancing`
的真机属性读写路径（L0 只能测降级结构与纯数学，签名命中必须真机钉）：

| 校验点 | 已知真值 | 断言 |
|---|---|---|
| `streaming.clamp_target_planned` | T_BigNoise(4096^2, MaxSize=0) 该被建议钳到 2048 且收益 >10MB | need_clamp 命中 + clamp_to/clamp_savings_mb |
| `streaming.clamped_texture_not_reflagged` | T_FakeBig(MaxSize=128) 已被限幅 | 不在 need_clamp + flags 含 max_size_capped（假阳性负控） |
| `streaming.editoricon_is_permanent_not_uncompressed` | T_CalibIcon 的 compression_settings=TC_EDITORICON | format_class=no_stream、pool_mb=0、permanent_mb>0、compression_savings_mb=0（防把常驻误报成 4B/px 省 3/4） |
| `streaming.vt_skip_cube` | T_CalibCube 是 TextureCube | 进 candidates.vt_blocked 且 vt_savings_mb=0（5.4 实证勾选框置灰） |
| `streaming.hdr_bucketed_high_bpp` | T_CalibHdr 的 compression_settings=TC_HDR | `format_class=high_bpp`、`high_bpp=True`、`uncompressed=False`、flags 含 `high_bpp_format`、进 candidates.high_bpp |
| `streaming.hdr_runtime_not_bc_estimate` | 引擎实测/估算原始字节（不信倍率表本身） | `runtime_mb >= 3.9 x est_bc`（5.4 实测 8.5x）——旧口径把 TC_HDR 归"压缩"按 1B/px 估，直接低估 6~8 倍 |
| `streaming.hdr_measured_not_multiplied_again` | 热态实测已含格式 | basis=resident_memory 时 `runtime_mb==measured`（±5%）；basis=derived_estimate 时必然 measured<est（回归：别把 192MB 再乘成 1.5GB） |
| `streaming.hdr_metrics_and_scan_same_ledger` | 同一张图两条通路 | `get_asset_metrics.runtime_verdict` 与 `scan_texture_streaming` 的 runtime_mb/每纹素字节完全相等（一本账） |
| `streaming.hdr_not_auto_recompressed` | 高位深改格式=美术决策 | `compression_savings_mb>0` 但 `actions` 不含 `set_compression`（工具默认不代改） |
| `streaming.summary_separates_ledgers` | 池内 vs 池外常驻两本账 | summary.non_streaming_count>=1 且 uncompressed_count=0；model.version 常驻 |
| `streaming.summary_counts_high_bpp` | high_bpp 与 uncompressed 分档 | summary.high_bpp_count>=1（收益比例 7/8 vs 3/4、动作不同，不能混一档）；model.version=v2-format-aware-runtime |
| `streaming.pool_ini_read` | 夹具工程 Config 里有 ini | read_pool_config files_scanned>=1；compare.demand_mb 有值（读不到 PoolSize 时判 unknown，不猜默认） |
| `streaming.clamp_dry_run_does_not_write` | dry_run=True | planned=1、changed=0、executed=False |
| `streaming.clamp_write_honest` | 双钥真写 MaxSize=2048 | changed=1 + api 命中签名 + saved!=False（写不进必须 failed） |
| `streaming.clamp_readback_from_disk` | 写完再量 | get_asset_metrics 回读 max_size==2048（真值闭环） |
| `streaming.clamp_restored_for_next_run` | 还原 | max_size=0 写回 changed=1（夹具自还原，可重复跑） |
| `streaming.vt_write_blocks_cube` | Cube+2D 一起开 VT | planned=1(2D) 且 skipped=1(Cube，带原因) |
| `compression.already_target_is_no_op` | T_BigNoise(TC_DEFAULT)+T_CalibHdr(TC_HDR) 一起强改 default（`only_uncompressed=False`） | planned=1 只有 HDR 是 change；已是目标格式那张 `no_op` 且原因含「已是目标压缩格式」、行内不带 savings_mb（5.4 真实工程 dry-run 暴露的缺陷 #10 回归线：别虚报 planned/收益、别白保存资产） |
| `compression.conservative_gate_still_blocks_high_bpp` | 同一批走默认闸门 | planned=0、no_op=2（默认绝不代改 TC_HDR，改格式属美术决策） |
| `nanite.target_blocked_before_write` | CalibMeshTranslucent 未开 Nanite + Translucent 材质 | category=blocked_translucent、blend_modes 读到 Translucent |
| `nanite.material_usage_readable_via_base_material` | 基类材质（`M_CalibTranslucent`）挂上的网格 | 材质清单 `used_with_nanite` **非 null** 且为 bool（读不到才是缺陷） |
| `nanite.material_instance_usage_not_null` | CalibMeshInstance 挂 `M_CalibInst`(MIC) | 5.4 实证 MIC 直读必抛异常 -> 必须经 `get_base_material()` 读到非 null，`usage_api` 非空（缺陷 #8 的回归线，编号见 `docs/TODO.md`「真机复盘」） |
| `nanite.skip_translucent_blocks` | 默认 skip_translucent=True | planned=0、skipped=1、原因含「半透明」——绝不误开 |
| `nanite.force_write_honest` | skip_translucent=False 强开（双钥） | changed=1 + api 记中哪条跨版本签名（5.0 supports_nanite / 5.3 nanite_settings.enabled / 5.4 nanite_enabled） |
| `nanite.conflict_detected` | 开成 Nanite 之后 | category=conflict_translucent、nanite_enabled=True + nanite_prop |
| `nanite.fix_write_honest` | fix_nanite_translucent 双钥真关 | changed=1、after=False（回读校验） |
| `nanite.self_restored` | 关回原状 | category 回到 blocked_translucent（夹具可重复校验） |
| `compose.instancing_groups` | CalibMap 1500 散装引擎 Cube | D2 路线 B：plan.strategy=plan_only（只出方案不动场景）+ Cube 组 actors>=1000 + drawcalls_before>after |

- 写校验**自还原**：钳完 `MaxSize=0` 写回、开完 Nanite 用 `fix_nanite_translucent` 关回，
  夹具可反复跑（不留脏状态给下一次矩阵）。
- `matrix.json` 另存 `opt_texture` / `opt_nanite` / `opt_instancing` 三段（含 api/tried），
  跨版本各跑一次即得 **VT / MaxSize / Nanite 三处属性签名 + HISM 探针**的兼容矩阵。
- ✅ 5.4.4 已真机验收 **50/50 全 PASS**（2026-10-10，夹具 errors=0，矩阵/日志留本地 `_internal/`）。
  ⚠️ 5.3 尚未带上这 28 项复跑，跨版本矩阵仍欠 5.3 一行。



## v1.5 优化族的真机验收（5.4 已跑，5.3 待补）

本轮在有编辑器的机器上完成：L0 345 用例（344 passed / 1 skipped）全绿，5.4.4 夹具
**50/50 真机全 PASS**。合并/发版前仍须在 UE 5.3（以及 5.5/5.8 等）各跑一次 `run_fixture`
并归档 `matrix.json`：

```bash
python scripts/calib_fixture/run_fixture.py --project <P> --editor <UnrealEditor.exe> \
    --matrix _internal/matrix-<版本>-v15.json
```

预期 50/50 全 PASS。红了不要放宽断言，按证据补候选签名：
- `nanite.force_write_honest` → 看 `matrix.opt_nanite.nanite_api` 命中哪条（5.0/5.1
  `supports_nanite` / 5.3 `nanite_settings.enabled` / 5.4+ `nanite_enabled`）；没命中就是真
  漂移，往 `opt_nanite.NANITE_FLAT_PROPS` 补候选。
- `streaming.clamp_write_honest` / `clamp_readback_from_disk` → `max_texture_size` 属性名漂移
  或写后未触发重压缩；`saved=False` 说明保存候选没命中。
- `streaming.editoricon_is_permanent_not_uncompressed` → 该版本 `str(compression_settings)`
  文本形态不同，往 `streaming` 的格式提示表补；判不准一律落 `unknown`（不误改美术格式）。
- `streaming.hdr_runtime_not_bc_estimate` / `hdr_bucketed_high_bpp` → 该版本 TC_HDR 的运行时字节/px
  与 5.4 不同（倍率表 `HIGH_BPP_FORMATS`/`HDR_BYTES_PER_TEXEL` 是实测钉的），先在夹具里回读
  `compression_settings` 文本形态，再决定补格式名还是补倍率；别改断言迁就口径。
- `compression.already_target_is_no_op` → 该版本 `str(compression_settings)` 形态不同导致同名判定
  失效（`_enum_member` 取不到成员名），看 checks 证据里的 before/after 文本再补解析候选。
- `nanite.material_instance_usage_not_null` → `get_base_material()` 在该版本不存在/返回 None，
  往 `optutil.base_material()` 补候选（属性名 `base_material` 已兜底）；读不到必须留 null 并红这项，
  不允许悄悄判 False。
- `compose.instancing_groups` → 关卡没打开或 `get_all_level_actors` 签名漂移，看返回 note。

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

同一套 `run_fixture`（50 项：域层 `unreal` API + 规则）与 `verify_cook_chain`（cook 9 项 / package 10 项）在不同引擎版本各跑一次即成矩阵。当前实测：

| 引擎 | L2 夹具 run_fixture | cook 全链路 | package 全链路 |
|---|---|---|---|
| UE 5.4.4 | **50/50**（2026-10-10 实跑，含 v1.5 优化族 28 项 + HDR 高位深/材质实例/压缩同名 no_op 靶；errors=0） | 9/9（695 包/0 error） | Development + Shipping 各 10/10 |
| UE 5.3.2 | 22/22（v1.5 新增 28 项**待复跑**，含 HDR 高位深/材质实例/压缩同名 no_op 靶） | 9/9 | Development + Shipping 各 10/10 |

- 域层跨版本靠多签名 `try` 兜底，`tried` 实测两版一致：`used_by`/`get_dependencies`=`name+opts`；纹理尺寸无直读 API → `blueprint_get_memory_size` + 源盘反推；5.3/5.4 `EditorAssetLibrary.move_asset` 皆 `AttributeMissing` → 走 `rename_asset` 兜底。
- 夹具生成器坑（已修）：**UE 5.3 的 Interchange 导入忽略 `AssetImportTask.destination_name`**（包名由源文件名决定），复用同一 PNG 造改名基准贴图（`T_CalibRef/Broken/Stub`）在 5.3 建不出来，连带 5 项 refs/copy 校验失败。修法是这些改名基准改走 `EditorAssetLibrary.duplicate_asset`（显式目标名，跨版本稳）→ 5.3/5.4 均回到 22/22。
- package 的 C++ 宿主坑（已内化）：`-build` 带 `-nocompileeditor`，cook 阶段用编辑器进程加载工程模块——给 C++ 工程跑 `--mode package` 前须先各编一次 **Editor 与 Game 两个 target**，否则 cook 报 `The game module '<X>' could not be found` 直接失败（蓝图工程无此问题，但蓝图工程又没有 Game target 可 `-build`，故 package 测不了）。本轮 5.3 package 用一次性 C++ 宿主验证通过。
- 5.4.4 实测签名命中（本轮夹具矩阵）：Nanite 走 `nanite_settings.enabled`；MaxSize 走
  `set_editor_property(max_texture_size)`；材质混合模式走 `get_blend_mode()`；`used_with_nanite`
  材质本体直读、材质实例必须 `get_base_material()>used_with_nanite`；纹理尺寸无直读 API（Cube 全靠源盘反推）。
- 本机覆盖状态：**5.4.4 夹具 50/50 + cook 9/9 + package Dev/Shipping 各 10/10**；
  5.3.2 仍是旧 22 项全绿，**v1.5 优化族与 HDR 台账未在 5.3 复跑**。更多版本（5.5/5.8 等）在其他机器补跑。

## 工具覆盖矩阵（当前 29 个 MCP 工具）

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
| `get_perf_report`（12 规则+降档+ROI 注解+vs-上次 diff+可配采样/cap） | test_rules(+4 用例:annotate_roi 纯逻辑/run_report 带 roi_summary/report diff vs previous/snapshot disabled) / test_rules_runtime / test_rules_opt(22 用例:5 条 v1.5 新规则纯逻辑+ROI 两本账 unit+high_bpp 三档收益与"默认不代改压缩"+缺桥时必须播报不静默) | report.fake_big_downgrade_evidence / real_4k_stays_error / texture_evidence_upgraded / cap_truncates_and_flags_hidden_errors / sampling_fields | sample_size/cap 可配（默认 top-80/cap=50）；大工程须显式调大 sample_size 或缩 scope。5.4 真机实测 sample_size 80→200 使 total 245→356（暴露 73 个被旧硬编码藏起的 error）；本轮 160 抽样在 14.38GB 美术工程得 381 findings（error 139/warn 242，含 7 条 high_bpp、0 条误报 Nanite 冲突）|
| `cook_package` / `get_cook_status`（cook+package） | test_cook(含 `test_package_command_template` package 模板)/ test_tasks | `--mode cook` 真机 9 项：uproject/编辑器关闭前置/dry 组装(+maps 定位)/双钥启动/状态收敛/succeeded(exit0)/cooked `CalibMap.umap` 落盘/`attribute_cook_errors` 0 错误组（UE5.4 实跑 BUILD SUCCESSFUL，695 包/0 error）。`--mode package` 真机 10 项：另走 `-build -cook -stage -pak -archive` 全链路，校验归档内 `.exe`+`.pak`（UE5.4 实跑 BUILD SUCCESSFUL/exit 0，exe+9.6MB pak 落盘/0 error 组；`--configuration Shipping` 另跑亦 10/10） | 跨版本 cook/package 兼容（5.3/5.8 各跑一次归档 matrix.json）；真实大工程 package 抽样 |
| `preview_asset_migration` | test_migration_preview | - | - |
| `migrate_asset_rename` / `_move` / `_asset`（写） | test_migrate / test_migrate_engine_shapes（双钥/dry_run/回滚/属性缺失形状） | migrate.rename_applies_redirect / rename_restored / move_applies / move_restored / copy_dryrun_plan / copy_applies（真机 rename_asset + 5.4 move→rename 兜底，内存态自还原；copy 走 `migrate_asset` duplicate 带走 uses 闭包 2 包 + 目标目录自动建 `make_directory`，内存态不落盘） | 真实工程提交后引用复核；`fix_up_redirectors` 5.4 python 未绑定（诚实报告未确认清理，编辑器手动兜底） |
| `scan_texture_streaming`（只读池诊断） | test_streaming_model（34 用例：钳尺寸/VT 可行性/格式四分类 compressed-uncompressed-no_stream-high_bpp-unknown/`runtime_ledger` 实测优先不重复乘/两本账汇总封顶/池对照/ini 优先级） + test_opt_texture（降级结构键含 high_bpp、cap 与窗口独立） | streaming.clamp_target_planned / clamped_texture_not_reflagged / editoricon_is_permanent_not_uncompressed / vt_skip_cube / hdr_bucketed_high_bpp / hdr_runtime_not_bc_estimate / hdr_measured_not_multiplied_again / hdr_metrics_and_scan_same_ledger / hdr_not_auto_recompressed / summary_separates_ledgers / summary_counts_high_bpp / pool_ini_read | 真实大工程的 `est_runtime` 与实测显存偏差；池需求上界 vs 实际驻留的手感（5.4 真实工程：抽样 140 张纹理池需求上界 4.2GB，其中 7 张 TC_HDR 立方图占 1.8GB） |
| `opt_texture_set_virtual` / `opt_texture_clamp_max_size` / `opt_texture_set_compression`（写·双钥） | test_opt_texture(+双钥/字符串 bool 归一/负 max_size/建议文案/high_bpp 闸门/别名解析/同名格式 no_op + `_enum_member` 文本形态) + test_opt_server_contract(脏 bool 不建客户端/export 原子写) | streaming.clamp_dry_run_does_not_write / clamp_write_honest / clamp_readback_from_disk / clamp_restored_for_next_run / vt_write_blocks_cube / compression.already_target_is_no_op / conservative_gate_still_blocks_high_bpp | 重压缩时机与盘上体积回落（改压缩需编辑器重压缩，cook 前磁盘不变）；`bUsedWithVT` 需工程开 VT 才有真收益；TC_HDR 需 `only_uncompressed=False` 才动（默认挡在闸门外） |
| `scan_nanite_candidates`（只读） | test_opt_nanite（27 用例：分类六态 + 降级键常驻 + `base_material()` 三态解包 + 材质清单非 null） | nanite.target_blocked_before_write / conflict_detected / material_usage_readable_via_base_material / material_instance_usage_not_null | 真实高模候选阈值手感（`min_triangles`）；真实工程某网格 5 材质经 `get_base_material()` 全部读到确定值 |
| `opt_mesh_set_nanite` / `fix_nanite_translucent`（写·双钥） | test_opt_nanite（三条版本签名钉死 + 写不进必须 failed + 半透明拦截 + blend_mode 读不到保守跳过） | nanite.skip_translucent_blocks / force_write_honest / fix_write_honest / self_restored | 材质 `bUsedWithNanite` 只报不改（动材质另议）；真实工程警告回落抽检 |
| `analyze_instancing`（只读，D2 路线 B） | test_opt_instancing（分组/阻断/drawcall 前后纯逻辑） | compose.instancing_groups | 路线 A（真折叠成 HISM）未实现：UE 关卡 Actor 级改动无事务回滚，需另开 PR |
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
- **改了 server 侧（`prism/rules.py`/`prism/server.py`），已在线的 MCP 服务不自动生效**——它是常驻
  stdio 进程，模块在**进程 import 时定格**。5.4 实测判据链：MCP `list_perf_rules` 返回的
  `uncompressed_texture` 文档串仍是旧文本（无「TC_HDR 高位深 8B/px」），而工作树 `prism/rules.py`
  已是新文本；该服务进程启动于本轮改动之前（`Get-CimInstance Win32_Process` 看 `python.exe -m
  prism.server` 的 CreationDate 即可对号）。**机制不是"加载了发布版拷贝"**：客户端以仓库目录为
  cwd 拉起 `python -m prism.server`，`sys.path[0]` 就是工作树，所以它一直跑工作树代码，只是旧那一版。
  现在已 `pip install -e .`（`Editable project location` 指向仓库），换 cwd 启动也解析到工作树；
  **改完 server 侧仍须重启 MCP 服务/客户端**才见效。对照实验（只读）：`list_perf_rules` 的文档串、
  `get_perf_report` 的 high_bpp 命中数。引擎侧（domain/streaming 打进插件的那份）则不同——
  `plugin-install --project <P> --yes --force` + 重启编辑器**当场生效**，无需重启 MCP（本轮实测：
  编辑器重启后 MCP 立刻拿到缺陷 #10 的新判定 `planned=1`/`no_op=3`）。
- 池需求是**保守上界**（可流送纹理按整条 mip 链进池），不等于实际驻留；未配 `*PoolSize` 的工程
  `verdict=unknown` 是诚实降级，不是漏判。
- **冷态源边长反推可能低估**（`source_side_exact=false` 时要当心）：Cube 的
  `texture_source_memory` 与 BPP/面数/mip 链三者耦合，只靠反推会得到非 2 的幂边长。5.4 真实工程
   `TC_HDR` 立方图实测：反推 2365（`runtime_mb=340`，basis=derived_estimate），载入后实测
   `memory_bytes` 对应边长 4096（`runtime_mb=1024`，basis=resident_memory）——差 3 倍。
   口径本身已诚实（`runtime_ledger` 实测优先 + `runtime_basis` 标注），但**读数前先看 basis**：
   要准确值就让纹理先进编辑器内存（打开引用它的关卡/资产），或按 `est x ratio` 当下限读。
   后续可加"Cube 源边长"真机靶改进反推（记 TODO，未擅自动口径）。
