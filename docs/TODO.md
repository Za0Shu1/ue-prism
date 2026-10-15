# TODO — 纹理/Nanite/合批 三类优化功能（v1.5 已落地，5.3/5.4 矩阵达标）

> 记录于 2026-10-08；2026-10-09 实现 PR-1/2/3 + PR-4 只读部分；2026-10-10 在一个新导入大批
> 美术资源的 UE 5.4.4 测试工程上完成 **L2 50/50 全 PASS + L3 真实工程抽检**，修掉真机暴露的
> 4 个缺陷（HDR 高位深台账、材质实例 Nanite 标志、日志归因噪音、压缩同名误判为待改），
> 另记 1 个未修口径（冷态 Cube 源边长反推，见下「真机复盘」）。
> 2026-10-14 补上 **PR-4 路线 A**（`opt_collapse_hism` 真折叠 + 回滚清单 + `--hism-roundtrip`
> 真机往返），5.4.4 夹具 **59/59 全 PASS** + 真实大工程复跑。2026-10-15 **补跑 UE 5.3.2：59/59 全
> PASS**（并在 5.4.4 沙箱用同一轮夹具复验 59/59，顺带钉住 5.3 导入自动开 VT 的真值坑，见复盘 #12）。
> 剩余：5.5/5.8/5.1 等版本各补一行矩阵（本机只剩 5.3/5.4 引擎）；#11（冷态 Cube 源边长反推）
> 已按 A″ 止血（撤金额不撤发现 + 不确定度单列）；B1 已知真值靶已入库并量清歧义面
>（5.3.2/5.4.4 逐字节一致，6 靶 2 正确 / 3 伪精确 / 1 阀生效）；压缩先验（方案 1）已实测
> 否决（覆盖式把正确金额推错、fallback 式零增益、faces 单修反噬）；B2 面数语义已经真实
> 工程只读复测定案：**双语义**——立方阵列源 6 面全算、2:1 全景源存整图（÷6 余数 +
> 2 的幂判定可无旁证区分）；C 定价修复放弃（16/16 张 import 记录都有原文件路径，
> 但 0/16 在场）。**D 双假设求解器已落地**（POOL_MODEL `v4-cube-dual-hypothesis`，
> 全景类 3 倍低估归位）；仍待拍板：A″ 阀加宽 / 缺源文件诊断。
> 触发来源：对 UE 5.3 工程 ProjectA 的实测分析（详见会话），暴露三类可产品化的优化点。

## 背景（ProjectA 实测坐实的痛点）
- `/Game` 约 20.6 GB；打包后 texture streaming 池超限，被迫把 `r.Streaming.PoolSize` 调到很高。根因：超大/未压缩大图（8K/16K/32K、`TC_EDITOR_ICON`）把整条 MiP 塞进池。
- 引擎日志 ~14,846 条 Warning，几乎全是「半透明材质挂 Nanite 网格」（`Car011`、`zhujie_SM_zhujie_*`）——Nanite 不支持 Translucent，静默降级 + 刷屏。
- 关卡 `Main_City`：2096 Actor，2003 散装 StaticMeshActor，实例化占比 0%；≥5 重复同网格 49 组（`SM_C1_B_Mix212`×171…）——合批机会巨大。
- 已知坑：`TextureCube`/`VolumeTexture` **不支持**流式虚拟纹理（编辑器里勾选框置灰）；`TC_EDITOR_ICON` 等非压缩格式使 VT/池收益打折。

## 开放决策（已按下列口径实现；要改判直接说）
- [x] **D1 命名**：新增 `opt_` 族（纯优化写入：降尺寸/开 VT/改压缩/开 Nanite），`fix_` 保留给
  纠错（`fix_nanite_translucent`/`fix_broken_references`），`migrate_` 给搬移。`AGENTS.md`
  「写操作命名」已补一行。
