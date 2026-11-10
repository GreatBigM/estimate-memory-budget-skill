# 公式与标定基线

> 适用范围：**Ingenic T32Pro（PRJ0xx）**，单核 MIPS / uclibc；4K-WiFi、双摄-1440p-WiFi 等均属该平台。T31/T33 等其它平台未标定。
>
> 定位：解释 `scripts/estimate_memory.py` 与 Excel 模板里的每条公式从哪来、系数怎么拟合、什么时候失效。
> 运行流程见 `SKILL.md`；坑见 `references/pitfalls.md`。

## 1. 两层账

| 层 | 主导变量 | 典型项 | 可见性 |
|---|---|---|---|
| rmem（媒体 carve-out） | 分辨率 | ISP MDNS、FrameSource VB、VENC pool、JPEG pool、SDK 预留块 | `/proc/meminfo` 不可见 |
| Linux 侧 | 码率 / 连接数 / 构建 | SDK 流缓存 ring、P2P 收发缓冲、音频 ring、内核/模块/CMA/进程/cache/zram | `/proc/meminfo` 可见 |

两本账不能相加成「Linux 可用」：rmem 是从总 DRAM 里 carve 出去的。

## 2. 公式（字节）

```
F      = W × H × 1.5                          # NV12 / YUV420SP 单帧
I      = bitrate(kbps) × 2.5 × 1000 / 8       # 已废弃：仅历史标定使用；码率不参与 VENC pool
P_venc = pool_a × F + pool_b                  # 编码器 pool：只与分辨率/编码流数有关，与码率无关
V_ivdc = ivdc_mem_line × W × 1.5              # IVDC 直通：只分配行缓存
V_vb   = nrVBs × F                            # 普通帧源：VB 数 × 整帧
J      = k_jpeg × F + jpeg_overhead           # JPEG 抓拍 pool
M_s    = k_isp × max(F of sensor s)           # ISP MDNS：每个 sensor 一份
R      = bitrate(kbps) × 128 × buffer_seconds # SDK 流缓存 ring（反汇编坐实）
B_p2p  = clients × (video_kb×1024×video_bufs
                    + audio_kb×1024×audio_bufs×2)
MTP    = fixed_kb×1024 + sessions × ((wnd_rcv+wnd_snd)×MTU + overhead_kb×1024)
P_h264 = h264_factor × (pool_a×F + pool_b)    # H264 无 RFC，默认 1.22
WebRTC = fixed_kb×1024 + sessions × per_session_kb×1024
```

**rmem 与码率无关**：`continuous_mem_info` 的 pool 由分辨率、编码流数、编码器配置决定，改码率不改变它。
码率只影响 Linux 侧的 SDK ring（`R = bitrate × 128 × 秒`）。早期的 `k_recon×F + k_stream×I` 是把
「分辨率」和「码率」一起拟合出来的码率代理式，两条标定点里两者同向变化，现已废弃。

`pool_a / pool_b` 由同产品两档实测反解（`pool = a×F + b`）：

| 产品族 | pool_a | pool_b (B) | 标定点 |
|---|---|---|---|
| 4K-4G / 4K 样机（5.15 + 同 SDK） | 0.799222 | 636,370 | 4K 10,579,968 / 480P 1,115,136 |
| 双摄-1440p-WiFi / 双摄-1440p-4G / H264+WebRTC 版 | 0.957336 | 95,958 | 2880×1620 6,795,776 / 480P 669,440 |

换分辨率/产品族必须重标；不同产品族的 a/b 差异说明编码器配置（buffer 数/SDK 版本）不同。

`R` 的 128 = `bitrate << 10 >> 3`，即 kbps → 字节/秒；来自 `libmike.a` 的 `ringbuffer_init`。

## 3. 历史系数（4K-WiFi 2026-06，28MB 场景；不是当前默认）

实测点（`rmem_optimization_analysis.md`，2026-06-23，28MB rmem / isp_memopt=5 调试场景）：

| 点 | pool 实测 B | 单帧 F | maxI I |
|---|---:|---:|---:|
| 4K H265 2000kbps | 14,312,448 | 12,441,600 | 625,000 |
| 832×480 H265 350kbps | 789,248 | 599,040 | 109,375 |

