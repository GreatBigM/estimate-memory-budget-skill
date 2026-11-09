# CHANGELOG

## 0.12.0 - 2026-11-09

- 「NPU + 算法」从功能开关里拆成独立一步（池 / 算法模型多选 / 是否含 CMA）。
- 问卷按两本账重组：第 1 块 RMEM（逐路 sensor）；第 2 块 Linux 分 2a OS 层（含驱动）与 2b 应用层（MIKE/MIIO/语控/MTP/网络）；NAS/SD 归应用层功能开关。
- 报告口径与问卷对齐：块 1 RMEM / 块 2 OS 层 + 应用层 / 块 3 算法。
- `references/formulas.md` 新增「数据出处分级」：A 实测 / B 公式坐实 / C 拟合反推 / D 估算 / 无出处，并列出补采集清单。
- WiFi 机型补账：新增 `network.wifi_supplicant_kb`（计入「其他」，填 0 触发警告）；内核侧 WiFi/BT ko 已含在 `fixed.modules`，用户态 wpa_supplicant 此前未单列。
- 2026-11-09 <参考机> 实测回填：wpa_supplicant RSS 992–1,608KB + udhcpc 432–468KB（最大场景取 2,076KB）；蓝牙 `miio_bt` 840–868KB；9 模块合计 1,979,441B；CmaTotal 6144KB；非 app 进程合计约 6,308KB。
- 2026-11-09 <参考机> 拉流实测：rmem 24,200,960B 与 <项目B> 4K/1500 逐字节一致（跨产品复验）；c_mi_ipc RSS 10.7–11.4MB、anon 8,288KB、Threads 81；1,028KB 匿名映射证实 P2P 视频块 1MB×1。
- **rmem 与码率解耦**：VENC pool 改为 `P = pool_a×F + pool_b`（<项目B>/<参考机> 0.799222/636,370；<项目C>/<项目D>/B01 0.957336/95,958），`k_recon×F + k_stream×I` 仅作旧 spec 回退；码率只影响 SDK ring。<项目C> 五项仍 111.70 vs 实测 111.66。
- P2P 并发口径：按单 client 线性叠加（实测 1,028KB 视频 + 128KB 音频估），`clients` 直接乘，不再要求多客户端 smaps。
- 文档：`formulas.md` 5d「c_mi_ipc 应用侧可算项」——ring/P2P/音频/日志缓冲（256KB）有代码配置。
- 文档：`formulas.md` 5e「可算性盘点」——公式 31.64MiB（50.7%）/ 查表常量 14.71MiB（23.6%）/ 取数确认 16.10MiB（25.8%）；kernel 已用 vmlinux 段算到 5.81MiB（拟合 6.20），app_base 用 smaps 残差确认 1.99MiB，file_cache 用 meminfo 确认。
- 全量一致性 review：SKILL / formulas / Excel 说明统一为「账面 + 硬占用」、slab 行、pool_a/pool_b、WiFi/BT 项；修正 H264 公式、标定步骤、精度口径；5 个模板 Excel 校验 0 drift。
- 判定改为**账面 / 硬占用两本**：硬占用 = 五项合计 − file_cache；判 OK/超看硬余量（<项目A> 账面 66.33 / 硬 58.43 / 硬余 5.57）。可回收项（SReclaimable、Buffers、file-backed text）不进账，zram 按压力上界保守计入。
- 固定项新增 **slab 行**：默认 4,071,424B（<参考机> 实测 SUnreclaim 3,976KB），计入 OS 层；理论更接近事实，旧拟合基线可能已吸收，按产品 A/B 后填 0 或实测值。含 slab 后 <项目A> 66.33MiB / 余 −2.33，<项目C> 115.59MiB（比实测多 3.93，待复核）。
- 文档：`formulas.md` 新增 5f「待确认清单」——P0 五项（Slab 未单列、<项目B> 负余量、file_cache 口径、<项目A> 自身 rmem、CMA/NPU 口径），P1/P2 若干；Excel 标定页标注旧双点拟合已废弃。
- 线程栈按常见配置 **64KB/线程** 计（SDK 默认 8MB 虚拟不驻留，实测触底 ~50KB）；基准含在 `app_base`，新增线程走 `features.slack.extra_threads`。
- 判定口径改为**最大场景**：常驻进程全开（wpa_supplicant/bluetoothd/adbd/apphilogcat/storage_test/start_user_process shell）+ 全流 + 满客户端 + 最大缓存 + JPEG 瞬时 + SD 录像；稳态只作参考（pitfalls 26）。
- 新增 `references/algo-models.md` 算法模型登记表：查表法取数，选中带入 `algo_model_kb`；用户新模型可追加登记。
- 首条登记：`algo-normal-power-pv` **【常电】人/车算法** 1229KB（<项目A> 实测 1.0MB + 0.2MB）；<项目C>/<项目D> 为同款算法但已含在 NPU 池内，`algo_model_kb` 保持 0 防双算；算法名约定 `【常电】/【低功耗】/【海外】` 前缀。
- pitfalls 新增 25（算法只查表、flash 大小 ≠ 工作内存、NPU 池不可缩）。

