# 测试规范（ue-prism）

> 面向开发者与 AI agent：每层怎么跑、测什么、当前覆盖到什么程度。
> 原则（AGENTS.md）：默认只读；写操作双钥；错误绝不伪装成 ok；列表带 total/truncated/cap。

## 四层测试体系

| 层 | 依赖 | 命令 | 说明 |
|---|---|---|---|
| **L0 离线套件** | 无引擎，Python>=3.10 | `python -m pytest` | 协议/信封/总线/规则纯逻辑/降级结构契约。改任何层必跑 |
| **L1 桥冒烟** | 编辑器在线 | `python scripts/verify_connection.py`；`python scripts/bus_call.py <bus_dir> ping` | 心跳、往返、UE 版本 |
| **L2 校准夹具** | 任意 UE5 工程装好桥插件（编辑器在线） | `python scripts/calib_fixture/run_fixture.py --project <P> --editor <UnrealEditor.exe>` | 已知真值资产端到端 59 项校验（含写路径 rename/move/copy 往返 + v1.5 优化族 28 项属性读写往返，均自还原，见下），并落 `matrix.json`（ue_version/API tried 命中/计时）——跨版本跑即得兼容矩阵 |
| **L2/L3 cook·package 全链路** | 任意 UE5 工程 + 本机 UE（**编辑器须关闭**） | `python scripts/verify_cook_chain.py --project <P> [--mode cook\|package]` | cook_package 真实全链路：dry-run 组装→双钥真执行→轮询状态→校验产物落盘→日志错误归因。`--mode cook` 校验 cooked `.umap`（9 项，L2）；`--mode package` 真编 `-build -cook -stage -pak -archive` 并校验归档内 `.exe`/`.pak`（10 项，L3——首编耗时长）。因需编辑器关闭、且不经过桥，与 run_fixture 互斥、单独成脚本 |
| **L3 真实工程抽样** | 客户的真实大工程 | 人工驱动 MCP 工具 + 抽检 | 假阳性率、大闭包耗时、阈值手感 |

## L2 校准夹具：资产真值表

夹具全部由代码生成（无二进制入库），落在目标工程 `/Game/PrismCalib/`，只新增、不动既有内容：

| 靶资产 | 真值 | 校验点 |
|---|---|---|
| `T_BigNoise` | 4096^2 源，显式 `MaxTextureSize=0`（不限幅）+ **显式关 VT**（5.3 导入按 `AutoVTSize` 自动开、5.4 不开；不钉住则 5.3 上 `already_virtual` 会吞掉钳/VT 候选） | `runtime_verdict=runtime_heavy`（~21MB 估算），`texture_size` 保持 error；v1.5 兼作钳尺寸写靶（钳到 2048→回读→还原 0）与 `need_clamp` / VT `planned` 正样本 |
| `T_FakeBig` | 同 4K 源，MaxSize=128（克隆自 `T_BigNoise`，同样显式关 VT） | `disk_only_bloat` + `capped_by_max_size`；`asset_size_top` 证据链带双证据降档；不得出现在 `need_clamp` |
| `T_CalibCube` | 引擎 TextureCube 副本 | Cube 尺寸可由源内存反推（`source_width>0`；5.4 实证无任何尺寸 API 时的兜底路径）；v1.5 兼作 VT 负控（Cube 不支持流式虚拟纹理 → 必进 `skipped`/`vt_blocked`） |
| `T_CalibIcon` | `T_Used1K` 副本 + `TC_EDITORICON` | v1.5 禁流送格式真值：`format_class=no_stream`→`pool_mb=0`/`permanent_mb>0`（恒常驻池外），**不得**被当成 4B/px 未压缩虚报收益 |
| `T_CalibHdr` | 引擎 TextureCube 副本 + `TC_HDR`（GUI StartupScripts 里设并回读） | 高位深真值：5.4 实测 `memory_bytes/est_runtime_bytes ≈ 8.5`（TC_HDR **不做块压缩**，按 8B/px 常驻）→ `format_class=high_bpp`、`runtime_mb==实测`（**不得再乘倍率**）、`actions` 不含 `set_compression`（改格式丢 HDR 动态范围，属美术决策）、`metrics` 与 `scan` 两本账同值 |
| `M_CalibTranslucent` + `CalibMeshTranslucent` | 引擎 Cube 副本挂 `BLEND_Translucent` 材质，Nanite 生成时刻意不开 | v1.5 Nanite 冲突真值链：`blocked_translucent` →（`skip_translucent=True`）必须拦截 →（强开双钥）`conflict_translucent` →（`fix_nanite_translucent`）关回原状。跨版本签名命中记进 `matrix.json` |
| `M_CalibInst`(MIC，父 `M_CalibRef`) + `CalibMeshInstance` | 5.4 探针实证：`MaterialInstanceConstant` **直读 `used_with_nanite` 必抛异常**，开关只在父 `UMaterial` 上（`get_base_material()` 够得着） | `scan_nanite_candidates` 材质清单必须给**非 null** 的 `used_with_nanite`，`usage_api` 记是否经 `get_base_material()`（旧实现在真实工程 5/5 材质恒 null） |
| `CalibMesh` | 引擎 StaticMesh(Cube) 副本 | 材质槽=最大 section 数（5.4 从 `get_num_sections` 派生，已知真值 1 槽）；碰撞面数在 5.4 `UStaticMesh` python 无访问器（绑定限制，非漏报） |
| `T_Orphan1..3`、`T_Used1K` | 无引用 | `scan_orphan_assets` 全命中；`CalibMap` 作为 level_root 被正确排除 |
| `CalibMap` | 1500 个平铺 SM_Cube（`PRISM_FIXTURE_ACTORS` 可调） | compose 聚合 world>=1400；`/Engine/BasicShapes/Cube x1500` 进合批机会清单；也是 `opt_collapse_hism` 的折叠/回滚真值靶（1500 -> 1 HISM -> 回滚 1500） |
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
- 运行时规模一本账（`streaming.runtime_ledger`，POOL_MODEL `v4-cube-dual-hypothesis`）：引擎实测
  `memory_bytes` 只要不低于 BC 下限就**直接采信**（它已含真实格式，再乘倍率会把 192MB 报成 1.5GB）；
  实测还是占位（未上传）才按"BC 近似 x 格式倍率"兜底（未压缩 4x / `TC_HDR` 8x）。domain 的
  `runtime_verdict` 与 `scan_texture_streaming` 同源，两个工具不会打架。
