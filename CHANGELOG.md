# CHANGELOG

## 0.16.6 - 2026-11-10

- pitfalls 新增 35：SigmaStar 上 `/proc/vmallocinfo` 求和（46.72MiB）≠ 内存占用（`VmallocUsed` 10.62MiB），
  差的 36MiB 是 `CamOsMemMap`/`iotable_init`/IPU 空间等 MMIO 映射。
- 明确 kernel 口径：`code + data + (VmallocUsed − modules) + KernelStack + PageTables`，
  且不可与君正的 vmlinux 段基线直接比较（同口径下 SSC303 code+data 为 5.29MiB）。

## 0.16.5 - 2026-11-10

- **Linux 系统基线实证**（SSC303 实机，formulas §7.5）：kernel 14.40MiB（code 4.74 + data 0.55 + vmalloc 净 7.09 + 栈 1.38 + 页表 0.64）、modules 3.53MiB（**35 个 .ko**）、slab 7.77、CMA 2.00，硬占用 27.71 —— 与模板 14.33/3.53/7.80/2.00 误差 ≤0.07MiB。
- 账面差 4.8MiB 全部来自 file cache（实测 23.14，运行时会波动），不影响硬余量判定。
- 采集清单补 `/proc/iomem` 的 kernel 段实证命令；注明两端 modules 数量不可互套（SigmaStar 35 vs 君正 11）。

## 0.16.4 - 2026-11-10

- 5M AOV 模板媒体侧改为**保守口径**：官方 dump 全量保留，不再扣除 demo 的额外编码上下文（1.73MiB）。
- 明确 **JPEG 常驻**（`ven_id3_*` 31KB + `jpe-alg0` + `ven_es_out_8_0` 100KB ≈ 0.13MiB）；此前把 `ven_es_out_8_0` 误归「demo 多开」，已修正。
- 媒体合计 17.41 → **19.23MiB**；五项 91.90 → **93.73**；128MiB 硬余 54.47 → **52.65**（96MiB 20.65，64MiB −11.35）。
- 如需回到「只建主+次两路编码」口径，扣 1.73MiB 即可，其余不变。

## 0.16.3 - 2026-11-10

- 5M AOV 模板：**AOV 与直播/录制共用同一 ring**，取消独立 AOV 录像通道（原 500kbps / 2fps / 60s，3.84MiB/摄）。
- 单摄 ring 6.75 → **3.08MiB**（主 2.20 + 次 0.51 + 音频 0.38）；应用 28.86 → **25.20**；五项 95.56 → **91.90**；128MiB 硬余 50.81 → **54.47**。
- 口径提示：共享 ring 的时间窗 = 容量 ÷ AOV 码率。主环 2.30MiB 在 500kbps 下覆盖约 **36s**；若产品要求 60s 事件前窗口，主环需回到 3.84MiB（ring 合计 4.55MiB），仍比双缓冲方案省 2.20MiB。

## 0.16.2 - 2026-11-10

- `templates/spec-sigmastar-5mp-aov.json`：4G 按**外挂 Cat.1** 计（模组侧跑 RTOS + 云栈，UART 唯一通道）⇒ `network.type=none`、`lte_supplicant_kb=0`，与 SigmaStar-256MB 样机同构。
- 五项 96.06 → **95.56**，硬占用 77.19，128MiB 硬余 **50.81**（96MiB 18.81，64MiB 仍不可行）。
- 消除「4G 已单列，确认 other_procs 是否双算」告警：`other_procs` 采自无 Linux 网络栈的样机，本就不含 supplicant。

## 0.16.1 - 2026-11-10

- **君正口径按官方文档订正**（`T32Pro 1.0.1.0`，`媒体功能开发参考` 2026-04 / `SDK 使用指南`）：
  - `V_vb` 改为 `depth × F`，并注明 no-copy depth（`IMP_FrameSource_SetFrameDepthCopyType`）会让有效深度扩展，
    配置的 nrVBs 只是下限，须用 `IMP_FrameSource_GetFrameDepth()` 取实际值。
  - 明确 `pool_b` 的旋钮：`IMP_Encoder_SetMaxStreamCnt()`（CreateChn 前调用，默认非 JPEG=NR_MAX_ENC_CHN_STREAM、JPEG=1）、
    `encAttr.bufSize=0` 时内部重算且过小会被放大。
  - 新增 OSD rmem 项说明：`IMP_ISP_Tuning_SetOsdPoolSize` / `IMP_OSD_SetPoolSize` 独立申请，
    现有 pool_a/pool_b 标定不含，调了计入 `sdk_reserved`。
  - 新增 bootargs 约束：`mem + rmem = DRAM`，改 rmem 必须等量反改 mem，给建议值时要一并给 mem。
