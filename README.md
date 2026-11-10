# estimate-memory-budget

嵌入式 IPC 内存预算评估技能，双平台：**君正 T32Pro**（rmem 公式）与 **SigmaStar**（MMA 堆实测或分块公式）。从音视频规格出发，估 **媒体保留区** 与 **Linux 侧（OS + 应用）** 占用，输出聊天摘要与 Excel 报告。

## 安装

```bash
# 国内源（gitee）
curl -fsSL https://gitee.com/GreatBigM/estimate-memory-budget-skill/raw/main/install.sh | bash

# 海外源（GitHub 镜像）
curl -fsSL https://raw.githubusercontent.com/GreatBigM/estimate-memory-budget-skill/main/install.sh | bash

# 指定目标 / 全部目标
... | bash -s -- --target claude,hermes
... | bash -s -- --all
```

支持 Hermes / Claude Code / DSH；重复执行 = 升级（自动备份旧版 + 版本对比）。

手动安装：把 `SKILL.md`、`templates/`、`references/`、`scripts/` 复制到对应 agent 的 skills 目录下。

## 用法

```
用户: 评估一个新机型：T32Pro，双摄，主码流 2560×1440
AI:  平台 → 逐路 sensor（主/次/抓拍/继续）→ Linux OS/应用 → NPU+算法 → 输出 → 复述确认
     → 脚本按 64/128MB 各跑一档 → 聊天摘要 + Excel
```

技能会先问音视频规格（分辨率、流数、次码流、抓拍、缓存秒数、P2P 客户端、WiFi/BT、MTP、语控、NPU/算法），**不问目标 DRAM**——DRAM 是估算结论，不是输入。

## 模型要点

- **rmem**：`MDNS + FS VB + VENC pool + JPEG + SDK 预留`，由分辨率/流数/编码器配置计算；**码率只影响 SDK ring，不影响 VENC pool**
- **Linux**：OS 层（kernel/modules/CMA/slab/file_cache/zram）+ 应用层（MIKE SDK ring/P2P/MTP/语控 + miio）
- **两本判定**：`账面 = 五项合计`；`硬占用 = 账面 − file_cache`；**判 OK/超看硬余量**
- **可回收项**：Slab 只计 `SUnreclaim`；`SReclaimable`、Buffers、file-backed text 不计；zram 按压力上界保守计入

### SigmaStar 分支

- 媒体内存是 `mma_heap` 的 carve-out：有实机采 `mma_heap_name0` 逐块归组；无实机按分块公式推
  （`VENC recon 0.872×F`、`ES 池默认 W×H/2`、`3DNR level3 0.511×F`、`SYS 1.46MiB`、`SCL 0.40×F_out+0.59`）
- **绑定模式决定有没有帧**：realtime 不落 DRAM，HW_RING 1/4 帧，FRAME_BASE ≥3 帧（端口默认深度 4）
- 应用侧 ring：`size = bitrate×128×秒`、`nodes = fps×秒`；每摄含主/次/第三 + AOV 录像 + 音频
- 判定同样按两本账，`Linux 可见 = DRAM − mma_heap cap`

## 数据来源与精度

- 公式系数由同产品两档实测反解（`P = pool_a×F + pool_b`），不跨产品族混用
- 固定基线取各产品 `continuous_mem_info` / `smaps` / `/proc/meminfo` 实测快照
- 同一构建内可复现；跨产品必须重采基线，未标定项目只给量级，不承诺 ≤1MB
- 算法模型走查表（`references/algo-models.md`），新模型可追加登记行

## 目录

```
SKILL.md                  主入口（流程/判定/清单）
templates/                各规格骨架（君正单摄/双摄、SigmaStar 实测/公式）
references/formulas.md    公式推导、标定、可算性盘点、待确认清单
references/pitfalls.md    口径陷阱
references/algo-models.md 算法模型工作内存登记表
scripts/estimate_memory.py 计算 + Excel 生成 + 自检
install.sh                一键安装/更新
```

## 许可

MIT