- **源边长安全阀（POOL_MODEL `v4-cube-dual-hypothesis`，复盘 #11 的 A″ 止血）**：冷态贴图未载入时
  `source_width` 只能由 `source_memory_bytes` 反推，Cube 6 面 x `TC_HDR` 8B/px x mip 链三者耦合让方程
  多解（真实工程 4096 立方 `hDRI` 曾被推成 2365、估 340MB，载入后实测 1024MB）。域层标
  `source_side_exact=false` 后，池模型**只撤依赖边长的金额**：`clamp_savings_mb` /
  `compression_savings_mb` 恒撤（面积比与 bc 折算都吃边长），`vt_savings_mb` / `streaming_release_mb`
  仅在没有实测（`runtime_basis=derived_estimate`）时撤——实测 MB 与边长无关，该照报。
  **发现、`clamp_to` 与 actions 一律保留**（不报价 != 没问题，#7 的隐身教训），条目加 `size_unverified`
  flag 与 `size_basis`（source_exact / source_unverified / runtime_px / unknown），汇总加
  `size_unverified_count` / `size_unverified_pool_mb` / `size_unverified_permanent_mb` /
  `savings_withheld_count`。金额是 `None` 而不是 `0.0`（0.0 的含义是"确认没有收益"），下游
  `float(x or 0)` 求和天然容错，`annotate_roi` 的 isinstance 守卫自动不把它计入 MB 总计。
  触发条件严格用 `is False`：缺键（`probe_source=False` 的保守路径、热态直读路径）一律不挡。
  残余风险如实说：`runtime_mb` 仍是反推估计，所以 `oversized_mip` 在不可信时封顶 warn、
  `uncompressed_texture` 的档位仍按台账 MB 判——彻底校准要等 #11 的 B（已知边长的大立方图真机靶）。
- 材质槽取回的是 `MaterialInterface`：`blend_mode` 在接口上可读，`used_with_nanite` 只在基类材质上，
  读不到必须 `base_material()` 解包再试一次（仍读不到才留 null，不猜值）。

### v1.5 优化族真值校验（`_opt_texture_checks` / `_opt_nanite_checks` / `_opt_instancing_checks` / `_opt_hism_checks`）

这四组校验直接打 `scan_texture_streaming` / `opt_*` / `fix_nanite_translucent` / `analyze_instancing` / `opt_collapse_hism`
的真机属性读写路径（L0 只能测降级结构与纯数学，签名命中必须真机钉）：

| 校验点 | 已知真值 | 断言 |
|---|---|---|
| `streaming.clamp_target_planned` | T_BigNoise(4096^2, MaxSize=0) 该被建议钳到 2048 且收益 >10MB | need_clamp 命中 + clamp_to/clamp_savings_mb；**反向守卫**：`size_basis=source_exact` 且 `source_side_exact=true`（已知 2 的幂靶绝不该被安全阀撤金额，#11） |
| `streaming.clamped_texture_not_reflagged` | T_FakeBig(MaxSize=128) 已被限幅 | 不在 need_clamp + flags 含 max_size_capped（假阳性负控） |
| `streaming.editoricon_is_permanent_not_uncompressed` | T_CalibIcon 的 compression_settings=TC_EDITORICON | format_class=no_stream、pool_mb=0、permanent_mb>0、compression_savings_mb=0（防把常驻误报成 4B/px 省 3/4） |
| `streaming.vt_skip_cube` | T_CalibCube 是 TextureCube | 进 candidates.vt_blocked 且 vt_savings_mb=0（5.4 实证勾选框置灰） |
| `streaming.hdr_bucketed_high_bpp` | T_CalibHdr 的 compression_settings=TC_HDR | `format_class=high_bpp`、`high_bpp=True`、`uncompressed=False`、flags 含 `high_bpp_format`、进 candidates.high_bpp |
| `streaming.hdr_runtime_not_bc_estimate` | 引擎实测/估算原始字节（不信倍率表本身） | `runtime_mb >= 3.9 x est_bc`（5.4 实测 8.5x）——旧口径把 TC_HDR 归"压缩"按 1B/px 估，直接低估 6~8 倍 |
| `streaming.hdr_measured_not_multiplied_again` | 热态实测已含格式 | basis=resident_memory 时 `runtime_mb==measured`（±5%）；basis=derived_estimate 时必然 measured<est（回归：别把 192MB 再乘成 1.5GB） |
| `streaming.hdr_metrics_and_scan_same_ledger` | 同一张图两条通路 | `get_asset_metrics.runtime_verdict` 与 `scan_texture_streaming` 的 runtime_mb/每纹素字节完全相等（一本账） |
| `streaming.hdr_not_auto_recompressed` | 高位深改格式=美术决策 | `compression_savings_mb>0` 但 `actions` 不含 `set_compression`（工具默认不代改） |
| `streaming.summary_separates_ledgers` | 池内 vs 池外常驻两本账 | summary.non_streaming_count>=1 且 uncompressed_count=0；model.version 常驻；size_unverified_count=0（夹具全是已知尺寸） |
| `streaming.summary_counts_high_bpp` | high_bpp 与 uncompressed 分档 | summary.high_bpp_count>=1（收益比例 7/8 vs 3/4、动作不同，不能混一档）；model.version=v4-cube-dual-hypothesis |
| `streaming.pool_ini_read` | 夹具工程 Config 里有 ini | read_pool_config files_scanned>=1；compare.demand_mb 有值（读不到 PoolSize 时判 unknown，不猜默认） |
| `streaming.clamp_dry_run_does_not_write` | dry_run=True | planned=1、changed=0、executed=False；行内 savings_mb 必须是数字且 `savings_withheld=0`（安全阀不得误伤可信边长） |
| 计划窗口可读性（L0 `test_window_rows_put_actionable_first`） | 宽窗口里 change 与 no_op 混排 | `targets` 按 change 优先（收益降序）返回、`list_cap=0` 取全量、`list_cap`/`targets_truncated` 常驻（真实大工程踩点：61 张待钳被前 80 行 no_op 淹没，计划看起来像空的） |
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
- ✅ **5.3.2 补跑 v1.5 新增项：59/59 全 PASS**（2026-10-15，独立沙箱工程 `PrismCompat53` +
  `--hism-roundtrip`：1500 散装 Cube -> 1 个 HISM（实例 1500）-> 回滚 1500，两侧真值都对得上）。
- ✅ 同一轮夹具（含显式关 VT 的真值钉子）在 **5.4.4 沙箱工程 `PrismCompat54` 复验 59/59**。
  ⚠️ 5.5/5.8/5.1 仍欠：本机只剩 5.3/5.4 引擎（注册表里的 5.1/5.5 是卸载残留）。
- ✅ 2026-10-14 在真实大工程（9242 资产 / 14.4GB，3163 纹理 + 1898 网格）复跑 **59/59 全 PASS**，
  且**同一编辑器会话连跑两轮**都绿。真实工程照出两处只在真机才会暴露的缺陷：
  注册表**多导出包按导出重复计数**（一个 HLOD 包占 76 格、400 格纹理窗口里 125 项"读不到尺寸"、
  池需求只报得出真实值的一小截——160 格窗口仅量到 35 张）与**单包引用查询失败被误判成整场 API 漂移**（残留内存态复制靶让同会话第二轮夹具必崩）。