- formulas.md 新增 §2b「君正 API 侧的三个修正项」；SKILL 操作 1 新增运行时取值一行、判定口径补 mem/rmem 约束。
- `spec-template.json` 备注补君正三则；pitfalls 新增 34。

## 0.16.0 - 2026-11-10

- **SigmaStar 公式化**（`references/formulas.md` §7）：用一份官方单目 5M AOV realtime dump 反解出分块公式与系数——`VENC recon 0.872×F`、`ES Ring pool 默认 W×H/2`（可 `u32BufSize` 覆盖，实测用 /4）、`DNR 0.511×F@level3`、`SYS 1.46`、`SCL 0.40×F_out+0.59`、realtime 块 0.17MiB/个；回验官方 dump 误差 0.6%。
- **绑定模式口径**：realtime 不落 DRAM 帧（VIF→ISP、ISP→SCL）；HW_RING = 1/4 帧；FRAME_BASE ≥3 帧且 `u32BufQueueDepth` 默认 4。heap 里没有 `vif_*` 不是漏采。
- **ring 池公式坐实**（SigmaStar-256MB 开机日志 `ringbuffer.c:608/666`）：`size = bitrate(kbps)×128×秒`、`nodes = fps×秒`；通道 = 每摄（主/次/第三）+ 1 路 AOV 录像（2fps/60s/500kbps）+ 1 路音频；双摄合计 19.21MiB，与 smaps 匿名块吻合。
- **应用侧拆分口径**：`固定 = smaps Anonymous 总量 − Σring`；纠正「基础 anon 不含 ring」的旧表注（那是重复计算口径）。
- **P2P 每客户端实测 +2.32MiB**（SigmaStar-256MB 拉流，含发送队列），高于君正侧块大小估算。
- 新增模板 `spec-sigmastar-5mp-aov.json`：单摄 5M AOV 4G，128MB / cap 24MiB / media 17.41MiB / app 28.86MiB / 五项 96.06 / 硬余 50.31。
- `algo-models.md` 新增 `algo-sigma-ipu-800x480`（1,632KB，MMA 堆内，勿再填 npu_algo）。
- pitfalls 新增 28~33：realtime 无 vif 块、默认 depth=4、ES 池默认 W×H/2、ring 公式与通道构成、smaps anon 含 ring、P2P 2.3MiB/客户端。

## 0.15.0 - 2026-11-10

- **双平台模板**：新增 SigmaStar 路径（`media.source=measured`），与君正 T32Pro 的 rmem 公式路径并存；`platform_family()` 判平台。
- 新增 `media` 规格块：`cap_bytes`（MMA 堆容量）+ `blocks[{name,group,bytes}]`；`group:"算法"` 的块自动从 RMEM 划到算法块，不双算。
- `Linux 可见 = DRAM − media.cap_bytes`（SigmaStar），不再用 used；`rmem.cap` / `rmem.usage` 进结果与摘要。
- 输入详细页在 SigmaStar 模式下把 FS/ENC 表换成「媒体内存块」表并列出 cap；详细表块 1 改为实测块行，块 4 列出 IPU 块。
- 网络类型新增 `none`（Linux 侧无网络栈，Cat.1 在对端 RTOS）；数据校验下拉同步。
- 新增告警：measured 但平台非 SigmaStar、blocks 为空/全 0、未填 cap。
- 新增模板 `spec-sigmastar.json`（骨架）与 `spec-sigmastar-256mb.json`（SigmaStar-256MB 样机 实测示例）；self-test 增加 SigmaStar 断言（RMEM 块合计、算法划出、可见按 cap）。
- SKILL 增加「平台两分支」对照表与「第 1 步 S：SigmaStar 媒体块」采集手册。

## 0.14.0 - 2026-11-10