解 2×2：

```
D = Fm×Is − Fs×Im
k_recon  = (Pm×Is − Ps×Im) / D = 1.0869
k_stream = (Fm×Ps − Fs×Pm) / D = 1.2630
```

其余系数：

| 系数 | 值 | 来源 |
|---|---:|---|
| k_isp | 0.8388 | 10,436,608 / 12,441,600（4K MDNS） |
| k_jpeg | 1.0 | 607,360 − 8,320 = 599,040 = 1×F |
| jpeg_overhead | 8,320 B | 同上 |
| maxi_factor | 2.5 | 代码 `maxIPictureSize = maxBitRate × 5/2` |

## 3b. 历史默认：2026-11-09 4K-4G 实机重标（已被 pool_a/pool_b 取代，仅作对照）

实机 `/tmp/continuous_mem_info`（rmem 24,064K，isp_memopt=0x7，4K/1500kbps + 480P/350kbps）：

| 块 | 实测字节 |
|---|---:|
| ISP MDNS | 9,433,088 |
| FS0 IVDC VB | 1,152,000 |
| FS1 VB（nrVBs=2） | 1,198,080 |
| VENC 主 4K/1500 | 10,579,968 |
| VENC 次 480P/350 | 1,115,136 |
| JPEG 832×480 | 609,536 |
| SDK 小块合计 | 113,152 |
| 合计 used / cap | 24,200,960 / 24,641,536 |

双点拟合：

```
k_recon  = 0.5875      # 4K 与 480P 两点
k_stream = 6.9780
k_isp    = 9,433,088 / 12,441,600 = 0.7582
jpeg_overhead = 609,536 − 599,040 = 10,496
```

**两套系数不要混用**：2026-06 的 28MB 场景是 `k_recon=1.0869 / k_stream=1.263 / k_isp=0.8388`；
当前 4K-4G 构建是 `0.5875 / 6.978 / 0.7582`。差异来自 RFC/streamCnt/isp_memopt 与 SDK 版本，
所以每个项目/构建都要用实机数据重标。

app 侧验证：`smaps` anon 6,652 kB；模型动态（ring 2,534,400 + audio 115,200 + P2P 1,310,720 + MTP 769,280）
+ 基础 2,000,000 ≈ 6,667 kB，差 15 kB（0.2%）。MTP 0.73 MB 与总量一致，但未做 A/B 隔离。

## 4. 多摄（双 sensor 及以上）

- `frame_sources[]` 每项带 `sensor`（0/1/…）；**MDNS 按 sensor 分组**：
  `M_total = Σ_s k_isp × max(F of FS in sensor s)`。
- 同一 sensor 的多个 FS（主/次/抓拍）不能把帧相加算 MDNS；MDNS 只跟该 sensor 的最大帧走。
- VENC pool、FS VB、SDK ring 仍按通道独立累加，不按 sensor 合并。
- 双摄的 rmem = Σ_sensor(MDNS + FS VB + VENC) + SDK 预留；不要用单摄总量 ×2。
- H264 只在部分变体开启（如 双摄-1440p-WiFi H264+WebRTC 版）；国内版对应的 encoder 填 `enabled=false`。
- JPEG 若开机创建就计入；若按需建拆则稳态不计，只评估抓拍瞬时峰值。

## 4b. 双摄-1440p-WiFi 双摄（2026-11-09 实机重标）

配置：2 × (2880×1620 主 + 832×480 次 + 832×480 抓拍)，128MB DDR / rmem 43MB，
`isp_memopt=8`、`ivdc_mem_line=810`。

实测 `continuous_mem_info`：used **42,776,320 B = 40.80 MiB** / cap 45,088,768（94%）。
重标系数：`k_isp=0.7275`、`k_recon=0.8559`、`k_stream=1.4331`、SDK 预留 **4,657,792 B**。
五项：RMEM 40.80 / Linux 系统 42.71 / Linux 应用 18.27 / 算法 5.00 / 其他 4.88
= **111.65 MiB**（加 slab 前；加后 115.59）；最终预留 **16.35 MiB**（加 slab 前）；按 bootargs rmem 43MB 的运行时余量 **14.14 MiB**
（与实测 MemFree 14.14MB 一致）。

