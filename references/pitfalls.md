# 坑与口径纪律

> 定位：估算前/交接前必读的反面边界。公式见 `references/formulas.md`，流程见 `SKILL.md`。

## 1. rmem 不在 `/proc/meminfo` 里

rmem 是从总 DRAM carve 出去的，`free` / `MemFree` 完全看不到媒体侧占用。
用 `impdbg --fs_info/--enc_info`、`/tmp/continuous_mem_info`（存在时）或 bootargs 的 rmem 值，不要用 MemFree 验证 rmem 估算。

## 2. 宏名/参数名里的数字 ≠ 常驻内存

历史假账：
- `MIKE_NAS_MEDIA_BUF_SIZE = 8MB` 只是传给流式 NAS API 的传输缓冲参数，懒分配，未驻留。
- WiFi 源码 `150 × 20KB = 3MB` 的 RX 预分配未编入 `.ko`。
- `flash_erase_len = 4194304` 是 flash 参数。

判据：只有 `.ko` 符号、系统级账本或 `smaps` 实测才能算常驻。

## 3. JPEG 不是稳态常驻

JPEG 抓拍已改按需建拆：空闲态 `enc_info` 只剩主码流。估算时 `enabled=false`；
若评估抓拍瞬时峰值，再临时加 0.6MB 左右。

## 4. 帧率 / GOP 不进内存公式

fps 与 GOP 影响 CPU、码流波动与 IDR 间隔，不改变 pool/ring 的分配尺寸。
相同分辨率与码率、不同帧率的估算相同，不是漏算。

## 5. 标定场景 ≠ 当前构建

默认系数来自 2026-06 的 28MB rmem / `isp_memopt=5` 场景；
当前生产可能是 23MB / `isp_memopt=0x7`。比例系数可复用，**绝对量必须实测重标**。
不要拿旧文档的 27.6MB 直接断言当前占用。

## 6. P2P 块数是估计值

`max_video_send_size` 是「发送视频的大小」；SDK 内部每客户端分配几块、音频是否双向各一块，
文档未给。默认视频 1 块、音频发送+接收各 1 块；误差可达 ±30%，重要项目用 smaps 实测。
2026-11-09 4K 样机 拉流实测补强：c_mi_ipc smaps 出现 **1,028KB 匿名映射、RSS 全驻留**，与
`video_kb=1024 × 1 块`吻合；音频 32KB 块太小未单独识别。**并发按单 client 线性叠加**（每个 client
独立分配，不共享缓冲）：每多一个 client 加 1,028KB 视频 + 128KB 音频，2 clients ≈ 2.3MB、3 clients ≈ 3.5MB，
不必再单独测多客户端。

## 7. JPEG 的 bufSize 不要套 1.5×

SDK 注释说 `bufSize` 通常不小于 `W×H×1.5`，但实测 832×480 JPEG 是 607,360 B ≈ 1×F + 8,320。
JPEG 公式用 `k_jpeg=1.0`，不要用 1.5。

## 8. type 必须精确匹配

脚本用 `type.upper() == "JPEG"` 分流；写成 `Jpeg`/`jpeg` 可以，但 `MJPEG`/`JPG` 会落到 H265 公式。

## 9. 设备采集纪律

- 写 SD 卡再拉回，不要写 `/tmp`（tmpfs 吃 RAM，污染测量）。
- 串口不要 `cat` 大文件，会冲掉 getty 提示符。
- 采样点：冷启动+60s、稳态、打流中、打流后 30s，各 n≥3。
- 真省内存的判据：`VmSwap↓` 且 `zram orig_data_size↓` 且 `MemFree↑`，三量同向。

## 10. 固定项不是硬上限

file cache 与 zram 可回收/可压缩；预算判定看趋势与余量，不要把「固定项合计」当不可动占用。
换内核/驱动/应用构建后，固定项要重新采集。

## 11. 双摄不是单摄账 × 2

