---
name: estimate-memory-budget
description: Ingenic T32Pro 内存评估：收集分辨率/码率/流配置，支持单摄/双摄/多摄、MTP 与 H264/WebRTC，按标定公式估算 rmem/Linux 内存并输出 Excel。
version: 0.12.0
category: embedded-memory
metadata:
  agent:
    triggers: [内存评估, 内存预估, 内存占用, rmem 估算, 双摄内存, MTP 内存, H264 内存, WebRTC 内存, B01 内存, memory budget]
---

# 内存预算评估

## ⚡ 使用方式（AI 替你完成）

```
用户: 评估一个新机型：T32Pro，双摄，主码流 2560×1440
AI:  第0步 平台 → 第1块 RMEM（逐路 sensor：主/次/抓拍/继续）→ 第2块 Linux（2a OS 层 / 2b 应用层）
     → 第3块 NPU+算法 → 输出 → 复述确认 → 脚本按 64MB/128MB 各跑一档 → 聊天摘要 + Excel
```

**铁律：先规格后计算；目标 DRAM/rmem 是结论不是输入；按最大场景（进程/流/客户端全开）算，稳态只作参考；源码能读到的默认值不反问；系数不臆造；写文件前先给聊天摘要。**

## 触发

- 新项目 / 新规格（换分辨率、码率、sensor 数、缓存秒数）要评估内存可行性
- 用户问「4K 够不够」「双摄怎么算」「多一路流超不超」「rmem 要不要加大」
- 需要把现有产品的内存账整理成可交付表格
- 有实测数据要反推 / 更新估算系数

## 操作路由

| 输入 | 操作 | 输出 |
|---|---|---|
| 只有需求，没有完整规格 | 操作 1→2→3→4 | 聊天摘要 + Excel |
| 规格已完整，只要数字 | 用 `templates/spec-template.json` 或 `templates/spec-dual-wifi.json` 填好跑脚本 | 聊天表格（可不出文件） |
| 有实测 pool / MDNS 数据 | 操作 5 重标定 → 重跑 | 新系数 + 新 Excel |
| 只问某一档分辨率/码率 | 跑脚本或看 Excel 的「速查表」 | 聊天一行结论 |

## 通用输入规格（每次评估必问）

> 技能不绑定具体机型；项目源码只提供默认值，最终以用户确认的规格为准。

1. **基本信息**：项目/机型、平台。**DRAM 与 rmem 都是输出，不问目标值**——未知时按 64MB / 128MB 各跑一次，并列余量供选型。
2. **网络**：WiFi / 4G / WiFi+4G；4G supplicant/模块预留 KB。
3. **视频**：Sensor 数；每路 FS 的分辨率/编码/fps/nrVBs/IVDC；每路 ENC 的 VENC 码率/ring 码率/是否分配 ring；JPEG 是否常驻。
4. **海外语控 SDK**：是否支持（统称 WebRTC / Alexa / Google）；sessions、fixed KB、per-session KB、实测覆盖。
5. **SDK/功能**：MTP、P2P clients/发送块、缓存秒数、音频；MIKE / miio_client / miio_ot 基础 RSS。
6. **NPU + 算法**：NPU 池 KB、算法模型 KB、是否含在 CMA。
7. **固定基线**：kernel / modules / CMA / app / other / cache / zram。
8. **输出**：账面占用（rmem / Linux / NPU / 算法）+ **硬占用 / 硬余量**。

## 操作 1：锁定源码默认值

先读项目入口，能读到的不要问用户：