- [x] **D2 HISM 合批**：**路线 B**（`analyze_instancing` 只读，`export=True` 把方案 JSON 原子写到
  `<bus>/plans/`，绝不碰关卡 Actor）与**路线 A**（`opt_collapse_hism` 真折叠）都已落地。路线 A 的
  安全网：默认 `mode=plan` 不动场景 + 双钥才折叠 + 落回滚清单 JSON（`mode=restore` 按清单重建源
  Actor 并销毁 HISM）+ 强提示「先提交 VCS 或另存关卡副本」——UE 对关卡改动无事务回滚，碰撞/光照/
  蓝图绑定不承诺复原。
- [x] **D3 节奏**：照原计划 PR-1 只读 → PR-2 纹理写 → PR-3 Nanite 写 → PR-4 只读部分。

## 实现落点（2026-10-09）
- 共享口径：新增 `prism/streaming.py`（纯 stdlib：池估算数学 + `Config/**/*.ini` 解析），
  domain 与 `rules.py` 同源取数，免得「工具报 8GB、报告报 6GB」两套账。`rules.py` 不得
  import `prism.domain.*`，需要同源就把数学放这种中立模块。
- domain 新增 `optutil.py`（通用双钥写骨架 `drive`：守卫→解析目标→inspect→dry/execute→
  save→诚实计数）、`opt_texture.py`、`opt_nanite.py`、`opt_instancing.py`、`opt_hism.py`（路线 A）；已登记
  `pluginpack.ENGINE_MODULES`（引擎侧闭包）。
- 收益分两本账：`est_savings_mb`（池/显存）与 `est_permanent_release_mb`（释放恒常驻）不合并。
  `TC_EDITOR_ICON` 系是「无 mip+禁流送→整张恒常驻池外」，不是「4B/px 未压缩」——开 VT 对它
  无从谈起，钳尺寸仍有效；改回 `TC_Default` 的收益是变可流送，别按省 3/4 池报价。
- server 新增 9 个工具（`scan_texture_streaming`、`opt_texture_set_virtual`、
  `opt_texture_clamp_max_size`、`opt_texture_set_compression`、`scan_nanite_candidates`、
  `opt_mesh_set_nanite`、`fix_nanite_translucent`、`analyze_instancing`、`opt_collapse_hism`），共 30 个。
- 规则 `get_perf_report` 7→12 条：+`texture_streaming_overflow`（含 ini 池对照）、
  `oversized_mip`、`uncompressed_texture`、`nanite_translucent_conflict`（离线直读编辑器日志）、
  `drawcall_batching`；三档 PROFILES 各配阈值（mobile 更敏感）。
- 测试：L0 29 文件 / 345 用例全绿；L2 夹具 +28 项真值校验（钳尺寸写往返、VT 拦 Cube、
  EditorIcon 归类、Nanite 冲突全链路自还原、合批 plan_only、HDR 高位深台账、材质实例基类解包、
  压缩同名 no_op）。
- ✅ **真机 L2 已跑**（2026-10-10，UE 5.4.4-35576357）：`run_fixture` **50/50 PASS**、commandlet
  errors=0（矩阵/日志产物留本地不入库）；证据 `measured/est_bc=8.52`、
  `T_CalibHdr` `runtime_mb==measured==1.062`、`high_bpp_count=1`、`M_CalibInst`
  `usage_api="get_base_material()>used_with_nanite"`。
- ⚠️ 未发版（AGENTS.md 发布纪律：需用户在当前对话明确授权「发布 vX.Y.Z」）。

## 真机复盘（2026-10-10 L2/L3 暴露：4 个已修 + 1 个待拍板）
- **#7 TC_HDR 运行时被低估 6~8 倍**：`streaming` 只按 BC(1B/px) 折算，`TC_HDR` 立方图在报告里
  "隐身"（真实工程 7 张共 1820MB 运行时/1592MB 潜在收益，最大 `hDRI` 1024MB 原本一条都不报）。
  修法：新增 `POOL_MODEL_VERSION="v2-format-aware-runtime"` + `format_class()` 桶 `high_bpp`
  + 一本账 `runtime_ledger(metrics)`（**实测 `memory_bytes` 达标即采信、不再乘倍率**；占位值才
  `est x ratio`），`rules`/`metrics`/`scan_texture_streaming` 同源；high_bpp 只出"钳尺寸/开 VT"
  建议，**不自动建议改压缩**（改格式=美术决策）。