## v1.5 优化族的真机验收（5.3 / 5.4 均已跑）

L0 461 用例（460 passed / 1 skipped）全绿；`run_fixture --hism-roundtrip` 在 **UE 5.3.2 与 UE 5.4.4
各 59/59 全 PASS**（2026-10-15，两具独立沙箱工程；2026-10-16 在 domain 追加 `scan_missing_source_files` 并重装插件后复跑，两版仍各 59/59；#11 D 落地成 POOL_MODEL
`v4-cube-dual-hypothesis` 后同日复跑，两版桥都回传 `source_layout=cube_array`）。5.3 这一行照出一处只在跨版本时才会露脸的
真值坑，产品代码不用改，改的是夹具：

- **5.3 的贴图导入器会按 `UTextureImportSettings.AutoVTSize`（源像素 ≥ 4096×4096）自动把大贴图置成
  `VirtualTextureStreaming=true`，5.4 起该行为取消**。于是原样导入的 4K 靶在 5.3 一进门就是“已是虚拟
  纹理”，`clamp_to` / `need_clamp` / VT `planned` 三条候选一起消失 -> `streaming.clamp_target_planned`、
  `streaming.vt_write_blocks_cube` 红。修法不是放宽断言，而是把“未开 VT 的 4K 大图”这块真值显式钉住
  （`gen_assets_cmdlet._pin_virtual_off`：写 + 回读 + 证据进 `assets_summary.json`），两版各 59/59。
  给用户的同一条读数：5.3 工程里大图 `already_virtual` 多半是引擎导入时替美术开的，不是美术勾的。
- 5.3 的 `LevelEditorSubsystem.new_level` 只吃 1 个参数（5.4 的 `new_level(dest, False)` 在 5.3 抛
  `takes at most 1 argument`），夹具多候选兜底命中；HISM 路线 A（裸 Actor + `new_object` HISM 根组件）
  在 5.3/5.4 同样写满 1500 实例——5.4 钉死的折叠路线不是 5.4 专属。

新增引擎版本（5.5/5.8/5.1…）时仍按同样姿势各跑一次 `run_fixture` 并归档 `matrix.json`：

```bash
python scripts/calib_fixture/run_fixture.py --project <P> --editor <UnrealEditor.exe> \
    --matrix _internal/matrix-<版本>-v15.json
```

预期 59/59 全 PASS。红了不要放宽断言，按证据补候选签名：
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

## #11 B 冷态反推真值靶（`make_dds.py` + `probe_source_truth.py`）

安全阀 A″ 只挡「无可信解」（`source_side_exact=false`）；**伪精确**（exact=true 但边长错）
必须拿**已知边长的源**才量得出来。DDS 的 DX10 头能把「边长 / 每像素字节数(BPT) / 面数」三件事
一次钉死（PNG 不行——位深与通道数由导入器决定），于是真值由靶文件自己写出，不依赖引擎实现。

跑法（任意引擎版本；只读度量 + 自清理。**不进 59 项门禁**：这些靶是未被引用的散装资产，
并进夹具目录要把孤儿/引用类校验的真值集一并改掉；口径定了再并）：

```bash
UnrealEditor-Cmd.exe <uproject> -run=pythonscript \
    -script=scripts/calib_fixture/probe_source_truth.py -stdout -unattended -nosplash
# 可选：PRISM_PROBE_DDS=<源图目录> / PRISM_PROBE_OUT=<输出目录> / PRISM_PROBE_KEEP=1
```

UE 5.4.4 与 5.3.2 各跑一次，**每个测量键逐字节一致**（2026-10-15）：

| 靶 | authored 真值 | 引擎 source_memory | 冷态反推 | 判定 |
|---|---|---|---|---|
| `T_CalibF32` | 512 @16B/px | 4,194,304 ✓ | 1024 @4B，exact=true | **伪精确**：边长 ×2，金额照报（池 10.64MB vs 同档 512 真值口径 2.66MB） |
| `T_CalibF16` | 512 @8B/px | 2,097,152 ✓ | 512 @8B，exact=true | 正确 |
| `T_CalibRGBA8` | 1024 @4B/px | 4,194,304 ✓ | 1024 @4B，exact=true | 正确 |
| `T_CalibR16` | 1024 @2B/px | 2,097,152 ✓ | 512 @8B，exact=true | **伪精确**：边长 ÷2 |
| `T_CalibR8` | 1024 @1B/px | 1,048,576 ✓ | 512 @4B，exact=true | **伪精确**：边长 ÷2 |
| `T_CalibCubeF32` | 256 @16B/px ×6 面 | 6,291,456 ✓（6 面全算） | 1254 @4B，exact=false | 安全阀生效：撤金额、留 `enable_vt` 动作（`vt_savings_mb=None`） |

四条读数结论（数字由 `tests/test_fixture_dds.py` 14 用例离线锁定，不需要引擎也能复算）：

- UE 把源位图**原样存进包内**：float 类 `source_disk_bytes == source_memory_bytes`，定点类只多几 K
  序列化开销。所以「读 UE 存的源图头」拿不到 PNG/DDS 头——C 只能改读 SourceArtifacts 的外部原文件。
- 歧义只出在 **BPT 不在 {4,8}** 的源上（候选顺序先试 4 再 8，等价类里 2 的幂解不止一个）：现状 6 张里 2 张正确。
- 压缩先验（方案 1）**已实测否决**：同解总量下换顺序只是「抢先试中者称王」——`T_CalibRGBA8`
  4,194,304 = 1024²×4 = 512²×16 同解，覆盖式把正确答案推成 (512,16) 伪精确；夹具 `T_CalibHdr`
  （`TC_HDR` 标注、源 8-bit、6×128²×4B = 393,216）正属「标 HDR 实为 8B/px」常见形态，被从
  (128,4,6) 推成 (64,16,6)。fallback-only 零增益（默认无解的唯一候选 CubeF32 在 faces=1 下
  先验照样无解）、faces 单修反噬（(512,4,6,True) 伪精确照常报价）——面数与金额须同批改，
  两条路都堵死。`bpp_candidates` 形参保留为工具面口子，默认顺序 (4,8,2,1,16,32,64) 不变。
- slice 数不可直读（探针 `api_surface` 已固化该负结论，复跑即得）：DDS 立方图落 `Texture2DArray`
  类，5.4 类级成员 78 个、5.3 69 个，无 array/slice/dim 成员，`blueprint_get_size_x/y` 也只有
  Texture2D 才有 -> 面数口径（B2）只能等真实工程只读复测，不是换 API 能撞出来的。


## #11 C 源信息可达性探针（`source_scan.py` + `probe_source_artifacts.py`）

B1 之后 C（"直读源图拿真边长"）剩的问题只有一个：源图的**真宽高到底还留在哪**。探针把 DDS
靶导入后三个面同时取证（5.3.2 与 5.4.4 结论完全一致，两版均自清理 6/6）：