## 4d. 4K-4G（单摄 4G + MTP）

配置：3840×2160 主 + 832×480 次，64MB DDR / rmem 23.5MB，9s 缓存，2 个 P2P 客户端，
`isp_memopt=0x7`、`ivdc_mem_line=200`、CMA 6MB。

MTP（小米 KCP MTP SDK，不是 USB MTP）：
`fixed=128KB + 3×((128+32)×920 + 64KB) = 769,280 B`。
其中 `WND_RCV=128`、`WND_SND=32`、`MTU=920` 来自 `net_ikcp.c.o` 的 `.rodata`，
64KB/session 是会话结构/MBuffer/加密的估算余量；有实测填 `measured_bytes` 覆盖。

估算（4K-4G 实测重标系数）：rmem ≈ **23.08 MiB**（24,200,960 B，cap 23.5 MiB，余 0.42）；
Linux 动态 ≈ **4.51 MiB**（ring 2.42 + audio 0.11 + P2P 1.25 + **MTP 0.73**）；
固定项 **33.93 MiB**（含 miio_client 1.63，加 slab 前）；五项合计 **62.02 MiB**，最终预留 **1.98 MiB**，
按 bootargs rmem 23.5MB 的运行时余量 **1.56 MiB**（实测 MemFree 1.57MB）。

## 4c. 4K-WiFi 当前源码默认值

- 主：3840×2160 H265 20fps GOP60 2000kbps，FS0 nrVBs=1 + IVDC
- 次：832×480 H265 20fps GOP60 350kbps，FS1 nrVBs=2
- JPEG：832×480，按需建拆（稳态不计）
- 缓存 6s、P2P 2 客户端、视频块 1MB、音频块 32KB
- DRAM=64MB、ivdc_mem_line=200

估算（4K-4G/4K 样机 同平台系数 + pool_a/pool_b）：rmem **22.50 MiB**、Linux 动态 **4.04 MiB**、
固定项 **35.71 MiB**（含 slab 3.88、miio_client 1.63）；五项合计 **66.33 MiB**，账面预留 **−2.33 MiB**；
扣除 file_cache 7.90 后硬占用 **58.43 MiB**、硬余量 **5.57 MiB**。rmem 已由 4K 样机 4K 逐字节复验。

## 4e. 双摄-1440p-H264-WebRTC 海外版（H264 + WebRTC，预测）

- 双摄-1440p-H264-WebRTC = 校准后的 双摄-1440p-WiFi 基础 + **1 路 480P H264**（默认 S0 enc2）+ WebRTC SDK；IMP 350kbps、SDK ring 500kbps。
- H264 编码器 pool 开机常驻；SDK ring 按需。若产品需要双 sensor H264，再开 enc5，pool 再 +0.82 MiB。
- H264 无 RFC：pool 按 H265 模型 × `h264_factor=1.22` 估算。
- WebRTC：KVS WebRTC/HWYK，`fixed=128KB + 1×1024KB = 1.125 MiB`（KVS 源码常量估算，可用 measured_bytes 覆盖）。
- 1MB/会话推导：2 个 jitter buffer × 3000×2 桶 × ≈24B ≈ 0.55MB；rolling buffer 3s × 实际码率
  （视频 500kbps + 音频）≈ 0.22MB；SCTP/DTLS/ICE/PC ≈ 0.2MB。
- 当前模型（校准后的 双摄-1440p-WiFi 系数）：rmem **41.57 / 42 MiB**（余 0.43），五项合计 **114.05 MiB**，
  最终预留 **13.95 MiB**（加 slab 前）；H264+WebRTC 版 未实机重标。

## 4g. 双摄-1440p-4G 双摄 4G（2026-11-09 实测）

- `continuous_mem_info` 与 双摄-1440p-WiFi **逐字节相同**：used 42,776,320 B / cap 45,088,768（40.80 MiB）。
- idle（未拉流）：MemTotal 81,152K、MemFree 23,320K；c_mi_ipc anon 14,784K、miio_client anon 400K（RSS 1,664K）。
- 盲测（双摄-1440p-WiFi 系数 + VENC 800）：rmem 39.94（低 0.86MiB）、MIKE SDK 15.24（实测 14.44，偏差 0.80MiB）。
- 五项合计/最终预留必须拉流后复测；idle 值 103.03/24.97（加 slab 前）不能当峰值。