- **#8 `used_with_nanite` 对材质实例恒 null**：5.4 直读 `MaterialInstanceConstant.used_with_nanite`
  抛异常 → 提示"需要补 bUsedWithNanite"永远漏报。修法：`optutil.base_material()`
  （`get_base_material()` → `prop:base_material` → 原样返回）解包基类材质后重读，
  `opt_nanite._material_view` 行内记 `usage_api`。夹具加 `M_CalibInst`/`CalibMeshInstance` 靶。
- **#9 `nanite_translucent_conflict` 把日志枚举当资产名**：5.4/5.3 行尾 `[BLEND_Translucent]`
  被塞进 `evidence.names`（假资产名）。修法：过滤 `blend*` token + 收紧真名断言用例。
  注：此为离线规则层（`rules.py`，不碰 `unreal`），由 L0 用例 + 真机日志复验覆盖。
- **#10 `opt_texture_set_compression` 把"已是目标格式"算成待改项**：真实工程 4 张贴图 dry-run
  （`only_uncompressed=False` 强改 TC_Default）报 `planned=4`/收益 896.055MB，其中 3 张本来就是
  `TC_DEFAULT`——既虚报收益、又会白白 save 资产。修法：`_compression_gate` 先做**同名判定**
  （新纯函数 `_enum_member()` 从 `str(枚举)` 取成员名，跨版本/本地化文本取不到就不判），
  已是目标格式一律 `no_op` 且行内不带 savings；同名判定优先于 `only_uncompressed` 闸门。
  夹具补两条回归校验：`compression.already_target_is_no_op`（planned=1，只有 TC_HDR 那张是 change）、
  `compression.conservative_gate_still_blocks_high_bpp`（默认闸门 planned=0）。