- 详细表接入 Excel 三级大纲：0 级=块小计/五大块合计，1 级=块内成员/应用，2 级=成员开销；层级规则写入 SKILL 的「详细表大纲层级」。
- Linux 应用块改为「应用 → 应用开销」二级展开：c_mi_ipc / miio_client / miio_ot 为一级，c_mi_ipc 的 ring / 音频 / P2P / MTP / 语控 / 线程 / 基础 anon 为二级；去掉 miio 中间小计。
- kernel / modules 一级展开：输入详细与详细表列出子项（vmlinux 段 + 其他分配段 + 页表/栈/配平；11 个 .ko）；4K-WiFi 取自 `out/image_4k-wifi` 构建产物（modules 1,820,108B，替换原拟合 1,782,579B）。
- 详细表改为单表连续 + A 列标块（取消纵向合并，避免跨折叠行显示异常），按块分页，行高按文本长度自适应。
- 速查表附录分页：主表 / 实测标定点 / 经验值基线各占一页；首列加宽。
- 输入详细页重排：常用项（视频与连接、FS/ENC 表）在前，网络/功能开关/NPU 居中，固定基线在后，标定系数与常量集中最后一节。
- 说明页五大块改为逐项构成表：有公式的列公式，无公式的标「经验值 / 配置值 / 查表」；速查表附录新增「经验值 / 历史实测基线」。
- 报告去掉 bootargs 锚定：删除「按 bootargs rmem 的运行时余量」行、`rmem_bootargs_mb` 输入与相关警告，报告只给评估值。

## 0.13.0 - 2026-11-09

- Excel 从 6 表精简为 5 表：说明 / 输入详细 / 详细表 / 汇总 / 速查表（附录）。
- 说明页改为「五大块构成 + 核心公式 + 两本账判定口径」。
- 输入详细页补齐全部输入：网络/蓝牙、MTP/语控、NPU/算法、固定项基线、线程栈均可在表内改；备注列写默认值来源。
- 详细表按五大块展开，Linux 应用块把 c_mi_ipc 逐项（ring/音频/P2P/MTP/语控/线程/基础 anon）与 miio_client/ot 分开；汇总页每块只留一行总计。
- 标定校验页取消，实测标定点与采集命令并入速查表附录；双点拟合段删除（pool_a/pool_b 已固化在 spec）。
- 公式表改为全联动：改输入黄色格自动重算，校验覆盖五大块小计与合计（原来只校验 3 项）。
- 速查表主表 VENC 列补 pool_a/pool_b 分支，与模型一致。
- 判定改为按硬余量（与判定口径一致）。

## 0.12.0 - 2026-11-09

