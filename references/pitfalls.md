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
2026-11-09 <参考机> 拉流实测补强：c_mi_ipc smaps 出现 **1,028KB 匿名映射、RSS 全驻留**，与
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

- <项目C> 只有海外版 B01 开 H264（`VIDEO_H264_IS_OPEN`）；国内版 enc2/enc5 填 `enabled=false`。
- <项目C> 双摄 `video_hal_init` 会开机创建 enc6/enc7 两路 JPEG；如果项目已改成按需建拆，才填 `enabled=false`。
- 海思/君正不同 SDK 的 JPEG 生命周期可能不同，先看 `jpeg_snap_setup` 是开机调用还是按需调用。

## 13. <项目C> 的内存参数别抄 <项目A>

- DDR 128MB；`CONFIG_<项目C>` 分支 bootargs 是 `mem=85M rmem=43M`，B01 是 `mem=86M rmem=42M`。
- `init.sh`：`isp_memopt=8`、`direct_mode=2`、`ivdc_mem_line=810`、`ivdc_mem_stride=2880`。
- CMA 8MB（<项目A> 是 4MB）；固定项要按 <项目C> 覆盖，不能沿用 <项目A> 的 30.2MB 基线。
- `isp_memopt` 与 <项目A> 的 `0x7` 不同，k_isp 的缩放关系未标定；41.94MiB 只是量级参考。

## 14. 平台判据用 <PRJxxx>，不是目录名

- 判据：`drivers/<DRIVER_DIRECTORY_NAME>/isp/tx-isp-<PRJxxx>/` + `sensors/*_<PRJxxx>.ko`。目录名由 `toolchain/*_cfg.cmake` 指定；<项目D>/<项目C>/<项目B> 实际是 `t32_t33_3_10_14` / `t32_t33`，不是 `t32pro_5_15_170`。
- 本技能的 k_isp / k_recon / k_stream 只对 **T32Pro** 的 ISP/VENC 行为标定；换 T31/T33 或其它厂商平台必须重采。
- 同一 T32Pro 平台的不同项目（<项目A>/<项目C>）可复用比例系数，但 `isp_memopt`、rmem 边界、CMA 大小要按项目覆盖。

## 15. MTP 是小米 KCP MTP，不是 USB MTP

- 符号在 `libmike.a`（`mtp_entry.c.o`、`mtp_*`），基于 KCP；不是 USB Media Transfer Protocol。
- 默认公式只含：固定 128KB + 会话 × ((128+32)×920 + 64KB/会话) = 3 会话 769,280 B。
- 实际开销还可能有加密、MBuffer、线程栈；有实测就填 `features.mtp.measured_bytes`，优先级高于公式。
- <项目B> 支持 MTP；<项目A>/<项目C> 默认不启用，别把 <项目B> 的 MTP 账带到它们身上。

## 16. VENC 码率 ≠ SDK ring 码率

<项目B> 实测：IMP `TARGET_BITRATE_MAIN=1500`、`SUB=350`，而 SDK `MIKE_STREAM_MAIN_BRATE=1800`、`SUB_BRATE=400`。
VENC pool 用前者，ring 用后者；模型已分 `bitrate` / `ring_bitrate` 两列。混用会把 ring 少算约 20%。

## 17. <项目C-B01> 的 H264 与 WebRTC 是两笔额外账

- `VIDEO_H264_IS_OPEN=1` 只在 B01 海外版；国内版 enc2/enc5 不创建，别把 B01 的 H264 账带到国内版。
- H264 **没有 RFC**：不能直接套 H265 的 `k_recon=0.5875`；用 `h264_factor=1.22` 在 H265 模型上修正，换分辨率/码率要重算。
- B01 默认只多 **1 路** 480P H264（S0 enc2）；不要默认把双 sensor 的 enc5 也算上，需要时再开。H264 pool 常驻，SDK ring/WebRTC 按需。
- WebRTC 是 KVS WebRTC/HWYK（`<项目C-B01>_target/libmike.a`），不是 USB/浏览器；默认 1.5MB/会话是经验值，必须用 smaps 或会话日志实测。
- B01 rmem=42MB（国内 43MB）；当前模型（单路 H264）估 42.86MB，超 0.86MB，必须上机 `continuous_mem_info` 复核后再定论。

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
`algo.img` 的文件大小（<项目A> 约 860KB）只是 flash 存储占用，不能当工作内存用。
NPU 池是另一笔：<项目A> 3.5MB 被动态缓冲吃干，评估时不要因为“池没满”就缩。

## 26. 按最大场景算，不按稳态

评估用途是选型和余量，默认**所有常驻组件全开**：WiFi/BT 用户态（`wpa_supplicant`、`bluetoothd`）、
`adbd`、`apphilogcat`、`storage_test`、`start_user_process.sh` 的常驻 shell；双 sensor 全流、
P2P 满客户端、缓存秒数取最大、JPEG 抓拍瞬时、SD 录像/上行同跑。
稳态快照（尤其开机早期、WiFi 未关联时采的 `other_procs`）会漏计，只能作参考。
无出处或 D 级估算项一律按上界取值，并在摘要里注明是最大值。

## 27. 码率不改 rmem，只改 SDK ring

`continuous_mem_info` 里的 VENC pool 由分辨率、编码流数、编码器配置决定，**与码率无关**：
<参考机> 4K/1500 与 <项目B> 4K/1500 的 pool 逐字节相同，即换了产品、只要分辨率和流数一致就不变；
把目标码率从 1500 改到 2000，rmem 不变。
码率只影响 Linux 侧 `R = bitrate × 128 × 缓存秒数`（libmike.a 反汇编坐实）。
向导里仍问码率，是为了算 ring，不是为了算 rmem；不要再写「码率调高 → VENC pool 变大」。