## 4f. 系统占用与硬余量（通用）

```
Linux 合计 = 动态 + 固定 + 4G supplicant/模块
rmem 需求  = MDNS + FS VB + VENC + JPEG + SDK 预留（自动计算，不做输入）
Linux 可见 = DRAM − rmem 需求
五项：
  1 RMEM       = MDNS + FS VB + VENC + JPEG + SDK 预留
  2 Linux系统  = kernel + modules + CMA(非算法) + slab(SUnreclaim) + file_cache + zram
  3 Linux应用  = MIKE SDK + miio_client + miio_ot
  4 算法       = NPU 池 + 算法模型
  5 其他       = other_procs + 网络(WiFi/4G) + 蓝牙
  五项合计     = 1+2+3+4+5
  账面预留     = DRAM − 五项合计
  硬占用       = 五项合计 − file_cache
  硬余量       = DRAM − 硬占用          # 判定看这一行

其中 MIKE SDK = 流 ring + 音频 ring + P2P + MTP + 海外语控 + 基础 anon；
CMA(非算法) = CMA 总量 −（NPU+算法，若含在 CMA）。
```

- `network.lte_supplicant_kb` 只在 WiFi+4G / 4G 时计入；若已包含在 `fixed.other_procs`，把其中一处置 0，避免双算。
- `npu_algo.npu_pool_kb` + `algo_model_kb`：`inside_cma=true` 时只是 CMA 的明细，不重复加；独立预留时才进系统占用。
- rmem 需求向上取整 1MB 作为 bootargs 建议值；硬余量按 rmem **需求** 扣（不是 bootargs 值）。

## 5. 适用边界

- `pool_a/pool_b` 只用两档 H.265 实测反解；H.264、其它 SoC/ISP 版本需重新标定。
- 双摄的 MDNS 是「每 sensor 一份」；双摄-1440p-WiFi 的 `isp_memopt=8` 与 4K-WiFi 的 `0x7` 不同，缩放关系未标定。
- ISP MDNS 对 `isp_memopt` 很敏感；换值后 k_isp 不可直接沿用。
- P2P 的「每客户端块数」是估计值（默认视频 1 块、音频双向各 1 块），误差可达 ±30%。
- 固定项是历史实测快照；file cache 属可回收占用，判定时从硬占用中扣除；zram 按压力上界计入硬账。
- 帧率 / GOP 不进内存公式，只影响 CPU 与码流波动。
- MTP 默认公式只覆盖 KCP 窗口/会话池/固定开销；实际 SDK 可能另有加密、MBuffer、线程栈开销，重要项目用 smaps 实测覆盖。
- H264 的 `h264_factor=1.22` 只是 480P/350kbps 的比值，换分辨率/码率要重算；H264 VENC pool 与 ring 都要单独看。
- WebRTC 的 1.5MB/会话是经验默认；KVS WebRTC 的 ICE/DTLS/SCTP/RTP 缓冲随会话和码流变化，必须实测。

## 5b. 精度验收（4K-4G，2026-11-09）

| 项 | 模型 | 实测 | 偏差 |
|---|---:|---:|---:|
| rmem 已用 | 24,201,521 B | 24,200,960 B | **0.0005 MiB** |
| MIKE SDK | 6,826,752 B ≈ 6,666KB | 6,652 kB | **0.014 MiB** |
| miio_client | 1,708,032 B | 1,668 kB | **0** |
| 最终预留 vs MemFree | 1.98 MiB | 1.57 MiB | 0.41 MiB |

双摄-1440p-WiFi（2026-11-09，rmem 43MB / MemTotal 81,488K / MemFree 14,480K）：

| 项 | 模型 | 实测 | 偏差 |
|---|---:|---:|---:|
| rmem 已用 | 42,776,320 B | 42,776,320 B | **0** |
| MIKE SDK | 16,978 KB | 16,972 KB | **0.006 MiB** |
| miio_client | 1,732 KB | 1,732 KB | **0** |
| 五项合计 | 111.65 MiB | 111.66 MiB | **0.01 MiB** |
| 最终预留 | 16.35 MiB | 16.35 MiB | **0** |