- 「NPU + 算法」从功能开关里拆成独立一步（池 / 算法模型多选 / 是否含 CMA）。
- 问卷按两本账重组：第 1 块 RMEM（逐路 sensor）；第 2 块 Linux 分 2a OS 层（含驱动）与 2b 应用层（MIKE/MIIO/语控/MTP/网络）；NAS/SD 归应用层功能开关。
- 报告口径与问卷对齐：块 1 RMEM / 块 2 OS 层 + 应用层 / 块 3 算法。
- `references/formulas.md` 新增「数据出处分级」：A 实测 / B 公式坐实 / C 拟合反推 / D 估算 / 无出处，并列出补采集清单。
- WiFi 机型补账：新增 `network.wifi_supplicant_kb`（计入「其他」，填 0 触发警告）；内核侧 WiFi/BT ko 已含在 `fixed.modules`，用户态 wpa_supplicant 此前未单列。
- 2026-11-09 4K 样机 实测回填：wpa_supplicant RSS 992–1,608KB + udhcpc 432–468KB（最大场景取 2,076KB）；蓝牙 `miio_bt` 840–868KB；9 模块合计 1,979,441B；CmaTotal 6144KB；非 app 进程合计约 6,308KB。
- 2026-11-09 4K 样机 拉流实测：rmem 24,200,960B 与 4K-4G 4K/1500 逐字节一致（跨产品复验）；c_mi_ipc RSS 10.7–11.4MB、anon 8,288KB、Threads 81；1,028KB 匿名映射证实 P2P 视频块 1MB×1。
- **rmem 与码率解耦**：VENC pool 改为 `P = pool_a×F + pool_b`（4K-4G/4K 样机 0.799222/636,370；双摄-1440p 系列/H264+WebRTC 版 0.957336/95,958），`k_recon×F + k_stream×I` 仅作旧 spec 回退；码率只影响 SDK ring。双摄-1440p-WiFi 五项仍 111.70 vs 实测 111.66。
- P2P 并发口径：按单 client 线性叠加（实测 1,028KB 视频 + 128KB 音频估），`clients` 直接乘，不再要求多客户端 smaps。
- 文档：`formulas.md` 5d「c_mi_ipc 应用侧可算项」——ring/P2P/音频/日志缓冲（256KB）有代码配置。
- 文档：`formulas.md` 5e「可算性盘点」——公式 31.64MiB（50.7%）/ 查表常量 14.71MiB（23.6%）/ 取数确认 16.10MiB（25.8%）；kernel 已用 vmlinux 段算到 5.81MiB（拟合 6.20），app_base 用 smaps 残差确认 1.99MiB，file_cache 用 meminfo 确认。
- 全量一致性 review：SKILL / formulas / Excel 说明统一为「账面 + 硬占用」、slab 行、pool_a/pool_b、WiFi/BT 项；修正 H264 公式、标定步骤、精度口径；5 个模板 Excel 校验 0 drift。
- 判定改为**账面 / 硬占用两本**：硬占用 = 五项合计 − file_cache；判 OK/超看硬余量（4K-WiFi 账面 66.33 / 硬 58.43 / 硬余 5.57）。可回收项（SReclaimable、Buffers、file-backed text）不进账，zram 按压力上界保守计入。
- 固定项新增 **slab 行**：默认 4,071,424B（4K 样机 实测 SUnreclaim 3,976KB），计入 OS 层；理论更接近事实，旧拟合基线可能已吸收，按产品 A/B 后填 0 或实测值。含 slab 后 4K-WiFi 66.33MiB / 余 −2.33，双摄-1440p-WiFi 115.59MiB（比实测多 3.93，待复核）。
- 文档：`formulas.md` 新增 5f「待确认清单」——P0 五项（Slab 未单列、4K-4G 负余量、file_cache 口径、4K-WiFi 自身 rmem、CMA/NPU 口径），P1/P2 若干；Excel 标定页标注旧双点拟合已废弃。
- 线程栈按常见配置 **64KB/线程** 计（SDK 默认 8MB 虚拟不驻留，实测触底 ~50KB）；基准含在 `app_base`，新增线程走 `features.slack.extra_threads`。
- 判定口径改为**最大场景**：常驻进程全开（wpa_supplicant/bluetoothd/adbd/apphilogcat/storage_test/start_user_process shell）+ 全流 + 满客户端 + 最大缓存 + JPEG 瞬时 + SD 录像；稳态只作参考（pitfalls 26）。
- 新增 `references/algo-models.md` 算法模型登记表：查表法取数，选中带入 `algo_model_kb`；用户新模型可追加登记。
- 首条登记：`algo-normal-power-pv` **【常电】人/车算法** 1229KB（4K-WiFi 实测 1.0MB + 0.2MB）；双摄-1440p-WiFi/双摄-1440p-4G 为同款算法但已含在 NPU 池内，`algo_model_kb` 保持 0 防双算；算法名约定 `【常电】/【低功耗】/【海外】` 前缀。
- pitfalls 新增 25（算法只查表、flash 大小 ≠ 工作内存、NPU 池不可缩）。

## 0.11.0 - 2026-11-09

- 操作 2 重写为**向导式**：平台 → 逐路 sensor（主/次/抓拍/继续添加）→ 语控/MTP/网络/NPU → 存储与输出 → 复述确认。
- 新增「建议码率表」（4K/2880/1440/1080/720/次码流），非表内分辨率按面积插值；用户只给分辨率时套建议值并一行复述。
- DRAM/rmem 明确为输出：未知时 64MB/128MB 各跑一档并列余量。
- pitfalls 新增 21（DRAM 不当输入）、22（规格问到能算就停）、23（存储介质不进内存公式）、24（向导顺序）。

## 0.10.0 - 2026-11-09