- MDNS 是**每个 sensor 一份**：`Σ_s k_isp × max(F of sensor s)`。同 sensor 的主/次/抓拍 FS 不能把帧相加算 MDNS。
- VENC / FS VB / ring 按通道独立累加；先把每个 sensor 的 FS 组和 ENC 列全，再逐项求和。
- 双摄两个 sensor 分辨率不同时，MDNS 要各算一次，不能用较大的那个乘 2。

## 12. H264 与 JPEG 的变体开关

- 双摄-1440p-WiFi 只有海外版 H264+WebRTC 版 开 H264（`VIDEO_H264_IS_OPEN`）；国内版 enc2/enc5 填 `enabled=false`。
- 双摄-1440p-WiFi 双摄 `video_hal_init` 会开机创建 enc6/enc7 两路 JPEG；如果项目已改成按需建拆，才填 `enabled=false`。
- 海思/君正不同 SDK 的 JPEG 生命周期可能不同，先看 `jpeg_snap_setup` 是开机调用还是按需调用。

## 13. 双摄-1440p-WiFi 的内存参数别抄 4K-WiFi

- DDR 128MB；`CONFIG_双摄-1440p-WiFi` 分支 bootargs 是 `mem=85M rmem=43M`，H264+WebRTC 版 是 `mem=86M rmem=42M`。
- `init.sh`：`isp_memopt=8`、`direct_mode=2`、`ivdc_mem_line=810`、`ivdc_mem_stride=2880`。
- CMA 8MB（4K-WiFi 是 4MB）；固定项要按 双摄-1440p-WiFi 覆盖，不能沿用 4K-WiFi 的 30.2MB 基线。
- `isp_memopt` 与 4K-WiFi 的 `0x7` 不同，k_isp 的缩放关系未标定；41.94MiB 只是量级参考。

## 14. 平台判据用 PRJ0xx，不是目录名

- 判据：`drivers/<DRIVER_DIRECTORY_NAME>/isp/tx-isp-PRJ0xx/` + `sensors/*_PRJ0xx.ko`。目录名由 `toolchain/*_cfg.cmake` 指定；双摄-1440p-4G/双摄-1440p-WiFi/4K-4G 实际是 `t32_t33_3_10_14` / `t32_t33`，不是 `t32pro_5_15_170`。
- 本技能的 k_isp / k_recon / k_stream 只对 **T32Pro** 的 ISP/VENC 行为标定；换 T31/T33 或其它厂商平台必须重采。
- 同一 T32Pro 平台的不同项目（4K-WiFi/双摄-1440p-WiFi）可复用比例系数，但 `isp_memopt`、rmem 边界、CMA 大小要按项目覆盖。

## 15. MTP 是小米 KCP MTP，不是 USB MTP

- 符号在 `libmike.a`（`mtp_entry.c.o`、`mtp_*`），基于 KCP；不是 USB Media Transfer Protocol。
- 默认公式只含：固定 128KB + 会话 × ((128+32)×920 + 64KB/会话) = 3 会话 769,280 B。
- 实际开销还可能有加密、MBuffer、线程栈；有实测就填 `features.mtp.measured_bytes`，优先级高于公式。
- 4K-4G 支持 MTP；4K-WiFi/双摄-1440p-WiFi 默认不启用，别把 4K-4G 的 MTP 账带到它们身上。

## 16. VENC 码率 ≠ SDK ring 码率

4K-4G 实测：IMP `TARGET_BITRATE_MAIN=1500`、`SUB=350`，而 SDK `MIKE_STREAM_MAIN_BRATE=1800`、`SUB_BRATE=400`。
VENC pool 用前者，ring 用后者；模型已分 `bitrate` / `ring_bitrate` 两列。混用会把 ring 少算约 20%。

## 17. 双摄-1440p-H264-WebRTC 的 H264 与 WebRTC 是两笔额外账