结论：验收看**五项合计 / 最终预留**（加 slab 前口径），同一项目标定后 ≤1MB；rmem/MIKE SDK 是中间项。加 slab 后以硬余量为判定。

⚠ 2026-11-09 起固定项新增 `slab` 行（默认 4,071,424B = 4K 样机 实测 SUnreclaim 3,976KB）。上表为加 slab **前**口径；
加后 双摄-1440p-WiFi 五项 115.59MiB，比实测多 3.93MiB——需 A/B 确认 slab 是否与旧拟合基线双算，再决定该产品填 3,976K 还是 0。

## 5c. 数据出处分级（2026-11-09 复盘）

| 等级 | 含义 | 涉及项 |
|---|---|---|
| A 实测 | 设备上直接量到，有快照/日志 | rmem 各块（`continuous_mem_info`）、MIKE anon / miio RSS（smaps）、NPU 池（insmod + 长测 need/avail）、zram 池（zsmalloc 4.88–5.28MB）、算法模型 1229KB（4K-WiFi 账户）、WiFi 用户态 wpa 992–1608KB + udhcpc 432–468KB、蓝牙 miio_bt 840–868KB（4K 样机） |
| B 公式坐实 | 公式来自反汇编/源码，并被实测验证 | SDK ring（码率×128×秒）、IVDC VB（`ivdc_mem_line`×W×1.5，200×3840×1.5=1,152,000 实测吻合）、FS VB（nrVBs×帧字节，4K-4G 逐字节吻合）、JPEG 固定开销 |
| C 拟合/反推 | 用总量倒推或凑到吻合，没有独立测量 | kernel 6.50MB / modules 1.78MB（`MemTotal−MemFree−Cached−Slab−Buffers` 反推）、app_base 2MB（拟合 anon，差 15KB）、双摄-1440p-WiFi fixed 全套 |
| D 估算 | 有依据但未实测，误差可达 ±30% | P2P 块数与发送块（1MB/32KB×2）、MTP 769,280B（未 A/B 隔离）、WebRTC 1.125MB（KVS 常量推导）、H264 `h264_factor=1.22`、4G supplicant 512KB |
| **无出处** | 找不到测量记录或推导链 | `file_cache 8,283,750B`、`other_procs 3,565,158B`（仅「进程 2.5M」粗估）；4K-WiFi 借用 4K-4G 的系数/SDK 预留/miio |

口径冲突（待闭环）：4K-WiFi `init.sh` 传 `cma_pool_size=0x380000`（3.5MB）、defconfig `CONFIG_CMA_SIZE_MBYTES=4`、模板 `npu_pool_kb=4096` 三者不一致，评估时按「池 3.5MB + CMA 4MB」分别标注。

**WiFi / 蓝牙机型的账**：

- 内核侧**已覆盖**：`fixed.modules` = `/proc/modules` 快照。4K 样机 实测 9 模块合计 **1,979,441B**（tx-isp 1,065,020 + aic8800_fdrv 464,512 + bluetooth 213,405 + audio 75,881 + aic_load_fw 63,180 + tnpu 40,934 + aic_btusb 31,665 + sensor 15,522 + env_flash 9,322）；WiFi 关/蓝牙关时 1,734,371B。
- WiFi 用户态**已实测**（4K 样机 2026-11-09）：`wpa_supplicant` RSS 992KB（长连）～1,608KB（新起），`udhcpc` 432～468KB；最大场景按 **2,076KB** 写入 `network.wifi_supplicant_kb`。
- 蓝牙用户态**已实测**：`miio_bt` RSS 840～868KB，按 **868KB** 写入 `features.bluetooth`；没有独立 `bluetoothd`。
- 同一快照的非网络常驻进程：linuxrc 812 + telnetd 228 + sh 516 + adbd 436 + logcat 392×2 + kernel_logd 388 = **3,164KB**；含 wpa/udhcpc/miio_bt 后全部非 app 进程约 **6,308KB**（最大场景）。
- 模板 `other_procs` 是各产品自己的快照；若该快照已含 wpa/udhcpc/miio_bt，对应字段填 0，避免双算。
- CMA 要按产品实测：4K 样机 实测 `CmaTotal 6144KB / CmaFree 68–552KB`，不能套 4K-WiFi 的 4MB。