- ✅ **#11 A″ 已止血（2026-10-15）冷态 Cube 源边长反推低估**：真实工程 4096 立方 `TC_HDR`（`hDRI`）在纹理
  未载入时被反推成 2365 边长 -> `runtime_mb` 340MB（`runtime_basis=derived_estimate`），载入后实测
  1024MB，差 3 倍。根因：`texture_source_memory` 里 BPP/面数/mip 链三者耦合，非 2 的幂解也满足误差
  容限（`source_side_exact=false` 已如实标注，不算谎报）。落地的口径 = **保留发现、只撤金额、量化不确定度**：
  - `streaming.texture_pool_entry`：条目级新增 `size_basis`（source_exact / source_unverified /
    runtime_px / unknown）与常驻 `source_side_exact`；`source_side_exact is False` 时 `flags +=
    size_unverified`，`clamp_savings_mb` / `compression_savings_mb` 置 **None**（不是 0.0——0.0 的语义
    是"确认没有收益"），`vt_savings_mb` / `streaming_release_mb` 仅在无实测时置 None（实测 MB 与边长
    无关，该照报）；`clamp_to` 与 `actions` 保留，无法用 `min_clamp_mb` 比较时仍给 `clamp_max_size`。
  - `streaming.aggregate()`：单列 `size_unverified_count` / `size_unverified_pool_mb` /
    `size_unverified_permanent_mb` / `savings_withheld_count`；None 金额按 0 计入合计，不猜数。
  - `rules`：`oversized_mip` 不因不报价而隐身（封顶 warn、证据带 `size_unverified`、advice 让先复测，
    #7 的教训）；`uncompressed_texture` 三档只撤金额（格式是直读属性，档位/建议照给）；
    `texture_streaming_overflow` 证据摊开不确定量并在 advice 播报。`annotate_roi` 靠 isinstance 守卫
    自动不计入 MB 总计。
  - `opt_texture` / `optutil`：`_clamp_decision` 不可信 -> `savings_mb=None` + 行内 `size_unverified`
    （不拒绝写入，双钥照旧）；`_advice_for` 不印 `None`；`drive` 记 `savings_withheld` 并在 dry-run
    note 播报"另有 N 项未报价"。
  - 触发条件严格 `is False`：`probe_source=False` 与热态路径（缺键）一律不挡，不会误伤既有读数。
    L0 15 用例钉口径；L2 反向守卫进 `checks.py`（夹具都是 2 的幂靶，必须 `size_basis=source_exact`
    且照常报价、`size_unverified_count=0`）。
  - 本轮**不动 `source_side_from_memory` 数学**（宁可少报价，不可编金额）。
  - 穷举审计（`test_source_side_exact_is_prior_consistency_not_proof` 等 3 用例钉住）给出残余边界：
    `source_memory = side^2 x bpp x faces` 只定住乘积，`side x sqrt(bpp)` 才是被定住的量——
    等价类里 2 的幂解不止一个（POT 真值取 32..65536 十二档 x 七档候选 BPP = 84 个组合全部
    exact=true，其中 60 个会把边长挪到同族的另一点：真 FP32 2048 被认成 RGBA8 4096、真 R8 512
    被认成 256）。所以
    **`exact=true` 的语义是"与 2 的幂 + RGBA8 先验自洽"，不是"边长被证明"**；它保证的只有
    "答案回算的源内存落在容差内"（总量账不跑偏），而按边长折算的比例收益仍可能偏。
    另一个方向同样存在：非 POT 真边长落在 pow2/sqrt2 附近（2896、11585）时，2% 容差会让
    另一档 BPP 的幂解通过校验 -> exact=true 而边长错 ~1.4 倍。候选集内不存在唯一解，
    要把边长真正定死只剩 B2（真实工程面数读数）与 C（读外部原文件）——B 先验已由 B1 靶 + 反证用例整体否决，见下。
  - **B1 已做（2026-10-15，靶与探针入库）**：`scripts/calib_fixture/make_dds.py` 自写 DX10 DDS，
    把「边长 / BPT / 面数」钉成已知真值（5.3 Interchange 忽略 `destination_name` 这条也不再脆——
    包名由源文件名决定，DDS 文件名即靶名）；`probe_source_truth.py` 把靶导进沙箱、走域层真管线读回、
    自清理，**不进 59 项门禁**（靶是未引用资产，并进夹具要先改孤儿/引用类真值集）。5.3.2 与 5.4.4
    每个键逐字节一致：6 张靶的 `source_memory_bytes` 全等于 authored payload；反推 2 张正确、
    3 张**伪精确**（512@16B 被认成 1024@4B；1024@2B/1B 被认成 512@8B/4B）、1 张阀生效（多 slice 的
    `array_size` 全部尺寸 API miss -> 1254 无可信解 -> 撤金额留动作）。数字由
    `tests/test_fixture_dds.py` 14 用例离线锁定（含方案 1 反证 2 条），含「伪精确仍报价」**快照锁**：口径一改必须同步改它。
  - **B 先验已实测否决（2026-10-15，反证 2 用例入库）**：按直读的 `compression_settings` 给候选
    BPP 重排（HDR 系先 8→16）作为产品改动不成立——总量同解时 source_memory 分不出真值，
    重排只是「抢先试中者称王」并盖 exact=true：`T_CalibRGBA8` 4,194,304 = 1024²×4 = 512²×16
    同解，默认顺序先 4 恰好正确，先验顺序先 8 再 16 就推成 (512,16) 伪精确、金额照报；
    夹具 `T_CalibHdr`（TextureCube 标 `TC_HDR` 但源 8-bit，6×128²×4B = 393,216）正属真实工程
    常见的「标 HDR 源却是 8B/px」形态，先验把正确答案 (128,4,6) 推成 (64,16,6)。覆盖式被
    改变答案的靶共 5 个（F32 修好；RGBA8/R8/CubeF32/T_CalibHdr 推错），净亏。fallback 式
    （默认无解才启用）零增益：唯一候选 CubeF32 在 faces=1 下先验照样无解（887@8 False）。
    faces 单修还反噬：只把面数修成 6 不动顺序，CubeF32 变 (512,4,6,True) 伪精确并照常报价
    ——面数与金额口径必须同批改，而两条路都堵死，方案 1 整体否决。`bpp_candidates` 形参
    保留为工具面口子（默认顺序与行为不变）。
  - **B2 已复测定案（2026-10-15，`scripts/recheck_cube_semantics.py` 只读，授权）**：
    既非「6 面全算」也非「只算一面」——`source_memory_bytes` 就是**导入时的原图位图**：
    立方阵列格式源 6 面全算（201,326,592 = 6×2048²×8，同值 6 张全中、÷6 余 0）；
    2:1 RGBE 全景源存整幅全景（134,217,728 = 8×4096²，hDRI 真边长 4096：
    6×4096²×8×4/3≈1073MB 对上载入实测 1024MB；按 faces=6 反推成 2365 时运行时要
    低 3 倍——正是 A″ 止血那笔账的数额来源）。两种语义可用「÷6 余数 + 2 的幂判定」
    无旁证干净切分：边长为 2 的幂的全景 src 必不被 6 整除，立方阵列必被整除，两假设
    至多一个成立、都不成立维持阀。修复提案 **D 双假设求解器**（纯数学：立方类先试
    6 面 ÷6 再试 8·S² 全景）——改金额口径，**等拍板，本轮不动数学**。
    16/16 张里 6 张现口径已正确、9 张全景类走阀或伪精确（D 已修正）、1 张非 2 的幂
    边长（3686）维持不可信。
  - **D 已落地（2026-10-15，`streaming.source_layout_from_memory`，POOL_MODEL v4）**：立方图
    反推改走双假设——先阵列 6xS^2xB（含因子 3 的真值仍归它解释，现口径金额不被改动），
    无 2 的幂解再试 2:1 全景 2xS^2xB；`side` 恒是面边长、`faces` 恒是运行时 6 面，域层新增
    `source_layout` 回传命中的存法。互斥性来自「候选 BPP 与 2 的幂边长都是 2 的幂 → 阵列解
    含因子 3、全景解不含」，2% 容差内也抢不到同一条 src（2^m/3 恒偏离 1 达 33%），L0 用
    7 档边长 x 7 档 BPP 穷举钉死。那张 hDRI 现在给面边长 4096 + `panorama_2to1`（旧口径 2365
    走阀），运行时账回到 1024MB 一档。残余不变：同一假设内部的 S x sqrt(BPP) 等价类（伪精确）
    与非 2 的幂边长（维持阀）。真机侧：夹具 `metrics.cube_measurable` 判据已扩为「立方图必须
    回传 source_layout」（不增项数，59 项门禁不变）。
  - **D 的真机靶缺口（如实记着）**：夹具造不出「2:1 全景导入成 TextureCube」这条路——那是
    美术侧管线（外部转换/插件），引擎导入器没有对应开关，硬造靶只会测我自己的代码。所以 D 的
    全景分支由 L0 穷举（7 档边长 x 7 档 BPP 互斥性）+ 真实工程只读取证脚本
    `scripts/recheck_cube_semantics.py`（现按双假设报告 layout）钉；若日后拿到可复现的导入管线，
    再补进夹具靶。
  - **C 已实证到底（2026-10-15，`probe_source_artifacts`，5.3.2/5.4.4 结论一致）**：包内
    没有源容器——6 张靶的 uasset 字节扫 PNG/JPEG/BMP/DDS 签名**全零命中**，名字表也只有
    `AssetImportData` 痕迹（判据在纯 stdlib 的 `source_scan.py`，7 条 L0 用例钉「认得真头 +
    自相矛盾拒绝 + 种子噪声零误报 + 截断安全」）。但 import 元数据留了一条**可信旁证**：
    `asset_import_data`（`InterchangeAssetImportData`）的 `extract_filenames()/
    get_first_filename()` 返回导入时记录的原始文件绝对路径——原文件在场，读容器头即得
    真宽高/面数，还能与 `src_mem` 乘积互验；不在场静默跳过。C 的失败面从「能不能读到头」
    缩小为「美术是否把原文件留在那个路径上」。`<Name>_Source` 对象在注册表/加载面都不存在。
    **复测把最后一环也定了**：真实工程 16 张立方 import 记录 16/16 在场、原文件 **0/16
    仍在记录路径**（记录散在桌面/外置盘/网盘目录）——C 作定价修复在客户实态=零命中，
    放弃；降为两个小用途：原文件恰在场时读头互验（`.hdr` 的 parse_hdr 已备好 + L0
    钉住），以及可产品化为「未保留源文件的资产清单」诊断（待拍板）。
    性价比排序（D 落地后更新）：**A″ 阀加宽**（如「反推 BPT ∉ {4,8} 也视为不可信」，待拍板）>
    C 降级为「缺源文件诊断清单」（待拍板）。D 已于同日落地，见上条。
    读数口径提醒不变：冷热态敏感的结论先看 `runtime_basis`
    与 `size_basis`。