## 0.11.0 - 2026-11-09

- 操作 2 重写为**向导式**：平台 → 逐路 sensor（主/次/抓拍/继续添加）→ 语控/MTP/网络/NPU → 存储与输出 → 复述确认。
- 新增「建议码率表」（4K/2880/1440/1080/720/次码流），非表内分辨率按面积插值；用户只给分辨率时套建议值并一行复述。
- DRAM/rmem 明确为输出：未知时 64MB/128MB 各跑一档并列余量。
- pitfalls 新增 21（DRAM 不当输入）、22（规格问到能算就停）、23（存储介质不进内存公式）、24（向导顺序）。

## 0.10.0 - 2026-11-09

- 新增 `templates/spec-dual-4g.json`：双摄 4G，VENC 800/350，ring 1800/700，9s/2 clients，H264/WebRTC off。
- 平台判据改为 **<PRJxxx>**（`tx-isp-<PRJxxx>` + `sensors/*_<PRJxxx>.ko`），修正 <项目D>/<项目C> 被误判为非 T32Pro。
- 新增 `rmem_bootargs_mb` 与“按 bootargs rmem 的运行时余量”行/判定，区分 rmem 需求与实车 carve-out。
- 新增 `calibrated` 标记与“未标定项目”硬告警；<项目C>/<项目B> 标记 true。
- `linux_ok` 计入 4G；多摄 SDK 预留/4G-other_procs 双算告警；JPEG 按 type 统计且不计 ring。
- 参考层按 <项目C>/<项目B>/<项目A>/B01/<项目D> 实测刷新；<项目C> audio 150。
- Review 结论：可做盲测/量级评估；验收前仍需 <项目D> 拉流峰值实测。

## 0.9.4 - 2026-11-09

- 术语统一：用户侧统一叫 **MIKE SDK**，不再叫 “MIKE anon”；Excel 系统汇总/文档/精度表同步。

## 0.9.3 - 2026-11-09

- <项目C> 实机重标：k_isp=0.7275、k_recon=0.8559、k_stream=1.4331、SDK 预留 4,657,792B。
- fixed 按 MemTotal/MemFree/Cached/Slab/Buffers 校准；MIKE anon 16.57MiB、miio 1.69MiB。
- 五项合计 111.65 vs 实测 111.66MiB（0.01MB），最终预留 16.35MiB；self-test 增加 <项目C> 断言。
- B01 基于校准后的 <项目C> 预测：rmem 41.57/42，五项合计 114.05，最终预留 13.95。
- 精度验收口径改为“五项合计/最终预留 ≤1MB”（用户确认）。

## 0.9.2 - 2026-11-09

- 明确精度目标：同一项目实测标定后 rmem / MIKE anon / 最终预留偏差 ≤1MB。
- self-test 增加 <项目B> 实测验收：rmem <1MB、MIKE anon <1MB、miio_client <1MB。
- 参考层补精度验收表：rmem 0.0005MB、MIKE anon 0.014MB、miio 0MB、最终预留 vs MemFree 0.41MB。

## 0.9.1 - 2026-11-09

- Review 修复：`templates/spec-4k-wifi.json` 补进 SKILL.md 支持清单（原 lint FAIL）。
- 默认系数/SDK 预留更新为 <项目B> 2026-11 实测（0.7582/0.5875/6.978，113,152B）；旧 28MB 场景降为历史参考。
- 新增 NPU+算法 > CMA 的告警；self-test 增加五项合计断言。
- 系统汇总加“脚本快照”提示：改输入参数后需重跑脚本。

## 0.9.0 - 2026-11-09

- 系统汇总固定五项展开：RMEM / Linux 系统 / Linux 应用 / 算法 / 其他。
- 每项带子项；`五项合计` 与 `最终预留` 一页看完。
- NPU/算法含 CMA 时从 Linux 系统 CMA 扣除，避免与算法项双算。