- **包内：死路**。uasset 字节里 PNG/JPEG/BMP/DDS 签名零命中（6 张全 0），名字表痕迹只有
  `AssetImportData`——与 B1「存的是裸位图」吻合。判据 `source_scan.py` 纯 stdlib（签名 +
  结构自洽校验），由 `tests/test_source_scan.py` 7 用例离线锁定：认得合成真头与 `.hdr` 分辨率行、拒绝声明尺寸
  自相矛盾的 BMP、64KB 种子噪声零误报、截断不越界不崩。
- **import 元数据：可用的那条**。`get_editor_property("asset_import_data")` 返回
  `InterchangeAssetImportData`，其 `extract_filenames()/get_first_filename()`（含 script_ 同义
  变体）给出导入时记录的**原始文件绝对路径**。原文件在场 -> 读容器头拿真宽高/面数并与
  `src_mem` 乘积互验；不在场 -> 静默跳过。C 的失败面由此缩到「美术是否保留原文件于记录路径」。
- **注册表/`_Source` 对象：不存在**。`<Name>_Source` 在 registry 与 load 面都拿不到；
  `AssetData` 的 Python 面（5.4）没有 `get_export_name`，判存在要用 `is_valid()`。

跑法与 truth 探针同（`-run=pythonscript -script=...probe_source_artifacts.py`，
`PRISM_PROBE_DDS/OUT/KEEP` 同语义），产出 `probe_source_artifacts.json`。
真机坑已钉进代码：5.4 下 python 仍持有最后一次 obj/子对象引用时 `delete_asset` 会在
`ObjectTools::GatherObjectReferencersForDeletion` 空指针崩掉编辑器——清理前必须释放引用并
`gc.collect()`（探针已这么做，之后 6/6 干净）；将来的产品删除逻辑同理。

### B2/C 真实工程只读复测（`scripts/recheck_cube_semantics.py`，2026-10-15 授权）

只读取证：枚举 /Game 全部 TextureCube（按 uasset 盘大小取前 N），冷态度量走域层真管线 +
import 记录 + 原文件头解析；零写入零保存，取证 JSON 只落工程外目录（`PRISM_RECHECK_OUT` 必填）。
结论两组：

- **src_mem 双语义定案**：立方阵列源 = 6×S²×B（÷6 余 0；201,326,592=6×2048²×8 同值 6 张
  全中）；2:1 RGBE 全景源 = 8×S²（必不被 6 整除；134,217,728=8×4096²，hDRI 真边长 4096，
  6×4096²×8×4/3≈1073MB 对上载入实测 1024MB）。两假设按「÷6 余数 + 2 的幂判定」互斥，
  无需旁证即可切分——这是已落地的 D（双假设求解器，POOL_MODEL `v4-cube-dual-hypothesis`）的数据底座。
- **C 定价修复放弃**：16/16 张 import 记录都有原始文件路径，但 0/16 仍在场（散在桌面/
  外置盘/网盘目录，均已丢失）。在场时读头互验仍可用（Radiance `.hdr` 的 `parse_hdr` 已
  进 `source_scan.py` 并由 L0 钉住）；另一半价值是「未保留源文件的资产清单」诊断（2026-10-16 已立项，暂名 `scan_missing_source_files`）。

取证工具自身的两条真机教训（脚本已内置）：工程插件可能预加载**旧版 `prism` 占住模块缓存**
（缺 `streaming` 直接 ImportError）——复测脚本先卸载非仓库来源的 `prism*` 模块再 import；
commandlet 里异常只进引擎日志不可读——兜底 `_fail()` 把 traceback 落盘到取证目录。

## #11 C 缺源诊断的真机校验（`scan_missing_source_files` + `probe_missing_source_files.py`）

C 落地成只读工具后，真机判据自建自清理，**不并进 59 项门禁**：门禁项会重读物化资产，
把源图目录改名的那一刻掺进 59 项会让孤儿/引用/预算三组真值读数跟着漂移，得不偿失。
改为独立探针，跑法（编辑器在线、工程侧已 `plugin-install` 当前仓库代码）：

    python scripts/calib_fixture/probe_missing_source_files.py <bus_dir> <project_dir>

已知真值就是夹具自己的导入源：`<proj>/Saved/PrismFixture/pngs` 改名 `pngs_off` -> 5 张
直连导入靶（`T_BigNoise/T_Used1K/T_Orphan1..3`）的记录路径全部失效 => verdict 必须翻
`missing`；改回 => 必须翻回 `present`；`finally` 里无条件复原。另有编辑器内 `create_asset`
造的 `M_CalibRef` 必须是 `no_record`（无导入记录 != 源丢失），关卡必须进 `skipped.level`
而不是被载入编辑器内存。

**2026-10-16 实测：UE 5.3.2 与 UE 5.4.4 各 12/12 全 PASS**，两版命中同一条签名
`extract_filenames`（`api.import_record_via` 就是跨版本兼容矩阵的观察窗），夹具目录
13 张纹理里 11 张有记录、5 张直连靶两态可复现，`missing_bytes` 随翻转在 0 与 133.7MB
之间移动，翻页窗口 `cap/scanned/truncated/total_available` 一致。

## LOD 族立项前取证（`probe_mesh_lod_api.py`，2026-10-16）

LOD 的**读**面早被 `get_asset_metrics` 真机钉过（`get_num_lods` + 每级 triangles/sections），
但**阈值能不能改**没量过——这决定 LOD 族是做成了写操作，还是只能做只读诊断。故先跑探针：
无头 commandlet 即可（夹具 `CalibMesh` 就是 commandlet 里 `duplicate_asset(/Engine/BasicShapes/Cube)`
造的，StaticMesh 副本不像 TextureCube 副本那样会崩）：

    UnrealEditor-Cmd.exe <uproject> -run=pythonscript -script=scripts/calib_fixture/probe_mesh_lod_api.py -stdout -unattended -nosplash

5.3.2 与 5.4.4 两轮结论**逐条一致**（`class_members` 差集为空），探针默认造完即删（删前释放引用
+ `gc.collect()`，5.4 实证仍持引用时 `delete_asset` 会空指针崩编辑器）：

- **ScreenSize 属性面不可达**：`lod_screen_sizes/screen_sizes/lod_screen_size/lod0_screen_size/
  screen_size_list/lod_info` 六候选全 `Failed to find property`，写面同样全 miss——每级阈值在
  `FStaticMeshRenderData`（非 UPROPERTY），Python 看不见。**LOD 阈值改不了，这条死路已钉死**。
- **`is_lod_screen_size_auto_computed()` 可读**（noarg 形态，两版均 True）：这是诚实判据的根，
  引擎自动算阈值时，"美术阈值不合理"这类结论不成立，工具必须回传该标志而不是猜。