| 字段 | 源码位置 |
|---|---|
| 平台 | **<PRJxxx>**：`drivers/<DRIVER_DIRECTORY_NAME>/isp/tx-isp-<PRJxxx>/` + `sensors/*_<PRJxxx>.ko`；目录名由 `toolchain/*_cfg.cmake` 指定（<项目D> 是 `t32_t33_3_10_14` / `t32_t33`） |
| 流参数 / 缓存秒数 / P2P / sensor 数 | `applications/c_mi_ipc/project_configs/<project>_config.h`（`CAMERA_SENSOR_COUNT`、`MAX_P2P_CLIENTS`、`MIKE_STREAM_BUFFER_MAX_SECONDS`） |
| 分辨率 / nrVBs / IVDC / fps / GOP / 目标码率 | `media_hal/video_hal_config.h` 的 `PROJECT_<project>` 分支（`SENSOR_NUM`、`FRAMESOURCE_COUNT`、`ENCODER_COUNT`） |
| P2P 音频块 / 视频发送块 | `mike_adapter/mike_p2p.c`（音频 32KB，`max_video_send_size`） |
| ivdc_mem_line / isp_memopt / direct_mode | `third_party/rootfs/resource/system_bin/<project>/init.sh` |
| MTP 是否启用 | 项目产品确认；`libmike.a` 内含 `mtp_*`（小米 KCP MTP SDK）；<项目B> 支持，<项目D> 控制台有 MTP 日志但默认按 0 计 |
| H264 / WebRTC | `<项目C>_config.h` 的 `MIKE_STREAM_SUPPORT_HWYK`；`video_hal_config.h` 的 `VIDEO_H264_IS_OPEN`（仅 B01=1） |
| DRAM | uboot `PRJ.h` 的 bootargs；**rmem 由管线计算**，bootargs 值只作对照 |
| 固定项基线 | 项目 CLAUDE.md 或既有内存卡片；没有就用模板默认 |

项目不在 `/mnt/data/<project>/` 时，改为逐项问用户，并记录来源为「用户提供」。

整理成预填表：`平台 | 项目 | DRAM | rmem | Sensor | FS | 分辨率 | 编码 | 帧率 | 码率 | nrVBs | IVDC | 抓拍 | 缓存 | P2P`。
帧率/GOP 只用于说明，不参与内存公式。

## 双摄 / 多摄

- FS 表有 `Sensor` 列（0/1/…）；**ISP MDNS 按 sensor 分组**，每组取该组最大帧，不是全机一份。
- 每个 sensor 的 FS 组通常是：主（IVDC）+ 次 + 抓拍/H264；同一 sensor 的多个 FS 帧不能相加算 MDNS。
- ENC 表的 `Sensor` 只用于可读性；VENC pool / ring 仍按通道独立累加。
- H264：<项目C> 仅海外版 B01 开启（`VIDEO_H264_IS_OPEN`）；国内版 enc2/enc5 填 `enabled=false`。
- JPEG：<项目C> 双摄 `video_hal_init` 开机创建 enc6/enc7；若项目已改按需建拆，把 `enabled` 改 false。
- <项目C> 参考 `templates/spec-dual-wifi.json`：6 FS / 8 ENC（2 路 disabled H264 + 2 路 JPEG），128MB / rmem 43MB / 8s / 3 clients。
- <项目D> 参考 `templates/spec-dual-4g.json`：双摄 4G，VENC 800/350，ring 1800/700，9s/2 clients，H264/WebRTC off。

## MTP（可选）

- 小米 MTP SDK（`libmike.a` 内 `mtp_*`，KCP），**不是 USB MTP**；<项目B> 支持，<项目A>/<项目C> 默认不计。
- 公式：`MTP = fixed_kb + sessions × ((wnd_rcv+wnd_snd)×MTU + overhead_kb)`。
- SDK 常量：`WND_RCV=128`、`WND_SND=32`、`MTU=920`；默认 fixed=128KB、sessions=3、overhead=64KB/session ⇒ **769,280 B**。
- 有实测就填 `features.mtp.measured_bytes`，优先于公式；用 app smaps 或 MTP 分配日志。
- <项目B> 实测（2026-11-09）：rmem 24,200,960 B 与模型逐项吻合；app anon 6,652 kB ≈ 动态 4,510 kB + 基础 2,000 kB，MTP 0.73 MB 已含在内。
- <项目B> 参考 `templates/spec-4k-4g.json`（MTP enabled）；<项目A>/<项目C> 默认 disabled。

## 海外语控 SDK / H264（B01）

- <项目C-B01> = <项目C> 基础 + **1 路 480P H264**（默认 S0 enc2）+ WebRTC SDK；IMP 350kbps、SDK ring 500kbps。
- H264 编码器 pool 开机常驻；SDK ring 按需分配。若产品需要双 sensor H264，再开 enc5，rmem 再 +0.918 MiB。
- H264 无 RFC：`pool = h264_factor × (pool_a×F + pool_b)`，默认 `h264_factor=1.22`（按 480P 比值）。
- 海外语控 SDK 统称 WebRTC / Alexa / Google；B01 走 KVS WebRTC/HWYK（`<项目C-B01>_target/libmike.a`）。`webrtc = fixed + sessions×per_session`，默认 128KB + 1×1024KB = **1.125MB**（KVS 源码常量推导）。
- 参考 `templates/spec-dual-wifi-b01.json`：6 FS / 8 ENC，默认只开 enc2 H264，WebRTC 开启。
- 当前模型（校准后的 <项目C> 系数）估 rmem **41.57 / 42 MiB**（余 0.43），五项合计 **114.05 MiB**，最终预留 **13.95 MiB**（加 slab 前口径）；B01 未实机重标。