- `VIDEO_H264_IS_OPEN=1` 只在 H264+WebRTC 版 海外版；国内版 enc2/enc5 不创建，别把 H264+WebRTC 版 的 H264 账带到国内版。
- H264 **没有 RFC**：不能直接套 H265 的 `k_recon=0.5875`；用 `h264_factor=1.22` 在 H265 模型上修正，换分辨率/码率要重算。
- H264+WebRTC 版 默认只多 **1 路** 480P H264（S0 enc2）；不要默认把双 sensor 的 enc5 也算上，需要时再开。H264 pool 常驻，SDK ring/WebRTC 按需。
- WebRTC 是 KVS WebRTC/HWYK（`dual-1440p-wifib01_target/libmike.a`），不是 USB/浏览器；默认 1.5MB/会话是经验值，必须用 smaps 或会话日志实测。
- H264+WebRTC 版 rmem=42MB（国内 43MB）；当前模型（单路 H264）估 42.86MB，超 0.86MB，必须上机 `continuous_mem_info` 复核后再定论。

## 18. 通用口径：NPU/算法、4G、海外语控

- NPU 池通常就是 CMA（TNPU `cma_pool_size`）；`inside_cma=true` 时不要再加一遍，否则系统占用虚高。
- 算法模型工作内存若在 CMA 内，同理只做明细；若独立预留（如放到 rmem/专用区），再计入系统占用。
- 4G supplicant/模块可能已含在 `fixed.other_procs`；二选一，不要双算。
- 海外语控 SDK 统称 WebRTC / Alexa / Google，统一用 `features.webrtc` 的 sessions/fixed/per_session；不要再单列 voice_assistant。
- `rmem 需求` 由管线计算，不做输入；`最终预留 = DRAM − rmem 需求 − Linux 合计 −（独立 NPU/算法）`。

## 19. 应用分组不要双算

- `fixed.other_procs` 不要再包含 `miio_client`/`miio_ot` 或 4G supplicant；它们各自单独计。
- MIKE SDK 的基础 anon 是 `fixed.app_base`；流 ring/P2P/MTP/海外语控是动态项，二者相加才是 MIKE SDK 合计。
- 系统汇总的「应用合计 + 平台固定 = Linux 合计」；平台固定只有 kernel/modules/CMA/cache/zram，不含应用堆。

## 20. 五项展开不要双算

- NPU/算法含在 CMA 时，`Linux 系统` 的 CMA 要减去 NPU+算法；算法作为第 4 项单独加，否则 CMA 与算法双算。
- `other_procs` 不要再包含 miio_client/miio_ot 或 4G supplicant；它们分别进第 3 项和第 5 项。
- `五项合计 = RMEM + Linux系统 + Linux应用 + 算法 + 其他`；`最终预留 = DRAM − 五项合计`。

## 21. 别把目标 DRAM 当输入

评估的目的是选型，DRAM 是结论。问「目标内存多少」等于让对方先拍脑袋，再让模型去凑。
正确顺序：音视频规格 → 估算 → 并列 64MB / 128MB 余量 → 选型。
同理 rmem 也不问，由 MDNS + FS VB + VENC + JPEG + SDK 算出后向上取整。

## 22. 规格只问到「能算」就停

一次只问一层：先分辨率，再（必要时）码率。用户只给「2560×1440」时，VENC/ring/nrVBs/IVDC
按同分辨率历史产品取默认并在聊天里一行复述，不要开第二轮「请补充八个字段」的问询。
选完还要手填一串数字，说明问题设计错了——用 `preview` 展示完整流行，让人点选而不是填空。

## 23. 存储介质不进内存公式

NOR / NAND、SD 卡、NAS 都可以问，但只作为备注与 fixed 基线口径（NAND 走 ubifs，file_cache 不同），
不加进 rmem / Linux 动态账。SD 录像和 NAS 8MB 宏都是写入/懒分配路径，`enabled` 后也不加字节，
除非有 `smaps` 实测。

## 24. 向导顺序：先规格后选型