- **每平台 Min LOD 可读写**：`get_minimum_lod_for_platform("ALL")` 基线 -1（无覆盖），
  `set_minimum_lod_for_platform(key, 3)` 后回读相符（ALL/Windows/WindowsNoEditor 三键均验）。
  `get_minimum_lod_for_platforms()`/`get_min_lod_for_quality_levels()` 回传的是 TMap
  （形态 `(Map, 0)`，out-param 风格），画质等级面的 `set_min_lod_for_quality_levels` 传 list 会
  `Failed to convert parameter` —— 需构造 `unreal.Map`，第一版不做该面，如实记为待办。
- **`unreal` 顶层 LOD 类面** 5.4 56 项 vs 5.3 54 项，差集只有 4 个 HLOD/Landscape 相关类
  （HLOD 族要立项时再钻）。`StaticMeshEditorSubsystem`/`StaticMeshEditorLibrary`/
  `AssetToolkitEditorSubsystem` 三个正门在两版 Python 上**都不存在**，候选链只能挂在资产身上。
- 探针产物 `probe_mesh_lod_api.json` 的 `verdict` 就是立项判据三档：`write_ok`/`read_only`/
  `nothing`。本轮 = `write_ok`（via `platform:ALL`）-> LOD 族按「只读扫描 + 每平台 Min LOD 写」立项。

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

同一套 `run_fixture`（59 项：域层 `unreal` API + 规则 + 枚举不变量 + v1.5 优化族属性读写往返 +
HISM 折叠↔回滚）与 `verify_cook_chain`（cook 9 项 / package 10 项）在不同引擎版本各跑一次即成矩阵。当前实测：

| 引擎 | L2 夹具 run_fixture | cook 全链路 | package 全链路 |
|---|---|---|---|
| UE 5.4.4 | **59/59**（2026-10-14 真实大工程复跑、同会话两轮；含 v1.5 优化族 28 项 + HDR 高位深/材质实例/压缩同名 no_op 靶 + 一包一条/冲突拒写 3 项不变量；errors=0。2026-10-10 首轮 50/50。2026-10-15 在独立沙箱 `PrismCompat54` 用加了显式关 VT 钉子的夹具复验 **59/59**，含 `--hism-roundtrip`） | 9/9（695 包/0 error） | Development + Shipping 各 10/10 |
| UE 5.3.2 | **59/59**（2026-10-15 沙箱 `PrismCompat53` 补跑 v1.5 新增项，含 `--hism-roundtrip` 真折叠 1500→1 HISM(实例 1500)→回滚 1500。首跑红两条 `streaming.clamp_target_planned` / `streaming.vt_write_blocks_cube`——根因是 5.3 导入器按 `AutoVTSize` 替大图开了 VT，夹具钉成真值后转绿，产品代码未改） | 9/9 | Development + Shipping 各 10/10 |

- 域层跨版本靠多签名 `try` 兜底，`tried` 实测两版一致：`used_by`/`get_dependencies`=`name+opts`；纹理尺寸无直读 API → `blueprint_get_memory_size` + 源盘反推；5.3/5.4 `EditorAssetLibrary.move_asset` 皆 `AttributeMissing` → 走 `rename_asset` 兜底。
- 5.3.2 实测签名（本轮补跑）：`LevelEditorSubsystem.new_level` 只吃 1 个参数（5.4 的
  `new_level(dest, False)` 在 5.3 抛 `takes at most 1 argument`，夹具多候选兜底命中）；HISM 路线 A
  与 5.4 走同一条（裸 Actor + `new_object` HISM 根组件，`get_instance_count` 回读 1500）；Nanite 仍命中
  `nanite_settings.enabled`；`EditorAssetLibrary.move_asset` 依旧 `AttributeMissing` → `rename_asset` 兜底。
- 夹具生成器坑（已修）：**UE 5.3 的 Interchange 导入忽略 `AssetImportTask.destination_name`**（包名由源文件名决定），复用同一 PNG 造改名基准贴图（`T_CalibRef/Broken/Stub`）在 5.3 建不出来，连带 5 项 refs/copy 校验失败。修法是这些改名基准改走 `EditorAssetLibrary.duplicate_asset`（显式目标名，跨版本稳）→ 5.3/5.4 均回到 22/22。
- package 的 C++ 宿主坑（已内化）：`-build` 带 `-nocompileeditor`，cook 阶段用编辑器进程加载工程模块——给 C++ 工程跑 `--mode package` 前须先各编一次 **Editor 与 Game 两个 target**，否则 cook 报 `The game module '<X>' could not be found` 直接失败（蓝图工程无此问题，但蓝图工程又没有 Game target 可 `-build`，故 package 测不了）。本轮 5.3 package 用一次性 C++ 宿主验证通过。
- 5.4.4 实测签名命中（本轮夹具矩阵）：Nanite 走 `nanite_settings.enabled`；MaxSize 走
  `set_editor_property(max_texture_size)`；材质混合模式走 `get_blend_mode()`；`used_with_nanite`
  材质本体直读、材质实例必须 `get_base_material()>used_with_nanite`；纹理尺寸无直读 API（Cube 全靠源盘反推）。
- 本机覆盖状态：**5.4.4 夹具 59/59 + cook 9/9 + package Dev/Shipping 各 10/10**；
  **5.3.2 夹具 59/59（2026-10-15 补跑，v1.5 优化族与 HDR 台账已在 5.3 真机钉住）**。
  更多版本（5.5/5.8/5.1/5.0）须在别的机器补跑——本机注册表里的 5.1/5.5 是卸载残留，只剩 5.3/5.4 两套引擎。

## 工具覆盖矩阵（当前 31 个 MCP 工具）