- **#12 5.3 导入器把 4K 大图自动开成 VT**（2026-10-15 补跑 5.3 时红两条：
  `streaming.clamp_target_planned` / `streaming.vt_write_blocks_cube`）：5.3 的 `UTextureImportSettings.AutoVTSize`
  让源像素 ≥ 4096×4096 的贴图一进工程就 `VirtualTextureStreaming=true`（5.4 起取消该行为），于是钳尺寸/VT
  两条候选被 `already_virtual` 一起吞掉。这是**夹具真值不跨版本**，不是产品缺陷——修法是把“未开 VT 的 4K
  大图”显式钉住（`gen_assets_cmdlet._pin_virtual_off` 写后回读，证据落 `assets_summary.json`），5.3/5.4 各 59/59。
  产品侧同条读数留给用户：5.3 工程里大图 `already_virtual` 多半是引擎替美术开的。
- 夹具自污染提醒：跑过 L2 的工程，编辑器日志会留下 10 条 `M_CalibTranslucent` 的
  `Invalid material ... Nanite` 警告，之后按名归因会命中它们——真实抽检请按资产路径核对或换日志窗口。

## 写操作铁律（所有 `opt_`/`fix_` 遵守，见 AGENTS.md）
- 默认只读；写入 `dry_run=True`+`confirm=True` 双钥才落盘（server 层 `_as_bool` 归一，domain 层再校验 `confirm`）。
- 结果一律 `envelope.make_ok/make_err`；错误不伪装 ok；列表带 `total/truncated/cap`。
- domain 只用 `unreal`+标准库、守 Python 3.7 子集；函数 `@register` 进 `FUNCTIONS`；`server.py` 绝不 import `unreal`。
- 无引擎时降级返回结构键（不删 `unreal` 分支）；跨版本 `unreal` 签名漂移多候选 try，失败报 `UE_API_MISMATCH`。
- 模板参考 `prism/domain/migrate.py`（`_try_op`/`_try_op_named`/`_no_engine`）。