- 新增 `templates/spec-dual-1440p-4g.json`：双摄 4G，VENC 800/350，ring 1800/700，9s/2 clients，H264/WebRTC off。
- 平台判据改为 **PRJ0xx**（`tx-isp-PRJ0xx` + `sensors/*_PRJ0xx.ko`），修正 双摄-1440p-4G/双摄-1440p-WiFi 被误判为非 T32Pro。
- 新增 `rmem_bootargs_mb` 与“按 bootargs rmem 的运行时余量”行/判定，区分 rmem 需求与实车 carve-out。
- 新增 `calibrated` 标记与“未标定项目”硬告警；双摄-1440p-WiFi/4K-4G 标记 true。
- `linux_ok` 计入 4G；多摄 SDK 预留/4G-other_procs 双算告警；JPEG 按 type 统计且不计 ring。
- 参考层按 双摄-1440p-WiFi/4K-4G/4K-WiFi/H264+WebRTC 版/双摄-1440p-4G 实测刷新；双摄-1440p-WiFi audio 150。
- Review 结论：可做盲测/量级评估；验收前仍需 双摄-1440p-4G 拉流峰值实测。

## 0.9.4 - 2026-11-09

- 术语统一：用户侧统一叫 **MIKE SDK**，不再叫 “MIKE anon”；Excel 系统汇总/文档/精度表同步。

## 0.9.3 - 2026-11-09

- 双摄-1440p-WiFi 实机重标：k_isp=0.7275、k_recon=0.8559、k_stream=1.4331、SDK 预留 4,657,792B。
- fixed 按 MemTotal/MemFree/Cached/Slab/Buffers 校准；MIKE anon 16.57MiB、miio 1.69MiB。
- 五项合计 111.65 vs 实测 111.66MiB（0.01MB），最终预留 16.35MiB；self-test 增加 双摄-1440p-WiFi 断言。
- H264+WebRTC 版 基于校准后的 双摄-1440p-WiFi 预测：rmem 41.57/42，五项合计 114.05，最终预留 13.95。
- 精度验收口径改为“五项合计/最终预留 ≤1MB”（用户确认）。

## 0.9.2 - 2026-11-09

- 明确精度目标：同一项目实测标定后 rmem / MIKE anon / 最终预留偏差 ≤1MB。
- self-test 增加 4K-4G 实测验收：rmem <1MB、MIKE anon <1MB、miio_client <1MB。
- 参考层补精度验收表：rmem 0.0005MB、MIKE anon 0.014MB、miio 0MB、最终预留 vs MemFree 0.41MB。

## 0.9.1 - 2026-11-09

- Review 修复：`templates/spec-4k-wifi.json` 补进 SKILL.md 支持清单（原 lint FAIL）。
- 默认系数/SDK 预留更新为 4K-4G 2026-11 实测（0.7582/0.5875/6.978，113,152B）；旧 28MB 场景降为历史参考。
- 新增 NPU+算法 > CMA 的告警；self-test 增加五项合计断言。
- 系统汇总加“脚本快照”提示：改输入参数后需重跑脚本。

## 0.9.0 - 2026-11-09

- 系统汇总固定五项展开：RMEM / Linux 系统 / Linux 应用 / 算法 / 其他。
- 每项带子项；`五项合计` 与 `最终预留` 一页看完。
- NPU/算法含 CMA 时从 Linux 系统 CMA 扣除，避免与算法项双算。

## 0.8.0 - 2026-11-09

- Linux 内存按应用分组：MIKE（c_mi_ipc）、miio_client / miio_ot、其他进程、平台固定。
- 系统汇总以「应用合计 + 平台固定 = Linux 合计」展示，MIKE 下展开 ring/audio/P2P/MTP/海外语控/基础 anon。
- `fixed.miio_client` / `fixed.miio_ot` 新增；4K-4G 用实测 1.63MiB 填入。
- 避免 other_procs 与 miio/4G 双算。

## 0.7.0 - 2026-11-09

- **rmem 改为输出，不做输入**：`rmem 需求 = MDNS + FS VB + VENC + JPEG + SDK`；bootargs 值只作对照。
- `Linux 可见 = DRAM − rmem 需求`；系统汇总/最终预留按 rmem 需求计算。
- Excel 输入表 B22 改为只读“自动”；内存估算新增“建议 rmem carve-out（向上取整 1MB）”。
- 系统汇总新增 rmem 需求、最终预留；4G 网络计入 Linux 合计。

## 0.6.0 - 2026-11-09

- 通用化输入 schema：网络（WiFi/4G）、海外语控 SDK（统称 WebRTC/Alexa/Google）、NPU+算法预留。
- 新增“系统汇总”表：DRAM / rmem / Linux / NPU+算法 / 系统占用 / 最终预留。
- 海外语控不再单列 voice_assistant，统一用 `features.webrtc` 的 sessions/fixed/per_session 计。
- `network.lte_supplicant_kb` 计入 Linux 合计；`npu_algo.inside_cma=true` 时不重复加。