| 工具 | L0 离线用例 | L2 夹具 checks | 仍欠（L3 真实工程） |
|---|---|---|---|
| `ping` | test_bus | ping.bridge_alive | - |
| `list_projects`（目录已删/搬走的**残留指针**进 `missing`，不进 `projects`、不可被 `project=` 选中） / `list_perf_rules` | test_registry(+残留 dir_exists 用例) / test_resolve(+残留不入自动池、点名残留报缺失路径、missing 拆分 3 用例) / test_rules | -（随 report 间接，真实注册表 7 条直连冒烟） | - |
| `describe_asset` / `get_asset_references`（classify_soft 硬/软区分） | test_assets(+2 用例:classify_soft 降级稳定键/_merge_detail 并集) / test_pr05_contract | refs.classify_soft_hard（硬引用真值靶 `M_CalibRef`→`T_CalibRef`） | 大引用面性能；软引用正样本抽检 |
| `scan_broken_references`（悬空/坏引用检测） | test_assets(+2 用例:降级结构键常驻/_dep_resolution_status 三态) | refs.broken_missing_target（已知 missing 靶 M_CalibBroken→T_CalibBroken + 健康靶负控） | 真实工程重定向桩(redirector)修复闭环、软引用漏检面（L3） |
| `fix_broken_references`（写·双钥） | test_migrate(+4 用例:双钥守卫/无引擎降级[读+写]/dry_run fixable-missing 路由) | refs.fix_plan_routing（真机路由 stub 靶进 missing） / refs.fix_write_honest（confirm 落盘 executed 不崩、诚实回报） | redirector 桩自动修复正样本（5.4 python 不留桩 → L3）；missing 需人工 VCS 还原/重指向 |
| `get_asset_chain`（环/god/影响半径） | test_assets(+SCC 纯逻辑) / test_buscall | -（小图 smoke） | 真实大工程的大闭包耗时、god_min_refs 阈值手感 |
| `scan_orphan_assets`（一包一条 + 单包引用查询失败降级） | test_orphan_degrade / test_scan_orphan_ref_failure（3 用例：单包失败不带走整场扫描、整窗全失败仍报 UE_API_MISMATCH、降级结构键常驻） | orphan.targets_found / map_not_orphaned | 真实工程假阳性抽检 >=20 条 |
| `scan_missing_source_files`（缺源诊断：import 记录路径在场性五档判据） | test_source_files（16 用例：降级结构键常驻、folder/limit 归一、`_import_record` 四条签名+属性面兜底、脏值不得变成路径、`_src_verdict` 五态、`_src_scan_skip` 排除、`_is_under` 分隔符归一、`_source_rollup` 计数/聚合/截断） + test_opt_server_contract（参数转发与超时） | 独立探针 `probe_missing_source_files`：5.3.2/5.4.4 各 12/12 PASS（present<->missing 往返 + no_record + 关卡排除），不进 59 项门禁 | 客户工程 16/16 有记录、0/16 在场已由 `recheck_cube_semantics.py` 取证；他人机器/网盘路径的记录形态（本机判 missing 属正确行为） | 
| 注册表枚举去重（`_dedupe_by_package` / `_export_object_path`：多导出包一包一条） | test_asset_enumeration_dedupe（7 用例：窗口挤占、类误标、`prefer_terms` 选代表导出、无 `object_path` 才回退 `package.leaf`、孤儿计数） | enumerate.scan_one_row_per_package / enumerate.opt_rows_unique_and_measurable | 5.0/5.1 无 `get_object_path` 的回退形状只有离线用例覆盖，待该版本真机复跑 |
| `get_asset_metrics`（真大/假大+材质槽；立方图另回传 `source_layout`，#11 D） | test_metrics_degrade / test_metrics_runtime / test_source_probe_guard（探针缓存与开关） | metrics.cube_measurable / real_4k_is_runtime_heavy / capped_4k_is_disk_only_bloat / mesh_material_slots | 真实美术资产压缩格式下 est 口径偏差；碰撞面数 5.4 `UStaticMesh` python 无访问器（绑定限制，见资产表 CalibMesh 行）|
| `list_level_actors`（compose） | test_actors_compose | compose.world_ge_1400 / cube_batching_opportunity | 真实 WP 关卡全载成本 |
| `read_editor_log`（P2 指纹归一+资产归因+按会话切分） / `attribute_cook_errors` | test_logscan(+3 会话用例:多会话拆分/每会话计数/单会话不标多) / test_attribute | -（纯离线，真实工程日志直读验证 distinct 194->10、LogPackageName 196 合 1 组归因 195 资产） | 与真实 cook 日志联动的归因抽检 |
| `scan_folder_assets`（目录级预算 by_dir） | test_folderscan(+5 用例:预算/深度/cap-truncated/子树基) | -（纯离线磁盘，无引擎 API，不需夹具靶；report 间接） | 真实大工程目录占比手感 |
| `get_perf_report`（12 规则+降档+ROI 注解+vs-上次 diff+可配采样/cap） | test_rules(+4 用例:annotate_roi 纯逻辑/run_report 带 roi_summary/report diff vs previous/snapshot disabled) / test_rules_runtime / test_rules_opt(26 用例:5 条 v1.5 新规则纯逻辑+ROI 两本账 unit+high_bpp 三档收益与"默认不代改压缩"+缺桥时必须播报不静默+安全阀 4 用例(oversized_mip 不因不报价隐身/可信窗口仍 error/uncompressed 三档只撤金额/overflow 摊开不确定量) | report.fake_big_downgrade_evidence / real_4k_stays_error / texture_evidence_upgraded / cap_truncates_and_flags_hidden_errors / sampling_fields | sample_size/cap 可配（默认 top-80/cap=50）；大工程须显式调大 sample_size 或缩 scope。5.4 真机实测 sample_size 80→200 使 total 245→356（暴露 73 个被旧硬编码藏起的 error）；本轮 160 抽样在 14.38GB 美术工程得 381 findings（error 139/warn 242，含 7 条 high_bpp、0 条误报 Nanite 冲突）|
| `cook_package` / `get_cook_status`（cook+package） | test_cook(含 `test_package_command_template` package 模板)/ test_tasks | `--mode cook` 真机 9 项：uproject/编辑器关闭前置/dry 组装(+maps 定位)/双钥启动/状态收敛/succeeded(exit0)/cooked `CalibMap.umap` 落盘/`attribute_cook_errors` 0 错误组（UE5.4 实跑 BUILD SUCCESSFUL，695 包/0 error）。`--mode package` 真机 10 项：另走 `-build -cook -stage -pak -archive` 全链路，校验归档内 `.exe`+`.pak`（UE5.4 实跑 BUILD SUCCESSFUL/exit 0，exe+9.6MB pak 落盘/0 error 组；`--configuration Shipping` 另跑亦 10/10） | 跨版本 cook/package 兼容（5.3/5.8 各跑一次归档 matrix.json）；真实大工程 package 抽样 |
| `preview_asset_migration` | test_migration_preview | - | - |
| `migrate_asset_rename` / `_move` / `_asset`（写） | test_migrate / test_migrate_engine_shapes（双钥/dry_run/回滚/属性缺失形状） | migrate.rename_applies_redirect / rename_restored / move_applies / move_restored / copy_dryrun_plan / copy_applies / copy_conflict_refused（真机 rename_asset + 5.4 move→rename 兜底，内存态自还原；copy 走 `migrate_asset` duplicate 带走 uses 闭包 2 包 + 目标目录自动建 `make_directory`，内存态不落盘；每次跑用唯一 dest 并清理历史 `copy*` 残留，同名冲突必须结构化拒写） | 真实工程提交后引用复核；`fix_up_redirectors` 5.4 python 未绑定（诚实报告未确认清理，编辑器手动兜底） |
| `scan_texture_streaming`（只读池诊断，probe_source 可关，安全阀见上） | test_streaming_model（42 用例：钳尺寸/VT 可行性/格式四分类 compressed-uncompressed-no_stream-high_bpp-unknown/`runtime_ledger` 实测优先不重复乘/两本账汇总封顶/池对照/ini 优先级/源边长安全阀 5 用例（撤金额留动作、实测仍报价、缺键不挡、汇总不确定度）/反推边长等价类审计 3 用例（exact=与 2 的幂先验自洽而非证明、答案必回算自洽、sqrt2 带可伪 exact） + test_fixture_dds（14 用例：DDS 已知真值靶字节布局 + 5.3/5.4 实测反推快照锁，含「伪精确仍报价」与方案 1 反证 2 条）) + test_opt_texture（降级结构键含 high_bpp、cap 与窗口独立、`_advice_for` 不印 None 金额） | streaming.clamp_target_planned / clamped_texture_not_reflagged / editoricon_is_permanent_not_uncompressed / vt_skip_cube / hdr_bucketed_high_bpp / hdr_runtime_not_bc_estimate / hdr_measured_not_multiplied_again / hdr_metrics_and_scan_same_ledger / hdr_not_auto_recompressed / summary_separates_ledgers / summary_counts_high_bpp / pool_ini_read / enumerate.scan_one_row_per_package / enumerate.opt_rows_unique_and_measurable | #11 B1 已量（`probe_source_truth`：5.3.2/5.4.4 逐字节一致，6 靶里 3 张伪精确、1 张阀生效）；方案 1 先验已否决，仍欠 B2 真 Cube 面数语义（需在真实工程只读复测 hDRI 那一张）；真实大工程的 `est_runtime` 与实测显存偏差；池需求上界 vs 实际驻留的手感（5.4 真实工程：抽样 140 张纹理池需求上界 4.2GB，其中 7 张 TC_HDR 立方图占 1.8GB） |
| `opt_texture_set_virtual` / `opt_texture_clamp_max_size` / `opt_texture_set_compression`（写·双钥） | test_opt_texture(+双钥/字符串 bool 归一/负 max_size/建议文案/high_bpp 闸门/别名解析/同名格式 no_op + `_enum_member` 文本形态 + `_clamp_decision` 安全阀 4 用例(撤金额留计划/no_op 提示复测/widen 不编代价/可信照报价) + `_acc_savings` 撤回金额不进两本账) + test_opt_server_contract(脏 bool 不建客户端/export 原子写) | streaming.clamp_dry_run_does_not_write / clamp_write_honest / clamp_readback_from_disk / clamp_restored_for_next_run / vt_write_blocks_cube / compression.already_target_is_no_op / conservative_gate_still_blocks_high_bpp | 重压缩时机与盘上体积回落（改压缩需编辑器重压缩，cook 前磁盘不变）；`bUsedWithVT` 需工程开 VT 才有真收益；TC_HDR 需 `only_uncompressed=False` 才动（默认挡在闸门外） |
| `scan_nanite_candidates`（只读） | test_opt_nanite（27 用例：分类六态 + 降级键常驻 + `base_material()` 三态解包 + 材质清单非 null） | nanite.target_blocked_before_write / conflict_detected / material_usage_readable_via_base_material / material_instance_usage_not_null | 真实高模候选阈值手感（`min_triangles`）；真实工程某网格 5 材质经 `get_base_material()` 全部读到确定值 |
| `opt_mesh_set_nanite` / `fix_nanite_translucent`（写·双钥） | test_opt_nanite（三条版本签名钉死 + 写不进必须 failed + 半透明拦截 + blend_mode 读不到保守跳过） | nanite.skip_translucent_blocks / force_write_honest / fix_write_honest / self_restored | 材质 `bUsedWithNanite` 只报不改（动材质另议）；真实工程警告回落抽检 |
| `analyze_instancing`（只读，D2 路线 B） | test_opt_instancing（分组/阻断/drawcall 前后纯逻辑） | compose.instancing_groups | 折叠动作本身交给 `opt_collapse_hism`；真实工程关卡仍需人工确认 blockers |
| `opt_collapse_hism`（写·双钥，路线 A） | test_opt_hism（25 用例：拒收 blockers/材质覆盖/over_limit + 双钥先于引注 + 假引注折叠→清单→回滚闭环 + 5.4 路线钉死（裸 Actor + `new_object` HISM 根组件、专用类/旧组件名兜底/什么都没绑的诚实失败）+ 实例写不满/变换读不到时整组退回不销毁 + 回滚网格只认回读） | hism.plan_does_not_touch_scene / collapse_needs_double_key / bad_mode_is_rejected；`--hism-roundtrip` 另跑 collapse_reduces_actors（含容器被认成已实例化）+ restore_round_trips + restore_puts_meshes_back | 清单只保类/网格/变换/标签/子关卡：碰撞、光照绑定、蓝图绑定不承诺复原；对象名由引注重分配（身份靠 label） |
| 基建（bus/envelope/cli/pluginpack/resolve） | test_bus / test_buscall / test_cli / test_pluginpack / test_resolve | - | - |