## Feature 1 — 纹理流送池超限（诊断 + 批量 VT + 批量钳 MaxTextureSize）
### 1a 只读分析
- [x] 新 domain `scan_texture_streaming(folder|map, …)`：聚合流送需求估算、Top 贡献纹理，逐张读 `virtual_texture/compression/max_texture_size/never_stream/lod_group`（复用 `prism/domain/metrics.py`：`max_texture_size`、`compression_settings`、`virtual_texture_streaming`），给「建议钳到多少 / 开 VT / 改压缩」。
- [x] 新规则进 `prism/rules.py`（PROFILES + finding）：`texture_streaming_overflow`、`oversized_mip`、`uncompressed_texture`（`TC_EDITOR_ICON`/未钳大图）。→ 进 `get_perf_report` 出优化项 + ROI。
- [x] 离线补充（server 侧，不碰引擎）：解析 `DefaultEngine.ini`/平台 DeviceProfile 读当前 `r.Streaming.PoolSize`，对比估算给「池开多大 / 该降哪些」。
### 1b 批量写入（双钥）
- [x] `opt_texture_set_virtual(paths|folder, dry_run, confirm)`：批量 `set_editor_property('virtual_texture_streaming', True)`。前置：Cube/Volume→`skipped`+原因；不支持压缩→报不写；已虚拟→`no_op`。改完 `save_loaded_asset` + 触发重压缩（漂移 `UE_API_MISMATCH`）。
- [x] `opt_texture_clamp_max_size(paths|folder, max_size, only_if_larger=True, dry_run, confirm)`：批量设 `max_texture_size`（0=不限）；只对超阈值生效；dry-run 出每张 before→after 分辨率 + `est_runtime` 变化。
- [x] `opt_texture_set_compression`：`Editor Icon`→`Default` 等，池收益大头。