## 五项展开（系统汇总）

系统汇总固定按五项展示，每项带子项：

1. **RMEM**：ISP MDNS / FS VB / VENC / JPEG / SDK 预留。
2. **Linux 系统**：kernel + modules / CMA（非算法）/ **slab（仅 SUnreclaim）** / file cache / zram。
3. **Linux 应用**：MIKE SDK（ring/audio/P2P/MTP/海外语控/基础 anon）+ miio_client/ot。
4. **算法**：NPU 池 + 算法模型；含 CMA 时从 Linux 系统 CMA 扣除，避免双算。
5. **其他**：other_procs（adbd/storage/log）+ 网络（WiFi wpa/udhcpc、4G supplicant）+ 蓝牙 miio_bt。

`五项合计 = 1+2+3+4+5`；`账面预留 = DRAM − 五项合计`；`硬占用 = 五项合计 − file_cache`；`硬余量 = DRAM − 硬占用`（判定看硬余量）。

## 操作 2：交互式确认规格（向导式）

一次只推进一层，每层 AskUserQuestion ≤4 问；**不问 DRAM/rmem**（它们是输出）。顺序：

### 第 0 步 平台

| 选项 | 后续默认值来源 |
|---|---|
| T32Pro / <PRJxxx> | 已有标定系数，可给验收级 |
| T31 / T33 | 无系数，只给量级；提示需采集 `k_isp/pool_a/pool_b` |
| 其他 SoC | 同 T31 |

新项目没有源码默认值，后续默认值一律取「同平台 + 同分辨率历史产品」，并在摘要里注明来源。

### 第 1 步 RMEM（媒体账）：Sensor 循环（每路一轮，4 问）

1. **主码流分辨率**：典型档位，`preview` 展示完整行 `宽×高 / VENC / ring / nrVBs / IVDC`；
   非典型分辨率选 Other 填 `宽×高 [VENC/ring]`，只填分辨率就按建议值补。
2. **次码流**：832×480（VENC350/ring700/nrVBs2）/ 640×360（350/400）/ 无次码流 / Other。
3. **抓拍 JPEG 常驻**：否（按需建拆，稳态不计）/ 是（+0.58MB 量级）。
4. **继续**：添加下一路 / 同规格复制一路 / 结束（进第 2 步）。

3 路以上时，每轮只问「分辨率 + 继续」，其余按第 1 路克隆，除非用户明确要改。

rmem 侧驱动参数默认沿用同平台（<项目A>：`ivdc_mem_line=200 isp_memopt=0x7 direct_mode=1`），只在用户要求改时问。

**建议码率表**（同平台历史值锚定，非表内分辨率按像素面积插值）：

| 分辨率 | VENC | ring | nrVBs | IVDC |
|---|---|---|---|---|
| 3840×2160 | 2000 | 2000 | 1 | 开 |
| 2880×1620 | 1500 | 1800 | 1 | 开 |
| 2560×1440 | 1500 | 1800 | 1 | 开 |
| 1920×1080 | 1000 | 1200 | 1 | 开 |
| 1280×720 | 600 | 700 | 1 | 开 |
| 832×480（次） | 350 | 700 | 2 | 关 |
| 640×360（次） | 350 | 400 | 2 | 关 |

用户只给分辨率时，直接套建议值并一行复述「取 VENC …/ring …/nrVBs …/IVDC …，有出入告诉我」，**不再追问一轮**。

**码率只用于 SDK ring**：VENC pool 由分辨率/编码流数决定，与码率无关（pitfalls 27）；改码率只改 Linux 侧 `bitrate×128×秒`。

### 第 2 步 Linux 账：分 OS 层与应用层两块问

#### 2a OS 层（含驱动，2 问）