固定顺序：平台 → **第 1 步 RMEM**（逐路 sensor）→ **第 2 步 Linux**（2a OS 层含驱动 → 2b 应用层：MIKE/MIIO/语控/MTP/NAS/4G）→ **第 3 步 NPU+算法** → 输出 → 复述确认。
不要先问 DRAM、不要一次把所有功能平铺成 4 个互不相关的选择；一轮只推进一层，问完就复述。
报告也按这个结构：第 1 步 = RMEM；第 2 步 = OS 层 + 应用层；第 3 步 = 算法；其他单列。

## 25. 算法模型只查表，不能套公式；flash 大小 ≠ 工作内存

算法模型的工作内存取决于模型结构、张量布局与运行时缓冲，无法从分辨率/码率推出。
一律查 `references/algo-models.md`，选中即带入；表里没有的，让用户给 KB 并追加登记行。
`algo.img` 的文件大小（4K-WiFi 约 860KB）只是 flash 存储占用，不能当工作内存用。
NPU 池是另一笔：4K-WiFi 3.5MB 被动态缓冲吃干，评估时不要因为“池没满”就缩。

## 26. 按最大场景算，不按稳态

评估用途是选型和余量，默认**所有常驻组件全开**：WiFi/BT 用户态（`wpa_supplicant`、`bluetoothd`）、
`adbd`、`apphilogcat`、`storage_test`、`start_user_process.sh` 的常驻 shell；双 sensor 全流、
P2P 满客户端、缓存秒数取最大、JPEG 抓拍瞬时、SD 录像/上行同跑。
稳态快照（尤其开机早期、WiFi 未关联时采的 `other_procs`）会漏计，只能作参考。
无出处或 D 级估算项一律按上界取值，并在摘要里注明是最大值。

## 27. 码率不改 rmem，只改 SDK ring

`continuous_mem_info` 里的 VENC pool 由分辨率、编码流数、编码器配置决定，**与码率无关**：
4K 样机 4K/1500 与 4K-4G 4K/1500 的 pool 逐字节相同，即换了产品、只要分辨率和流数一致就不变；
把目标码率从 1500 改到 2000，rmem 不变。
码率只影响 Linux 侧 `R = bitrate × 128 × 缓存秒数`（libmike.a 反汇编坐实）。
向导里仍问码率，是为了算 ring，不是为了算 rmem；不要再写「码率调高 → VENC pool 变大」。

## 28. SigmaStar realtime 配置下堆里没有 `vif_*` 块，不是漏采

`vif_zh.html` §1.1：realtime = VIF 与后级硬件直连，**不落 DRAM**；framemode 才先把数据传到 Dram。
所以「官方 dump 找不到 vif 块」是配置使然，别去翻 `mma_heap_name1` 或判定漏采。
反过来，看到 `vif_D0C0P0_priMma` 这类大块就说明该产品走的是 framemode，帧缓冲全在账上。

## 29. 输出端口默认深度是 4 帧，不是 1

`sys_zh.html` §2.4.13：`u32BufQueueDepth` 默认 **4**、`u32UserFrameDepth` 默认 0。
5M 一帧 6.67MiB，漏设一个 framemode 端口的深度就是 27MiB。realtime / ring 绑定时 `u32UserFrameDepth` 必须为 0。
评估时先确认代码里是否显式调过 `MI_SYS_SetChnOutputPortDepth`，没调就按 4 帧算。

## 30. VENC 码流池默认按 W×H/2，能改小一半

`venc_zh.html` §1.4.17：H.264/H.265/JPEG 默认 `Ring pool = W×H/2`，`u32BufSize=0` 时生效。
官方 5M dump 用的是 **W×H/4**（`ven_es_out_0_0` 1,167,360B），说明显式调过。
5M 双通道按默认比按 /4 多 2.2MiB；预算里要写明取值，不能默认按官方数值。

## 31. ring 池按「码率×秒」，节点数按「fps×秒」

开机日志坐实（`ringbuffer.c:608/666`）：
`size = bitrate(kbps) × 128 × 秒`、`nodes = fps × 秒`（144 = 12fps×12s）。
通道不是只有主/次：SigmaStar-256MB 每摄还有第三码流、**一路 AOV 录像**（2fps/60s/500kbps）和一路音频。
只按主流算 ring 会漏掉一半以上（AOV 录像 3.84MiB/摄 比主流还大）。