## Feature 2 — 批量开 Nanite（并规避/提示透明材质）
### 2a 只读分析
- [x] `scan_nanite_candidates(folder|map)`：按三角面/材质槽列适合开 Nanite 的网格；反查网格材质 `blend_mode`，含 Translucent/Modulate/Additive/双面半透明 → 标「风险（开了也静默失效+刷屏）」。
- [x] 新规则 `nanite_translucent_conflict`（error）：复用 `prism/attributelog.py`/`read_editor_log` 按指纹把已有 Nanite 透明警告归因到具体资产。
### 2b 批量写入（双钥）
- [x] `opt_mesh_set_nanite(paths|folder, enabled=True, skip_translucent=True, dry_run, confirm)`：设 Nanite 开关。跨版本：5.0/5.1 `bSupportsNanite` → 5.3 `NaniteSettings.enabled`/`FMeshNaniteSettings` → 5.4+ 字段再变；多签名 try。命中透明材质默认 `skipped: [{mesh, blend_mode}]`；提示材质还需 `bUsedWithNanite`。
- [x] `fix_nanite_translucent(folder|paths, dry_run, confirm)`：把已开 Nanite 却挂透明材质的网格关掉 Nanite（消警告/降级）。

## Feature 3 — 场景模型优化（HISM/ISM 合并）
### 3a 只读分析（复用既有）
- [x] 复用 `prism/domain/actors.py` 的 `list_level_actors(compose=True)`「重复 StaticMesh 未实例化」机会。
- [x] `analyze_instancing(min_count=5)`（关卡=当前打开关卡；未做 map/folder 选择器）：输出候选组（按**网格包路径**分组，未细分材质/transform 集合）、可折叠 drawcall/Actor 估计、排除已实例化宿主，并把组内混入非 StaticMeshActor（蓝图放置器）标成 `blockers` 提示人工确认——独立碰撞/Lightmap 覆盖/可交互标记**未纳入判定**（需逐 Actor 语义分析，留给路线 A 的执行前置）。
- [x] 新规则 `drawcall_batching`（warn）：把机会清单纳入 `get_perf_report` ROI。
### 3b 执行（按 D2，风险最高）
- [x] 路线 B（优先）：只生成合并方案/脚本 + 镜像关卡副本，不就地删 Actor。
- [x] 路线 A：`opt_collapse_hism(mode=plan|collapse|restore, manifest, min_count, allow_blockers, ...)`
  （实际命名，非原计划的 `opt_level_merge_hism`）——同网格散装 `StaticMeshActor` 折叠进
  `HierarchicalInstancedStaticMeshComponent`，per-instance transform 原样搬入实例，销毁源 Actor。
  5.4.4 真机钉死的口径：裸 Actor + `new_object` 建的 HISM 根组件（`EditorLevelLibrary` /
  `LevelEditorSubsystem` 放置器路线在 5.4 写不满实例）；实例计数只信 `get_instance_count`，
  写不满或变换读不到 → 整组诚实退回、不销毁源 Actor；材质覆盖/混入蓝图放置器默认整组拒收。
- [x] 安全网：默认出 plan（列将被删源 Actor + 将生成的 HISM）；执行双钥；强提示「先提交 VCS/另存关卡副本，UE 无事务级回滚」。