1. **Flash**：NOR / NAND。NAND 走 ubifs，`file_cache` 口径不同，记备注；不进 rmem 公式。
2. **固定基线**：沿用同平台实测（kernel / modules / CMA / **slab** / cache / zram）/ 自定义。
   新平台无基线时按量级给并标注「未实测」。`slab` 取 `SUnreclaim` 经验值（默认 4,071,424B），可回收部分不计。`modules` 基线含已加载 `.ko`（<项目A> `/proc/modules` 实测 1.70MB，含 tx-isp 0.86MB、aic8800_fdrv 0.38MB、bluetooth 0.20MB）；**用户态 wpa_supplicant 不在里面**，见 2b 网络。

#### 2b 应用层（4 问）

1. **海外语控 SDK**：不启用 / WebRTC / Alexa / Google。
   启用即追加 1 路 480P H264（pool 常驻、ring 按需）+ SDK `128KB + sessions×1024KB`（默认 1 session）。
2. **MTP**：不启用 / 默认 KCP（3 sessions ≈ 769,280 B）/ 实测字节。
3. **NAS / SD 卡**：不支持 / SD 卡 / SD + NAS。
   SD 录像与 NAS 的 8MB 传输缓冲都是写入/懒分配路径，不加字节，只记备注。
4. **网络**：WiFi / 4G / WiFi+4G。
   - WiFi 机型追加一问 `network.wifi_supplicant_kb`：`未实测按 1536KB 估` / `已知实测 KB` / `基线已含，填 0`（填 0 会触发漏计警告）。
     已知数据（<参考机> 2026-11-09 实测）：`wpa_supplicant` 992–1,608KB、`udhcpc` 432–468KB，最大场景取 **2,076KB**。
   - 4G supplicant 默认 512KB，可改。
   - **蓝牙**（AIC8800 BT）追加一问「启用/不启用」：启用时 `features.bluetooth.enabled=true`，用户态按 `miio_bt` **868KB**（<参考机> 实测）；
     内核 `bluetooth.ko` 213,405B + `aic_btusb` 31,665B 应在 `fixed.modules` 基线内。

缓存与 P2P 默认 6s / 2 clients（MIKE 应用账），用户要改才问。P2P 并发按单 client 线性叠加：每 client ≈ **1,028KB 视频 + 128KB 音频（估）**，直接乘 clients 数。

应用侧可算项（见 `references/formulas.md` 5d）：ring、P2P、音频、日志缓冲都有代码配置；
线程栈按常见配置 **64KB/线程**（默认 8MB 虚拟不驻留），基准已含在 `app_base`，新增线程用 `features.slack.extra_threads`；不要重复加。

### 第 3 步 NPU + 算法（3 问，单独一步）

1. **NPU 池**：CMA 默认 4MB / 按产品 `init.sh` 实测值（见 `references/algo-models.md` 池参考表）/ 自定义 KB。
   池不可随手缩：<项目A> 的 3.5MB 在长测中被动态缓冲吃干（`need=323KB avail=24KB`）。
2. **算法模型**（多选）：先读 `references/algo-models.md`，按平台过滤登记项，列出「无算法」+ 各模型（名称 / KB / 来源）。
   选中多项则 KB 求和填入 `algo_model_kb`。表里没有的走 Other 填「名称 KB」，随后追加登记行。
3. **NPU/算法是否含在 CMA**：在（CMA 池内，算法项单列，不从 CMA 双算）/ 不在（单独加）。

算法模型**只用查表法**，不套分辨率/码率公式；flash 上的模型文件大小不是工作内存。

### 第 4 步 输出（1 问）

Excel + 聊天 / 仅聊天；项目名（Other 填，缺省 `new_t32pro`）。

### 第 5 步 复述确认

贴完整规格表（FS/ENC 每行标注取值来源：源码默认 / 同档建议 / 用户指定），未指定项集中列一行，算法模型行标注登记表 ID，等确认后再跑脚本。

报告口径与问卷对齐：**第 1 步 = RMEM**；**第 2 步 = Linux，分 OS 层（脚本标签 `Linux系统`：kernel/modules/驱动/CMA/cache/zram）与应用层（脚本标签 `Linux应用`：MIKE/miio/P2P/MTP/语控/4G）**；算法与其他单列。

规则（跨步骤）：