## 新功能测试纪律

0. **公开仓库纪律**：入库文件（`docs/`、`README`、代码注释）不得出现客户工程名、真实机器路径、内部设计稿内容；逐日排障/手动测试记录一律放 `_internal/`（已 gitignore），不随仓库分发。

1. 新 domain 函数：必须带无引擎降级用例（结构键常驻），命名前缀 `get_/list_/scan_/describe_/read_`。
2. 新写操作：默认 dry_run + 双钥用例 + 回滚路径用例（范式见 test_migrate.py）。
3. 触碰 unreal 真机 API 路径：给夹具加一个已知真值靶（能反推的先反推），并在本文档真值表登记。
4. 规则改动：test_rules*.py 用例必更新；报告字段变更需带 `total/truncated/cap` 断言。
5. 提交前 `python -m pytest` 全绿；跨版本改动在 L2 各引擎版本各跑一次并归档 matrix.json。

## 已知限制

- 引擎侧竞态（5.4 真实工程实测崩溃，非本仓库可控）：改 `MaxTextureSize`/压缩格式会异步重压缩贴图，
  此期间调 `blueprint_get_texture_source_disk_and_memory_size` 走 `FTextureSource::CalcMipSize`，会撞
  `Assertion failed: MipIndex < Block.NumMips (Texture.cpp:2722)` **直接崩掉编辑器**，Python 的 `try` 拦不住断言。
  护栏：同一路径只探一次（会话缓存 `_source_probe`），批量写后的复扫传 `probe_source=False` 绕开，
  关掉后 `sampling.source_probe=off` 显式播报、钳尺寸判定退化为运行时边长（不会静默给偏大的收益）。
  回归见 `tests/test_source_probe_guard.py`（6 用例：只探一次/关闭不触碰/失败不重试/字段退化/缓存复位）。