## 测试随功能走（强制）
- [x] L0 离线：每个新 domain 函数加无引擎降级用例（结构键常驻）；每个 `opt_`/`fix_` 加 dry-run/双钥/`no_engine`/跳过路径用例；新规则加纯逻辑 + `total/truncated/cap` 报告纪律断言。
- [x] L2 夹具（`scripts/calib_fixture/` + 已登记 `docs/TESTING.md` 真值表与覆盖矩阵）已知真值靶：
  5.4.4 已实跑 **50/50 全 PASS**（2026-10-10，含 v1.5 新增 28 项）：
  - 一张 8K/16K 未虚拟大图（VT+钳尺寸，校验前后 `est_runtime`/`max_size`）；
  - 一张 Cube（VT 应 `skipped`）；
  - 一个「透明材质上 Nanite」网格（`skip_translucent` 拦截 / `fix_` 关闭）；
  - 一组 ≥5 重复同 mesh Actor（合批机会真值）；
  - 一张 `TC_HDR` 立方图（高位深归 `high_bpp`、运行时按实测不二次放大）；
  - 一个挂 `MaterialInstanceConstant` 的网格（材质槽解包基类材质读 `used_with_nanite`）；
  - TC_DEFAULT + TC_HDR 一起强改压缩（已是目标格式必须 no_op，默认闸门仍挡高位深）。
  - `matrix.json` 记录 5.3/5.4+ 三处 API（VT / Nanite / HISM）的 `tried` 命中 → 自动出跨版本兼容矩阵。
- [x] L3 真实工程抽检（2026-10-10，5.4.4 测试工程：`/Game` 9238 包 / 14.38GB，新导入美术）：
  `get_perf_report(scope=/Game, sample_size=160)` → 381 findings（error 139 / warn 242）、
  池需求上界 4341MB（140 张实测，need_clamp 90 / need_vt 104、恒常驻 345MB）、
  `nanite_translucent_conflict` 0 误报；工程 ini 无任何 `*PoolSize` → 溢出判定诚实降级 `unknown`。
  `scan_broken_references` 全量分页扫 9244 包（4 秒）→ 18 条悬空硬引用（16 条属新美术：材质
  指向已删/改名的贴图与网格；另 2 条是夹具自造的 `T_CalibBroken`/`T_CalibStub`）；
  `fix_broken_references` dry-run 把它们全路由进 `unfixable_missing`（无重定向桩可自动修）。
  手感：`sample_size=80` 在万级资产工程会系统性欠采样（`high_bpp` 全靠大资产度量面命中）。
- 合并前：L0 全绿；触碰 unreal 真机路径的写入 → 至少一个引擎版本 L2 全 PASS。
  **当前状态：L0 444 用例全绿（443 passed / 1 skipped）；L2 5.4.4 与 5.3.2 各 59/59 全 PASS（含路线 A
  真折叠↔回滚往返，以及 #11 D（POOL_MODEL v4）落地后的同日复跑）→ 跨版本矩阵两列达标；仍欠 5.5/5.8/5.1（本机无引擎）。
  可合并，发版仍需用户明确授权。**

## 里程碑
- [x] PR-1（只读，低风险）：`scan_texture_streaming` + 3 纹理规则 + `scan_nanite_candidates`/`nanite_translucent_conflict` + `analyze_instancing`/`drawcall_batching`；产出进 `get_perf_report`。附 L0 用例 + L2 夹具靶。
- [x] PR-2：VT / MaxTextureSize / 压缩 批量写（双钥，含 Cube 与不支持格式跳过）。
- [x] PR-3：Nanite 写（`opt_mesh_set_nanite` + `fix_nanite_translucent`，主攻跨版本 API）。
- [x] PR-4：HISM 合批——只读（`analyze_instancing` + `drawcall_batching` 规则 + 路线 B 方案落盘）与
  执行（路线 A `opt_collapse_hism` 真折叠 + 回滚清单 + `--hism-roundtrip` 真机往返）均已完成。

## 发布纪律
- 攒够增量后**停下来问**；只有用户当前对话明确授权「发布 vX.Y.Z」才打 tag / 推 tag / 传 PyPI / 建 Release。版本仅在获授权真正发版时才 bump（`pyproject.toml` + `prism/__init__.py`）。