- 用户只改某一项时，只重问该项，其余沿用已确认值。
- **VENC ≠ ring**：<项目B> 实测 VENC=1500/350、SDK ring=1800/400，分列填写。
- **算法模型只查表**：从 `references/algo-models.md` 取数；用户给的新模型追加登记行（名称/平台/KB/来源/日期），来源标「用户提供（待实测）」。
- 缓存默认 6s；低于 6s 标「需产品确认」。
- 规格确认后写 JSON，以 `templates/spec-template.json` 为骨架；已知项目直接用对应 `templates/spec-*.json`。
- **DRAM 未知**：64MB / 128MB 各跑一次，并列「硬占用 / 硬余量（附账面值）」；Excel 用推荐档生成，另一档结论留在摘要里。

## 操作 3：估算与聊天摘要

依赖：`python3` + `openpyxl`；`libreoffice` 可选（无则输出公式版，缓存值由 Excel 打开时重算）。

```bash
python3 scripts/estimate_memory.py --spec /tmp/mem_spec.json --no-xlsx --json
```

- `--no-xlsx` 只计算不写文件；`--json` 给出结构化分项，去掉则输出文本摘要。
- `warnings` 会标出 H264 未标定、JPEG 瞬时峰值、多摄 MDNS 分组、海外语控估算等口径提醒。
- 先给用户看这段（数值按 MiB；双摄时 MDNS 分行列出）：

```
rmem : MDNS S0 x.xx | S1 x.xx | FS VB x.xx | VENC x.xx | SDK x.xx = xx.xx MB
Linux: ring x.xx | audio x.xx | P2P x.xx | MTP x.xx | WebRTC x.xx | 固定 xx.xx | 网络 x.xx | 蓝牙 x.xx = xx.xx MB
判定 : Linux 硬余 x.xx；账面预留 x.xx；硬占用 x.xx；硬余量 x.xx（判定看硬余量）
五项 : RMEM xx | Linux系统 xx | Linux应用 xx | 算法 xx | 其他 xx = xx（账面）
预留 : 硬占用 xx.xx MB，硬余量 xx.xx MB（账面 xx.xx / xx.xx）
```

- 超了要量化并给杠杆：nrVBs↓、开 IVDC、码率↓、缓存秒数↓、P2P 客户端/发送块↓。
  其中缓存秒数常被产品约束锁死，先标注再建议。
- 公式与系数来源见 `references/formulas.md`；口径陷阱见 `references/pitfalls.md`。

## 操作 4：输出 Excel

默认路径：`/mnt/data/<project>/docs/<project>_Memory_Estimate.xlsx`；目录不存在则用当前目录。
目标已存在时先问是否覆盖。

```bash
python3 scripts/estimate_memory.py --spec /tmp/mem_spec.json \
  --out /mnt/data/<project>/docs/<project>_Memory_Estimate.xlsx
```

输出 6 个工作表：

| 表 | 内容 |
|---|---|
| 说明 | 公式总览、数据来源、使用步骤、口径限制 |
| 输入参数 | 规格（黄色可改）+ FS（含 Sensor）/ ENC 表 |
| 内存估算 | MDNS 按 sensor 分行 + rmem / Linux 动态 / 固定项与预算判定 |
| 标定校验 | 实测点、模型预测、误差、双点拟合 |
| 速查表 | 常见分辨率 × 码率的横比（双摄按 sensor 各算一行） |
| 系统汇总 | **RMEM / Linux 系统 / Linux 应用 / 算法 / 其他** 五项展开 + 五项合计 + **账面预留 / 硬占用 / 硬余量** |

脚本按 spec 动态生成行数，不再受固定模板容量限制；支持任意 FS / ENC 数。
脚本会自动用 LibreOffice 重算缓存值，并校验 Excel 与 Python 模型一致（差 >0.5% 直接报错）。
无 LibreOffice 时仍输出公式版，提示用户用 Excel 打开重算。

## 操作 5：重标定（可选）

1. 采每个 sensor 主编码通道的 pool 实测字节：`impdbg --enc_info` 或 `/tmp/continuous_mem_info`。
2. 用同产品两档实测值反解 `pool_a` / `pool_b`（`P = a×F + b`）；`k_isp = 实测 MDNS / 该 sensor 最大帧字节`。
3. 写回 spec 的 `coefficients`，重跑脚本；「标定校验」误差应 < 10%。
4. 换 `isp_memopt`、换 SoC、换 H.264 时必须重标，不能沿用 H.265 系数；双摄两个 sensor 的 MDNS 可能不同，分别核对。

## 已知限制

- `系统汇总` 是脚本快照，改 Excel 输入后需重跑脚本；`输入参数` 只覆盖视频/P2P/系数，network/NPU/fixed 在 spec JSON。
- 未实测项目默认套用模板系数/fixed，只给量级；不要用未标定项目承诺 ≤1MB。