**拉流态（4K 样机，2026-11-09，P2P client 0 在线）**：

- `continuous_mem_info`：used **24,200,960B** / cap 24,641,536B（98%），分块 9,433,088（MDNS）+ 10,579,968（4K VENC）+ 1,198,080（FS1 VB）+ 1,152,000（FS0 IVDC）+ 609,536（JPEG）+ 其余 —— 与 4K-4G 4K/1500 实测**逐字节相同**，等于在同平台第二个产品上复验了 4K 单摄系数。
- c_mi_ipc：`VmRSS` 10,668–11,440KB、`VmSwap` 740–1,092KB、`Threads` 81、smaps `Anonymous` 8,288KB（其中线程栈约 4,084KB、heap 1,756KB）。
- smaps 出现 **1,028KB 匿名映射且 RSS 1,028KB**，与 P2P `video_kb=1024 × 1 块`吻合；音频 32KB 块太小未单独识别。
- **P2P 并发按单 client 线性叠加**（不共享缓冲）：每 client ≈ 1,028KB 视频 + 128KB 音频（音频为估）；2 clients ≈ 2.3MB、3 clients ≈ 3.5MB。`p2p.clients` 直接乘单 client 值即可，不需要再测多客户端。
- 系统：MemFree 1,668KB、Cached 7,396KB、AnonPages 8,612KB、Slab 5,120KB、zram 954,368B、load 8.47。

补采集清单（按优先级；一次上机可把 C/D 抬到 A）：
- **P0 验收级**：目标项目自己的 `continuous_mem_info`（现在是借同平台系数）。P2P 单客户端已实测，多客户端按线性叠加，无需补测。
- **P1 最大场景**：`storage_test` / SD 录像在跑时的进程与 slab；zram 压力态（空闲只有 576KB，不能代表 swap 上限）。
- **P2 可选功能**：MTP 开/关 A/B；H264/WebRTC（只对 H264+WebRTC 版 类机型）；JPEG 抓拍瞬时。
- 进程侧已补：wpa_supplicant / udhcpc / miio_bt 已实测（4K 样机）；仍缺 `start_user_process.sh` 常驻 shell。
所有采集在**最大场景**下做：WiFi 已关联、BT 已上电、双流全开、P2P 满客户端、SD 录像在跑。

## 5d. c_mi_ipc 应用侧：能从代码算的部分

`c_mi_ipc` 不是黑盒，下面这些都能从源码配置直接算；只有 libmike.a 内部行为才需要实测。

| 项 | 代码配置 | 位置 | 模型 |
|---|---|---|---|
| 流 ring | `buffer_max_seconds` = `MIKE_STREAM_BUFFER_MAX_SECONDS`（6）；码率来自 `g_mike_video_channel_cfgs` | `mike_source.c:243`、`mike_stream_config.h` | 已计：`bitrate×128×秒` |
| P2P | `max_clients` = `MIKE_P2P_MAX_SESSIONS`（=MAX_P2P_CLIENTS=2）；`max_video_send_size` = 1MB；`max_audio_send/recv_size` = 32KB | `mike_p2p.c:601-605`、`4k-wifi_config.h:9,26` | 已计：单 client 1,028KB 实测，线性叠加 |
| 日志缓冲 | `mi_log_param_t.max_size = 256*1024` | `mike_init.c:3099` | 含在 `app_base`（2MB）内，未单列，避免双算 |
| 音频 | OPUS 16kHz/16bit/100kbps，25fps | `4k-wifi_config.h:35-39` | 已计：音频 ring |
| MTP | 应用层无配置，由 SDK/云端驱动 | `libmike.a` | 公式（769,280B）或实测 |
| 线程栈 | 代码 71 处 `pthread_create`；SDK 栈默认 **8MB 虚拟**（smaps 实测 18/19 条），物理触底约 50KB/线程 | `mike_init.c:697-704` | 按常见配置 **64KB/线程** 计：基准线程栈含在 `app_base`；新增线程用 `features.slack.extra_threads`，脚本按 64KB/线程加 |
| AI 模型 | 模型文件 + 运行时张量 | `libtracker` / NPU 驱动 | 查 `algo-models.md` |