- 夹具不测渲染性能（无材质/光照变体），它校准的是**工具判定逻辑与 API 命中率**。
- 夹具的写校验是**内存态自还原**：强杀编辑器（`Stop-Process`）会留下未保存改动，下次启动弹
  `Restore Packages` 模态框把桥自启卡住（桥在弹窗后才起）。要么用 `run_fixture --close-editor`
  优雅关闭，要么重启前删 `<工程>/Saved/Autosaves/PackageRestoreData.json`。
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
- **源边长反推的等价类残余**（`source_side_exact=false` 时要当心）：源侧只有
  `source_memory_bytes` 可读，边长 = sqrt(源内存 / (面积因子 x BPP))，多解是常态。#11 已把 Cube 的
  两条存法定案并落进 D（`streaming.source_layout_from_memory`）：先按立方阵列 6xS^2xB 解，无 2 的幂解
  再按 2:1 全景 2xS^2xB 解。那张 5.4 真实工程 `TC_HDR` 立方图（曾反推 2365、估 340MB，载入实测
  1024MB）现在直接命中面边长 4096 + `source_layout=panorama_2to1`，3 倍低估归位。仍未解的是**同一假设
  内部**的多解（S x sqrt(BPP) 为常数）：`exact=true` 只说明「与 2 的幂先验自洽」，不是边长被证明；
  非 2 的幂边长（实测那张 3686）两假设都无解 -> 维持阀。读数前先看 `runtime_basis` 与 `size_basis`，
  要准确值就让纹理先进编辑器内存（打开引用它的关卡/资产）。

## HISM 折叠的 5.4 真机口径（v1.5 PR-5，本轮实测）

`--hism-roundtrip` 会把 CalibMap 真折叠成一个 HISM 再按清单回滚（写关卡两次）。
跑法：

```bash
python scripts/calib_fixture/run_fixture.py --project <P> --editor <UnrealEditor.exe> \
    --hism-roundtrip                      # 重建关卡真值 + 59 项校验
python scripts/calib_fixture/checks.py <bus_dir> <project_dir> --hism-roundtrip  # 只跑校验
PRISM_FIXTURE_RESET_MAP=1 ... --skip-assets  # 地图只数量不对时强制重建
```

真机事实（全部用只读探针 + 命名表核对得出，不是推测）：

- **UE 5.4.4 的 Python 里根本没有 HISM/ISM 的 Actor 类**：`dir(unreal)` 无
  `HierarchicalStaticMeshActor`/`InstancedStaticMeshActor`，476 个
  `UnrealEditor-*.dll` 的名字表（ASCII + UTF-16）零命中，引注源码也无此类。
  引注自己的实例化工具（`MeshMergeUtilities`）走的就是"裸 AActor + 实例组件"。
- 组件真名是 **`HierarchicalInstancedStaticMeshComponent`**，不是想当当的
  `HierarchicalStaticMeshComponent`；照错名找组件会永远 miss（旧名保留为兜底候选）。
- `Actor.add_component_by_class` 是 `ScriptNoExport/BlueprintInternalUseOnly`，不进脚本绑定。
  可行入口：`unreal.new_object(cls, actor, name)` 造组件 →
  `actor.set_editor_property("root_component", comp)` 挂上去。
- `add_instances(transforms)` 单参数直接 `TypeError`（绑定的是 `(transforms, bWorldSpace)`）；
  双参能写但**返回空列表**，写入数只能取前后 `get_instance_count()` 的差。
- **持久化已验证**：这样造出的容器存盘→重载地图后 Actor、组件、实例数、网格全在。
  夹具里的它是硬依据：折叠后存盘的 `.umap` 名字表里只剩
  `HierarchicalInstancedStaticMeshComponent`/`PrismHISMComponent`/`Cube_HISM`，`StaticMeshActor` 零命中。
- 属性保真可读到的有限：`mobility` 抄得动，`b_cast_shadow`/`collision_profile_name`
  读不到就记 `fidelity_skipped:...`，不假装一致。
- 材质覆盖判定连踩四个坑（`get_materials()` 长度、网格槽声明为 None、
  覆盖数组被预填网格自身材质、引注默认材质两条路径不同），最终口径：
  **候选与网格自身材质按路径比对**，同包裹不同导出名算同一个、两侧都是引注默认材质算同一个，
  判不准走 `material_override_unknown` 记账放行，不得拒收整组。

本轮真机照出的两个缺陷（离线用例反馈不了，因为假引注按同一个错误假设写）：

- **回滚丢网格**：`_restore` 把网格往 `AActor` 上写（真机既没 `actor.static_mesh` 属性也没
  `set_static_mesh` 方法），只要没抛异常就 `break` 当成功——数量对了，地图里却是 1500 个空壳。
  现在写进 `StaticMeshComponent` 并**只认 `_mesh_probe` 回读**，落不到位置记
  `restore_mesh_missing` 并让 `success=False`，`failed` 明细封顶 20 条 + `failed_truncated`。
  对应真值校验：`hism.restore_puts_meshes_back`。
- **折完不自认**：容器是裸 Actor，`analyze_instancing` 原先按 Actor 类名判实例化，
  会把它漏成"无网格 Actor"，合批账数不到自己头上。现在看组件：
  `root_component` 或任一 `ActorComponent` 命中 ISM/HISM/Foliage → `excluded_instanced_actors`。

## 三本账（别把它们加在一起）

| 账 | 口径 | 谁会动 | 本轮真实工程数字 |
|---|---|---|---|
| 源盘 `.uasset/.umap` | `scan_folder_assets` / 磁盘文件清单 | 只有重存源资产才动；钳 MaxSize 不改源图 | 9243 文件 14733.3 → 14729.3 MB（**−4.0 MB**，当天重存 1066 个资产文件（含 1 个关卡）） |
| 运行时流送池 | `scan_texture_streaming` 的 `pool_demand_mb`（同一 399 大容量窗口） | 钳尺寸/开 VT/改压缩的收益都记这里 | 6131.8 → 1319.7 MB（**−4812.1 MB / −78.5%**） |
| cooked 产物 | 真重编后的 `.uasset`（`cook_package`） | 钳尺寸要重压缩才落到盘 | 本轮未重编，不报数字 |

- 钳 MaxSize 的收益写在**池与 cooked**，不写在源盘：源图和它的全部 mip 仍然在 `.uasset` 里。
  所以"改了上千张贴图源盘只降了几 MB"是预期行为，不是效果没生效。
- **VT 与钳尺寸不可相加**：先钳尺寸再看 VT，钳完后候选集的 `pool_release_mb` 会归零（本轮即如此：
  3116 个候选在钳到 1024 后可释放 0.0 MB，故未批量开 VT）。
- 工程 ini 没写 `r.Streaming.PoolSize` 时 `verdict=unknown`：建议人工设一个上限，否则溢不溢根本无从判定。
- 重量口（循环验收用）：`scan_texture_streaming` 的 `probe_source=True` 会把资产装进内存，
  **批量写完不要立刻重测**（引用冷热态交错时会把编辑器拖崩）；要快就用 `probe_source=False`。