## 精度目标（验收）

- 验收标准：同一项目标定后，**五项合计 / 最终预留与实测量的偏差 ≤1MB**；rmem、MIKE SDK 是中间项，尽量贴近。
- 未实测项目只给量级，不承诺 1MB。
- <项目B>（2026-11 实测）：五项合计 vs 实机总占用偏差 **0.41MB**，rmem 0.0005MB、MIKE SDK 0.014MB。
- <项目C>（2026-11 实测）：五项合计 **111.65 vs 111.66MB（0.01MB）**，最终预留 **16.35MB**，rmem/MIKE SDK/miio 逐项吻合（加 slab 后 115.59，差 3.93，待 A/B）。

## 判定口径

- **两本判定**：`账面占用 = 五项合计`；`硬占用 = 五项合计 − file_cache`（page cache 可回收）。判 OK/超看**硬余量**，账面值只作趋势参考。设备能跑不等于账面 ≤ DRAM。
- **可回收项不进账**：Slab 只计 `SUnreclaim`（SReclaimable 的 dentry/inode 不计）、Buffers 不计、file-backed text 不计；zram 按压力上界计入硬账（保守）。
- **默认最大场景**：所有常驻组件在线（WiFi 的 `wpa_supplicant`、`bluetoothd`、`adbd`、`apphilogcat`、`storage_test`、`start_user_process` shell）+ 双 sensor 全流 + P2P 满客户端 + 缓存秒数取最大 + JPEG 抓拍瞬时 + SD 录像/上行同跑。稳态快照只作参考，无出处/估算项按上界取值。
- **rmem 需求**由 MDNS + FS VB + VENC + JPEG + SDK 自动计算，不做输入；向上取整 1MB 作为 bootargs 建议值。
- Linux：`可见 = DRAM − rmem 需求`；`硬合计 = 动态 + 固定 + 网络/蓝牙 − file_cache`；`硬余量 = 可见 − 硬合计`。
- 系统：`账面占用 = rmem + Linux 合计 +（独立 NPU/算法）`；`硬占用 = 账面 − file_cache`；`硬余量 = DRAM − 硬占用`。
- 五项展开：`RMEM / Linux 系统 / Linux 应用 / 算法 / 其他`；账面预留 = DRAM − 五项合计，硬余量 = DRAM −（五项合计 − file_cache）。
- 固定项是历史实测基线；file cache / zram 可回收/压缩，判定看趋势，不当硬上限。
- 单位统一用 MiB（1,048,576 B）；对外说「MB」时注明按 1024 换算。
- 两本账不相加：rmem 需求先从 DRAM 扣掉，再算 Linux 可见；rmem 不能拿 Linux 余量补。
- 本模型只对 **Ingenic T32Pro（<PRJxxx>）** 标定；T31/T33/其他平台必须重新采集系数。

## 支持文件清单

- `scripts/estimate_memory.py` — 规格 JSON → 计算（rmem/Linux/NPU/算法/最终预留）→ 动态生成 Excel → 重算校验；`--no-xlsx` 仅计算
- `templates/spec-template.json` — 单摄规格骨架（<项目A> 默认）
- `templates/spec-dual-wifi.json` — <项目C> 双摄规格（6 FS / 8 ENC，含 H264/JPEG 开关）
- `templates/spec-4k-4g.json` — <项目B> 单摄 4G 规格（MTP enabled，64MB / rmem 23.5MB / 9s）
- `templates/spec-dual-wifi-b01.json` — <项目C-B01> 海外双摄（H264 + WebRTC，128MB / rmem 42MB）
- `templates/spec-4k-wifi.json` — <项目A> 单摄 4K 预测（MTP/WebRTC 关闭，NPU 4MB）
- `templates/spec-dual-4g.json` — <项目D> 双摄 4G（VENC 800/350，ring 1800/700，9s/2 clients，H264/WebRTC off）
- `references/formulas.md` — 公式推导、双点拟合、系数来源、双摄与适用边界
- `references/algo-models.md` — 算法模型工作内存登记表（查表法，含 NPU 池参考值；新模型登记到这里）
- `references/pitfalls.md` — 口径陷阱：rmem 不可见、宏名假账、JPEG 按需、双摄不是乘 2 等
- `CHANGELOG.md` — 版本记录