## 32. smaps 的 Anonymous 含 ring，别信「基础 anon 不含 ring」的表注

SigmaStar-256MB 的 `Anonymous 41.32MiB` 里就有 19.21MiB 的 ring 池（开机日志逐通道对得上）。
历史 Excel 把它标成「基础 anon，不含 ring」并另算 ring，属于重复计算口径；实测以 smaps 总量为准，
要拆就 `固定 = 总 anon − Σring`。

## 33. P2P 每客户端实测 ~2.3MiB，不是 1MiB

SigmaStar-256MB 拉流实测：anon 42,316KB → 44,636KB（+2.32MiB/客户端），增长在 `[heap]` 里（发送队列 + 重传缓冲）。
君正侧反汇编得出的「1,028KB 视频块 + 128KB 音频」是块本身，不含队列开销；SigmaStar 侧按 2.3MiB/客户端取。

## 34. 君正三个 API 会改账面，不能只看配置值

依据 `T32Pro 1.0.1.0`（`媒体功能开发参考` 2026-04 / `SDK 使用指南`）：

- **no-copy depth**：`IMP_FrameSource_SetFrameDepthCopyType(chn, 非0)` 启用后，有效深度会被扩展为该通道的缓冲数
  ⇒ 配置的 `nrVBs` 只是下限，必须用 `IMP_FrameSource_GetFrameDepth()` 读实际生效值（§2.4.11/§2.4.12）。
- **码流 buffer 数**：`IMP_Encoder_SetMaxStreamCnt()` 必须在 `IMP_Encoder_CreateChn()` **之前**调用；
  默认非 JPEG 为 `NR_MAX_ENC_CHN_STREAM` 个、JPEG 为 1 个；`encAttr.bufSize` 传 0 时内部重算，给太小会被自动放大。
  这些直接进 VENC pool（即 `pool_b`），换产品必须重标或显式设小（§5.5.18）。
- **OSD 池**：`IMP_ISP_Tuning_SetOsdPoolSize()` / `IMP_OSD_SetPoolSize()` 显式申请 rmem，是**独立项**；
  现有 `pool_a/pool_b` 标定不含它，调了就把字节数加进 `sdk_reserved`，否则整块漏账。
- **rmem 与 mem 必须等量互换**：`mem + rmem = 芯片真实内存`，只改 rmem 会改掉总内存（`SDK 使用指南` §2.1.4）。
  评估给 rmem 建议值时，要一并给出 mem。

## 35. SigmaStar：`/proc/vmallocinfo` 求和 ≠ 内存占用

实测（SSC303，2026-11-10）：`meminfo` 的 `VmallocUsed` = **10.62MiB**，而 `/proc/vmallocinfo` 全部求和 = **46.72MiB**。
差的 36MiB 是 **ioremap/MMIO 地址映射，不消耗物理内存**：

| 调用点 | 求和 | 性质 |
|---|---|---|
| `CamOsMemMap` | 25.0MiB | ISP/编码器的 MMIO 映射 |
| `iotable_init` | 6.0MiB | 静态 iotable 映射 |
| `MI_IPU_TASK_MappingChipTopIpuSpace` | 3.57MiB | IPU 空间映射 |
| `load_module` | 3.67MiB | 35 个 `.ko`（真正占内存，已单列 modules） |
| `kernel_clone` | 2.06MiB | 内核线程栈（占内存） |
| `__zstd_init` | 1.24MiB | zstd 解压工作区（占内存） |

**账只能用 `meminfo` 的 `VmallocUsed`**；`vmallocinfo` 只用来定位「谁在占 vmalloc 地址空间」，不能求和入账。
kernel 计算口径：`code + data + (VmallocUsed − modules) + KernelStack + PageTables`。
别拿君正的 kernel 基线（vmlinux 段，不含 vmalloc）直接对比——同口径下 SSC303 的 code+data 只有 5.29MiB。