## 0.8.0 - 2026-11-09

- Linux 内存按应用分组：MIKE（c_mi_ipc）、miio_client / miio_ot、其他进程、平台固定。
- 系统汇总以「应用合计 + 平台固定 = Linux 合计」展示，MIKE 下展开 ring/audio/P2P/MTP/海外语控/基础 anon。
- `fixed.miio_client` / `fixed.miio_ot` 新增；<项目B> 用实测 1.63MiB 填入。
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

- 修正 B01 口径：<项目C-B01> = <项目C> 基础 + **1 路 480P H264**（默认 S0 enc2）+ WebRTC；双 sensor H264 需手动开 enc5。
- 单路 H264 模型 rmem 42.86 / 42 MiB（超 0.86），Linux 动态 11.86 MiB。

## 0.5.0 - 2026-11-09

- 新增 <项目C-B01> 海外版：每 sensor 多一路 H264 + KVS WebRTC/HWYK。
- H264 编码器按 `h264_factor=1.22`（无 RFC）在 H265 模型上修正；VENC pool 常驻，ring 按需。
- 新增 `features.webrtc`：fixed 128KB + sessions×per_session 1024KB（KVS 源码常量推导）；支持 measured_bytes 覆盖。
- 新增 `templates/spec-dual-wifi-b01.json`（6 FS / 8 ENC，H264/WebRTC 开启）；Excel 输入表加 WebRTC 段。
- 当前模型 B01：rmem 43.78 / 42 MiB（超 1.78），Linux 动态 11.86 MiB；需 B01 实机重标。

## 0.4.0 - 2026-11-09

- ENC 表新增 `ring_bitrate`：VENC 码率与 SDK ring 码率分列（<项目B> 实测 1500/350 vs 1800/400）。
- <项目B> 用实机 `continuous_mem_info` 重标：k_isp=0.7582、k_recon=0.5875、k_stream=6.9780、SDK 预留 113,152B。
- 标定页新增 <项目B> 实测块与双点拟合；模型 rmem 与实测 24,200,960B 逐项吻合（误差 <0.01%）。
- app anon 6,652kB ≈ 模型动态 4,510kB + 基础 2,000kB，MTP 0.73MB 已含在内。

## 0.3.0 - 2026-11-09

- 新增 MTP SDK 统计（小米 KCP MTP，不是 USB MTP）：`features.mtp` 可配置 sessions/窗口/MTU/overhead，支持实测覆盖。
- 默认公式：128KB + 3×((128+32)×920 + 64KB) = 769,280 B；<项目B> 默认启用，<项目A>/<项目C> 默认关闭。
- 新增 `templates/spec-4k-4g.json`：<项目B> 单摄 4G（64MB / rmem 23.5MB / 9s / MTP）。
- Excel 输入表新增 MTP 段（B28~B35），Linux 动态区新增 MTP 行。

## 0.2.1 - 2026-11-09

- 明确平台为 **Ingenic T32Pro（<PRJxxx>）**：spec 增加 `platform` 字段，Excel 标题与摘要带平台名。
- 非 T32Pro 平台自动告警；`platform` 进 spec 模板（<项目A> / <项目C> 均为 T32Pro）。

## 0.2.0 - 2026-11-09

- 支持双摄/多摄：FS 表新增 `sensor` 列，ISP MDNS 按 sensor 分组独立计算。
- Excel 改为按 spec 动态生成，不再受固定模板行数限制（任意 FS / ENC 数）。
- 新增 `templates/spec-dual-wifi.json`：<项目C> 双摄（6 FS / 8 ENC，128MB / rmem 43MB / 8s / 3 clients）。
- `--self-test` 增加双摄 MDNS 分组断言；移除 `assets/estimator-template.xlsx` 模板依赖。

## 0.1.0 - 2026-11-09

- 初始版本：交互式收集规格 → 公式估算 rmem / Linux 内存 → 输出 Excel。
- 脚本 `scripts/estimate_memory.py`：`--spec` / `--out` / `--json` / `--quiet` / `--no-xlsx` / `--self-test`；
  自动调用 LibreOffice 重算并校验 Excel 与 Python 模型一致性。
- 模板 `assets/estimator-template.xlsx`：说明 / 输入参数 / 内存估算 / 标定校验 / 速查表。
- 参考层：公式与双点拟合（`references/formulas.md`）、口径陷阱（`references/pitfalls.md`）。