## 0.5.1 - 2026-11-09

- 修正 H264+WebRTC 版 口径：双摄-1440p-H264-WebRTC = 双摄-1440p-WiFi 基础 + **1 路 480P H264**（默认 S0 enc2）+ WebRTC；双 sensor H264 需手动开 enc5。
- 单路 H264 模型 rmem 42.86 / 42 MiB（超 0.86），Linux 动态 11.86 MiB。

## 0.5.0 - 2026-11-09

- 新增 双摄-1440p-H264-WebRTC 海外版：每 sensor 多一路 H264 + KVS WebRTC/HWYK。
- H264 编码器按 `h264_factor=1.22`（无 RFC）在 H265 模型上修正；VENC pool 常驻，ring 按需。
- 新增 `features.webrtc`：fixed 128KB + sessions×per_session 1024KB（KVS 源码常量推导）；支持 measured_bytes 覆盖。
- 新增 `templates/spec-dual-1440p-h264-webrtc.json`（6 FS / 8 ENC，H264/WebRTC 开启）；Excel 输入表加 WebRTC 段。
- 当前模型 H264+WebRTC 版：rmem 43.78 / 42 MiB（超 1.78），Linux 动态 11.86 MiB；需 H264+WebRTC 版 实机重标。

## 0.4.0 - 2026-11-09

- ENC 表新增 `ring_bitrate`：VENC 码率与 SDK ring 码率分列（4K-4G 实测 1500/350 vs 1800/400）。
- 4K-4G 用实机 `continuous_mem_info` 重标：k_isp=0.7582、k_recon=0.5875、k_stream=6.9780、SDK 预留 113,152B。
- 标定页新增 4K-4G 实测块与双点拟合；模型 rmem 与实测 24,200,960B 逐项吻合（误差 <0.01%）。
- app anon 6,652kB ≈ 模型动态 4,510kB + 基础 2,000kB，MTP 0.73MB 已含在内。

## 0.3.0 - 2026-11-09

- 新增 MTP SDK 统计（小米 KCP MTP，不是 USB MTP）：`features.mtp` 可配置 sessions/窗口/MTU/overhead，支持实测覆盖。
- 默认公式：128KB + 3×((128+32)×920 + 64KB) = 769,280 B；4K-4G 默认启用，4K-WiFi/双摄-1440p-WiFi 默认关闭。
- 新增 `templates/spec-4k-4g.json`：4K-4G 单摄 4G（64MB / rmem 23.5MB / 9s / MTP）。
- Excel 输入表新增 MTP 段（B28~B35），Linux 动态区新增 MTP 行。

## 0.2.1 - 2026-11-09

- 明确平台为 **Ingenic T32Pro（PRJ0xx）**：spec 增加 `platform` 字段，Excel 标题与摘要带平台名。
- 非 T32Pro 平台自动告警；`platform` 进 spec 模板（4K-WiFi / 双摄-1440p-WiFi 均为 T32Pro）。

## 0.2.0 - 2026-11-09

- 支持双摄/多摄：FS 表新增 `sensor` 列，ISP MDNS 按 sensor 分组独立计算。
- Excel 改为按 spec 动态生成，不再受固定模板行数限制（任意 FS / ENC 数）。
- 新增 `templates/spec-dual-1440p-wifi.json`：双摄-1440p-WiFi 双摄（6 FS / 8 ENC，128MB / rmem 43MB / 8s / 3 clients）。
- `--self-test` 增加双摄 MDNS 分组断言；移除 `assets/estimator-template.xlsx` 模板依赖。

## 0.1.0 - 2026-11-09

- 初始版本：交互式收集规格 → 公式估算 rmem / Linux 内存 → 输出 Excel。
- 脚本 `scripts/estimate_memory.py`：`--spec` / `--out` / `--json` / `--quiet` / `--no-xlsx` / `--self-test`；
  自动调用 LibreOffice 重算并校验 Excel 与 Python 模型一致性。
- 模板 `assets/estimator-template.xlsx`：说明 / 输入参数 / 内存估算 / 标定校验 / 速查表。
- 参考层：公式与双点拟合（`references/formulas.md`）、口径陷阱（`references/pitfalls.md`）。