结论：**ring / P2P / 音频 / 日志缓冲可算，线程栈按常见配置算**（64KB/线程，虚拟 8MB 不驻留）。
`app_base` 是拟合残差（含基准线程栈、heap、日志缓冲等），不要把这些配置项再单独加一遍；
只有「新方案比基线多开线程」时，才用 `features.slack.extra_threads` × `thread_stack_kb` 加增量。

## 5e. 可算性盘点（4K-WiFi 最大场景 66.33MiB，含 slab）

| 类别 | 项 | MiB | 占比 |
|---|---|---|---|
| **公式可算** | MDNS 9.00 + FS VB 2.24 + VENC 11.15 + ring 1.72 + audio 0.07 + P2P 2.25 + NPU 4.00 + 算法 1.20 | **31.64** | **47.7%** |
| **查表/实测常量** | SDK 预留 0.11 + 模块 1.70 + **slab 3.88** + WiFi 2.03 + BT 0.85 + miio 1.63 + other_procs 3.40 + zram 5.00 | **18.59** | **28.0%** |
| **需取数确认** | kernel 6.20 + file_cache 7.90 + app_base 2.00 | **16.10** | **24.3%** |
| **合计** | | **66.33** | 100% |

三个「算不出来」项的性质与改进路径：

- **kernel 6.20MB**：由 `MemTotal−MemFree−Cached−Slab−Buffers` 反推的拟合值；可以从 `vmlinux` 的 text/data/bss 段直接算，但需按构建产物取数，且不含 slab/页表。
- **file_cache 7.90MB**：取决于工作负载，且可回收；预算上应按区间（4K 样机 实测 7.4–14.0MB）看趋势，不当硬上限。
- **app_base 2.00MB**：扣掉所有可算动态项后的残差，包含线程栈、heap、日志缓冲等；可进一步拆成「线程数×64KB + heap 实测」但会引入新的实测依赖。

### 进一步确认（2026-11-09，已做）

三项都能从「反推」升级为「可确认」，只差一次构建/实测取数：

| 项 | 确认式 | 实测/构建值 | 与拟合值对比 |
|---|---|---|---|
| kernel | `.text+.rodata+.data+.bss + PageTables + KernelStack` | `out/image_4k-wifi/vmlinux`：4,474,380B + 520K + 1,064K = **5.81MiB** | 拟合 6.20，差 0.39（对齐/percpu/vmalloc 驻留页） |
| app_base | `smaps Anonymous − (ring+P2P+MTP+audio)` | 4K-4G：6,652K − 4,618K = **1.99MiB** | 拟合 2.00，差 0.01 |
| file_cache | `Cached + Buffers − Shmem − SwapCached` | 4K 样机：**10.1–14.2MiB**（随负载/uptime 变） | 模板 7.9 是低端快照，按区间看趋势 |

结论升级：**没有真正的黑盒**——kernel 可由构建产物算，app_base 可由 smaps 残差算，file_cache 可由 meminfo 算。
所谓「算不出来」只是**需要每个构建/产品取一次数**，不是不可推导；同一构建内模型可复现，跨产品必须重采。
最终口径：公式 31.64（47.7%）+ 查表 18.59（28.0%，含 slab 3.88）+ 取数确认 16.10（24.3%）= 66.33。

**可回收 / 缓存类盘点**（硬判定时扣除或不计）：

| 项 | 典型值（4K 样机） | 处理 |
|---|---|---|
| page cache（file_cache） | 10.1–14.2MiB | 账面计入，**硬判定扣除** |
| Slab `SReclaimable`（dentry/inode 等） | 0.78–1.30MiB | 不计入；`fixed.slab` 只取 `SUnreclaim`（3.9MiB） |
| Buffers | 0.38–0.86MiB | 不计入 |
| file-backed text（squashfs 代码页） | RSS 中约 2MiB | 不计入 |
| zram | 常态 0.6–1.0MiB / 压力 5MiB | 按压力上界计入硬账（保守） |
| `SUnreclaim` slab | 3.9MiB | 计入硬账 |

因此 **4K-WiFi 账面 66.33 > 64 不代表装不下**：扣除 file_cache 7.90 后硬占用 **58.43**，硬余量 **5.57MiB**。
判定以硬余量为准；账面值用来看趋势和比较方案。

**注意 5e 是按「推导方式」分类，不是账归属**。按五项账：

- `app_base` 属于 **Linux 应用（第 3 项）**，是 MIKE SDK 的基础 anon；Linux 应用 7.67 = ring 1.72 + audio 0.07 + P2P 2.25 + **app_base 2.00** + miio 1.63。
- `kernel`、`file_cache` 属于 **Linux 系统 / OS 层（第 2 项）**。
- 其余项按来源归 RMEM / 算法 / 其他，不因「可算性」改变归属。

## 5f. 待确认清单（2026-11-09）

| 优先 | 项 | 现状 | 影响 | 确认方法 |
|---|---|---|---|---|
| **已闭环** | Slab | 固定项新增 `slab` 行，默认 4,071,424B（4K 样机 SUnreclaim 3,976K）；计入 OS 层 | 经验值，理论更接近事实；旧拟合基线可能已吸收，按产品 A/B 后填 0 或实测值 |
| **P0** | 4K-4G 最大场景 −0.89MiB | 加入 WiFi 2,076 + BT 868 后超 64MB；但基线可能已含这些 | 负余量是否成立 | 关 WiFi/BT 各采一次 meminfo + ps，看 fixed 基线是否已含 |
| **P0** | file_cache 口径 | 模板 7.9MiB（4K-WiFi 早期快照）；4K 样机 实测 10.1–14.2 | 硬余量 ±6MB | 最大场景下 `Cached+Buffers−Shmem−SwapCached`，按上界取值 |
| **P0** | 4K-WiFi 自身 rmem | 借 4K-4G/4K 样机 系数；4K pool 已跨产品逐字节复验 | 量级已可信，验收仍差 | 目标机跑一次 `continuous_mem_info` |
| **P0** | CMA / NPU 池口径 | defconfig 4MB、init.sh `cma_pool_size=0x380000`（3.5MB）、4K 样机 实测 `CmaTotal 6144K` | 算法项归属、告警 | 目标机 `grep Cma /proc/meminfo` + 核对 init.sh |
| **P1** | SD / storage_test | 未测（SD 已挂载但无录像作业） | 「其他」项 | 录像/回放时 ps + meminfo |
| **P1** | zram 压力态 | 空闲 576–954KB；模型默认 5MB | 压力下余量 | 压测时读 `zram0/mem_used_total` |
| **P1** | other_procs 组成 | 不同产品 3.4MB vs 6.3MB（含 WiFi/BT） | 「其他」项 | 固定「非网络基线」定义，逐进程 smaps |
| **P1** | start_user_process shell | 未测 | 「其他」项 | ps 归因 |
| **P1** | 4G supplicant 512KB | 未实测 | 4G 机型「其他」 | 4G 机型 ps |
| **P2** | MTP 未 A/B 隔离 | 769,280B 公式；总量吻合但未开关对比 | 应用项 | MTP 开/关各抓一次 smaps |
| **P2** | H264 / WebRTC | 未实测（仅 H264+WebRTC 版 机型） | 应用项 | H264+WebRTC 版 拉流 smaps |
| **P2** | P2P 音频块 | 32KB×2 未单独识别 | 应用项小项 | 高分辨率 smaps 差值 |
| **P2** | 双摄-1440p-4G 峰值 | 未拉流 | 验收 | 拉流后 continuous_mem_info |
| **P2** | Excel 标定页 | 仍展示已废弃的 `k_recon/k_stream` 双点拟合 | 文档一致性 | 改为 pool_a/pool_b 拟合或标注废弃 |

## 6. 重标定

1. 采集两个编码通道的 pool 实测值（`/tmp/continuous_mem_info` 或 `impdbg --enc_info`）。
2. 用上面的 `D` 公式重算 `k_recon` / `k_stream`。
3. `k_isp = 实测 MDNS / 最大帧字节`。
4. 写回 spec 的 `coefficients`，重跑脚本；用『速查表』附表实测点对照『详细表』对应项，误差应 < 10%。
5. 双摄分别核对两个 sensor 的 MDNS 与主编码 pool；两个 sensor 分辨率不同时 `k_isp` 要各算一次。
