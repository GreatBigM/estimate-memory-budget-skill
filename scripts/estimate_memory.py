#!/usr/bin/env python3
"""内存预算估算：读 JSON 规格 -> 计算 rmem/Linux 分项 -> 动态生成 Excel。

支持单摄/双摄/多摄：FS 行按 sensor 分组，每组独立计算 ISP MDNS。
用法:
  python3 estimate_memory.py --spec spec.json --out out.xlsx [--json] [--quiet]
  python3 estimate_memory.py --spec spec.json --no-xlsx [--json]
  python3 estimate_memory.py --self-test

无 TTY 交互；所有输入走 --spec。
"""
import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation

HERE = os.path.dirname(os.path.abspath(__file__))
MIB = 1024 * 1024
FIXED_KEYS = ("kernel", "modules", "cma", "slab", "app_base", "miio_client", "miio_ot", "other_procs", "file_cache", "zram")

DEFAULTS = {
    "project": "<项目A>",
    "platform": "T32Pro",
    "network": {"type": "wifi", "wifi_supplicant_kb": 0, "lte_supplicant_kb": 0, "lte_module_kb": 0},
    "dram_mb": 64,
    "buffer_seconds": 6,
    "p2p": {"clients": 2, "video_kb": 1024, "video_bufs": 1, "audio_kb": 32, "audio_bufs": 2},
    "ivdc_mem_line": 200,
    "coefficients": {
        "k_isp": 0.7582, "k_recon": 0.5875, "k_stream": 6.9780,
        "maxi_factor": 2.5, "k_jpeg": 1.0, "jpeg_overhead": 8320,
    },
    "audio": {"bitrate": 100, "channels": 1},
    "features": {
        "mtp": {
            "enabled": False, "sessions": 3, "fixed_kb": 128,
            "wnd_rcv": 128, "wnd_snd": 32, "mtu_bytes": 920,
            "overhead_kb_per_session": 64, "measured_bytes": 0,
        },
        "webrtc": {
            "enabled": False, "sessions": 1, "fixed_kb": 128,
            "per_session_kb": 1024, "measured_bytes": 0,
        },
        "npu_algo": {"npu_pool_kb": 0, "algo_model_kb": 0, "inside_cma": True},
        "bluetooth": {"enabled": False, "userspace_kb": 868},
        "slack": {"extra_threads": 0, "thread_stack_kb": 64},
    },
    "sdk_reserved": 113152,
    "frame_sources": [
        {"enabled": True, "sensor": 0, "name": "FS0 主码流", "w": 3840, "h": 2160, "nrvbs": 1, "ivdc": True},
        {"enabled": True, "sensor": 0, "name": "FS1 次码流/抓拍", "w": 832, "h": 480, "nrvbs": 2, "ivdc": False},
    ],
    "encoders": [
        {"enabled": True, "sensor": 0, "name": "enc0 主码流", "type": "H265", "w": 3840, "h": 2160, "bitrate": 2000, "ring": True},
        {"enabled": True, "sensor": 0, "name": "enc1 次码流", "type": "H265", "w": 832, "h": 480, "bitrate": 350, "ring": True},
        {"enabled": False, "sensor": 0, "name": "enc2 JPEG 抓拍", "type": "JPEG", "w": 832, "h": 480, "bitrate": 0, "ring": False},
    ],
    "fixed": {
        "kernel": 6501171, "modules": 1782579, "cma": 4194304, "slab": 4071424,
        "app_base": 2097152, "miio_client": 0, "miio_ot": 0, "other_procs": 3565158,
        "file_cache": 8283750, "zram": 5242880,
    },
}


def merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def frame_bytes(w, h):
    return float(w) * float(h) * 1.5


def _warnings(spec):
    warns = []
    if str(spec.get("platform", "")).replace(" ", "").lower() != "t32pro":
        warns.append(f"平台 {spec.get('platform')} 非 T32Pro：系数按 T32Pro 标定，需重标")
    enabled = [e for e in spec["encoders"] if e.get("enabled")]
    if any(str(e.get("type", "")).upper() == "H264" for e in enabled):
        warns.append("含 H264 通道：按 H265 模型 × h264_factor 估算，建议重标")
    if any(str(e.get("type", "")).upper() == "JPEG" for e in enabled):
        warns.append("JPEG 已计入（开机常驻）；若按需建拆请改 enabled=false")
    sensors = {int(fs.get("sensor", 0)) for fs in spec["frame_sources"] if fs.get("enabled")}
    if len(sensors) > 1:
        warns.append(f"多摄：MDNS 按 {len(sensors)} 个 sensor 分组分别计算")
    w = spec.get("features", {}).get("webrtc", {})
    if w.get("enabled"):
        if w.get("measured_bytes", 0) > 0:
            warns.append("海外语控 SDK 使用实测字节")
        else:
            warns.append("海外语控 SDK 按每会话默认值估算，建议用 smaps/会话日志实测覆盖")
    na = spec.get("features", {}).get("npu_algo", {})
    if na.get("inside_cma", True):
        cma_kb = float(spec.get("fixed", {}).get("cma", 0)) / 1024.0
        if cma_kb and (na.get("npu_pool_kb", 0) + na.get("algo_model_kb", 0)) > cma_kb:
            warns.append("NPU+算法超过 CMA 总量：超出部分按独立预留计，建议核对 cma_pool_size")
    if not spec.get("calibrated", False):
        warns.append("未标定项目：系数/fixed 为模板/继承值，仅量级参考，不可用于 ≤1MB 验收")
    if len(sensors) > 1 and spec.get("sdk_reserved", 0) <= 200000:
        warns.append("多摄但 SDK 预留仍为单摄默认值；请覆盖为双摄实测/模板值")
    net = spec.get("network", {})
    nt = str(net.get("type", "wifi")).lower()
    if nt in ("4g", "wifi+4g") and spec.get("fixed", {}).get("other_procs", 0) > 0:
        warns.append("4G 已单列；确认 other_procs 不再包含 4G supplicant，避免双算")
    if nt in ("wifi", "wifi+4g") and float(net.get("wifi_supplicant_kb", 0)) <= 0:
        warns.append("WiFi 机型未填 network.wifi_supplicant_kb：wpa_supplicant 可能已含在 other_procs 快照，也可能漏计（<参考机> 实测 wpa 1,608KB + udhcpc 468KB）")
    if spec.get("features", {}).get("bluetooth", {}).get("enabled"):
        warns.append("蓝牙按 miio_bt 用户态 868KB 计（<参考机> 实测）；内核模块 bluetooth.ko + aic_btusb 约 240KB 应在 fixed.modules 基线内，若基线未含需自行加回")
    sl = spec.get("features", {}).get("slack", {})
    if float(sl.get("extra_threads", 0)) > 0:
        warns.append(f"新增 {sl.get('extra_threads')} 个线程按 {sl.get('thread_stack_kb', 64)}KB/线程计（常见配置；基准线程栈已含在 app_base）")
    m = spec.get("features", {}).get("mtp", {})
    if m.get("enabled"):
        if m.get("measured_bytes", 0) > 0:
            warns.append("MTP 使用实测字节")
        else:
            warns.append("MTP 按 KCP 窗口估算，建议用 smaps 实测覆盖")
    return warns


def compute(spec):
    c = spec["coefficients"]
    fs_enabled = [x for x in spec["frame_sources"] if x.get("enabled")]

    sensor_max = OrderedDict()
    for x in fs_enabled:
        s = int(x.get("sensor", 0))
        sensor_max[s] = max(sensor_max.get(s, 0.0), frame_bytes(x["w"], x["h"]))
    mdns_by_sensor = OrderedDict((s, c["k_isp"] * f) for s, f in sorted(sensor_max.items()))
    mdns = sum(mdns_by_sensor.values())

    fs_vb = sum(
        spec["ivdc_mem_line"] * x["w"] * 1.5 if x.get("ivdc")
        else x.get("nrvbs", 0) * frame_bytes(x["w"], x["h"])
        for x in fs_enabled
    )

    venc_items, rings = [], 0.0
    for e in spec["encoders"]:
        if not e.get("enabled"):
            continue
        f = frame_bytes(e["w"], e["h"])
        codec = str(e.get("type", "")).upper()
        if codec == "JPEG":
            pool = c["k_jpeg"] * f + c["jpeg_overhead"]
        else:
            if c.get("pool_a") is not None:
                pool = float(c["pool_a"]) * f + float(c.get("pool_b", 0))
            else:
                max_i = float(e.get("bitrate", 0)) * c["maxi_factor"] * 1000.0 / 8.0
                pool = c["k_recon"] * f + c["k_stream"] * max_i
            if codec == "H264":
                pool *= c.get("h264_factor", 1.22)
            if e.get("ring") and codec != "JPEG":
                rings += float(e.get("ring_bitrate", e.get("bitrate", 0))) * 128.0 * spec["buffer_seconds"]
        venc_items.append({"name": e.get("name", "enc"), "bytes": pool, "sensor": e.get("sensor", 0), "type": codec})

    a = spec["audio"]
    audio_ring = float(a.get("bitrate", 0)) * 128.0 * spec["buffer_seconds"] * a.get("channels", 1)
    p = spec["p2p"]
    p2p = float(p.get("clients", 0)) * (
        p.get("video_kb", 0) * 1024.0 * p.get("video_bufs", 1)
        + p.get("audio_kb", 0) * 1024.0 * p.get("audio_bufs", 2) * 2
    )
    fixed_total = float(sum(spec["fixed"].get(k, 0) for k in FIXED_KEYS))

    m = spec.get("features", {}).get("mtp", {})
    mtp = 0.0
    if m.get("enabled"):
        if m.get("measured_bytes", 0) > 0:
            mtp = float(m["measured_bytes"])
        else:
            mtp = (
                m.get("fixed_kb", 128) * 1024.0
                + m.get("sessions", 3) * (
                    (m.get("wnd_rcv", 128) + m.get("wnd_snd", 32)) * m.get("mtu_bytes", 920)
                    + m.get("overhead_kb_per_session", 64) * 1024.0
                )
            )

    w = spec.get("features", {}).get("webrtc", {})
    webrtc = 0.0
    if w.get("enabled"):
        if w.get("measured_bytes", 0) > 0:
            webrtc = float(w["measured_bytes"])
        else:
            webrtc = w.get("fixed_kb", 128) * 1024.0 + w.get("sessions", 1) * w.get("per_session_kb", 1536) * 1024.0

    rmem = mdns + fs_vb + sum(x["bytes"] for x in venc_items) + spec["sdk_reserved"]
    linux_dyn = rings + audio_ring + p2p + mtp + webrtc
    linux_visible = spec["dram_mb"] * MIB - rmem
    linux_total = linux_dyn + fixed_total

    net = spec.get("network", {})
    network_kb = 0.0
    nt = str(net.get("type", "wifi")).lower()
    if nt in ("wifi", "wifi+4g"):
        network_kb += float(net.get("wifi_supplicant_kb", 0))
    if nt in ("4g", "wifi+4g"):
        network_kb += float(net.get("lte_supplicant_kb", 0) + net.get("lte_module_kb", 0))

    na = spec.get("features", {}).get("npu_algo", {})
    npu_kb = float(na.get("npu_pool_kb", 0))
    algo_kb = float(na.get("algo_model_kb", 0))
    npu_algo_kb = npu_kb + algo_kb
    inside_cma = bool(na.get("inside_cma", True))

    bt = spec.get("features", {}).get("bluetooth", {})
    bluetooth_kb = float(bt.get("userspace_kb", 868)) if bt.get("enabled") else 0.0

    sl = spec.get("features", {}).get("slack", {})
    extra_threads = float(sl.get("extra_threads", 0))
    thread_stack_kb = float(sl.get("thread_stack_kb", 64))
    extra_threads_kb = extra_threads * thread_stack_kb
    if extra_threads_kb:
        linux_dyn += extra_threads_kb * 1024.0
        linux_total = linux_dyn + fixed_total

    linux_total_full = linux_total + network_kb * 1024.0 + bluetooth_kb * 1024.0
    apps = {
        "mike_base": float(spec["fixed"].get("app_base", 0)),
        "mike_dynamic": linux_dyn,
        "mike_total": linux_dyn + float(spec["fixed"].get("app_base", 0)),
        "miio_client": float(spec["fixed"].get("miio_client", 0)),
        "miio_ot": float(spec["fixed"].get("miio_ot", 0)),
        "miio_total": float(spec["fixed"].get("miio_client", 0) + spec["fixed"].get("miio_ot", 0)),
        "other_procs": float(spec["fixed"].get("other_procs", 0)),
        "network": network_kb * 1024.0,
        "bluetooth": bluetooth_kb * 1024.0,
        "other_total": float(spec["fixed"].get("other_procs", 0)) + network_kb * 1024.0 + bluetooth_kb * 1024.0,
        "platform": float(sum(spec["fixed"].get(k, 0) for k in ("kernel", "modules", "cma", "slab", "file_cache", "zram"))),
    }
    apps["total"] = apps["mike_total"] + apps["miio_total"] + apps["other_total"]

    npu_algo_bytes = npu_algo_kb * 1024.0
    platform_cma = max(0.0, float(spec["fixed"].get("cma", 0)) - (npu_algo_bytes if inside_cma else 0.0))
    linux_system = (
        float(spec["fixed"].get("kernel", 0)) + float(spec["fixed"].get("modules", 0))
        + platform_cma + float(spec["fixed"].get("slab", 0))
        + float(spec["fixed"].get("file_cache", 0)) + float(spec["fixed"].get("zram", 0))
    )
    linux_apps = apps["mike_total"] + apps["miio_total"]
    algorithm = npu_algo_bytes
    other = apps["other_total"]
    top_total = rmem + linux_system + linux_apps + algorithm + other
    system_used = top_total
    final_reserve = spec["dram_mb"] * MIB - top_total
    file_cache = float(spec["fixed"].get("file_cache", 0))
    linux_total_hard = linux_total_full - file_cache
    hard_total = top_total - file_cache
    hard_reserve = spec["dram_mb"] * MIB - hard_total
    bootargs_rmem_kb = float(spec.get("rmem_bootargs_mb", 0)) * 1024.0
    bootargs_need_gap = max(0.0, bootargs_rmem_kb * 1024.0 - rmem)
    runtime_reserve = final_reserve - bootargs_need_gap

    return {
        "project": spec["project"],
        "platform": spec.get("platform", "T32Pro"),
        "warnings": _warnings(spec),
        "system": {
            "dram": spec["dram_mb"] * MIB,
            "rmem_need": rmem,
            "linux_dynamic": linux_dyn, "linux_fixed": fixed_total,
            "network": network_kb * 1024.0,
            "bluetooth": bluetooth_kb * 1024.0,
            "linux_total": linux_total_full,
            "linux_visible": linux_visible,
            "linux_free": linux_visible - linux_total_full,
            "apps": apps,
            "top": {
                "rmem": rmem, "linux_system": linux_system, "linux_apps": linux_apps,
                "algorithm": algorithm, "other": other, "total": top_total,
                "final_reserve": final_reserve,
                "kernel": float(spec["fixed"].get("kernel", 0)),
                "modules": float(spec["fixed"].get("modules", 0)),
                "platform_cma": platform_cma,
                "slab": float(spec["fixed"].get("slab", 0)),
                "file_cache": float(spec["fixed"].get("file_cache", 0)),
                "zram": float(spec["fixed"].get("zram", 0)),
            },
            "npu": npu_kb * 1024.0, "algo": algo_kb * 1024.0,
            "npu_algo": npu_algo_kb * 1024.0, "inside_cma": inside_cma,
            "system_used": system_used, "final_reserve": final_reserve,
            "hard_total": hard_total, "hard_reserve": hard_reserve,
            "bootargs_rmem": bootargs_rmem_kb * 1024.0,
            "runtime_reserve": runtime_reserve,
        },
        "rmem": {
            "mdns_by_sensor": mdns_by_sensor, "mdns": mdns, "fs_vb": fs_vb,
            "venc": venc_items, "sdk_reserved": spec["sdk_reserved"],
            "total": rmem, "need": rmem,
        },
        "linux": {
            "rings": rings, "audio_ring": audio_ring, "p2p": p2p, "mtp": mtp, "webrtc": webrtc,
            "dynamic": linux_dyn, "fixed": fixed_total, "total": linux_total_full,
            "total_hard": linux_total_hard, "visible": linux_visible,
            "headroom": linux_visible - linux_total_full,
            "headroom_hard": linux_visible - linux_total_hard,
        },
        "verdict": {"linux_ok": linux_total_hard <= linux_visible, "reserve_ok": hard_reserve >= 0},
    }


# ---------------- Excel styling ----------------
NAVY, LBLUE, YELLOW, GREY = "1F4E79", "DDEBF7", "FFF2CC", "F2F2F2"
GREEN, RED, WHITE = "E2EFDA", "FCE4EC", "FFFFFF"
FONT = "Microsoft YaHei"
_thin = Side(style="thin", color="BFBFBF")
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)


def title(ws, text, span):
    ws.merge_cells(f"A1:{span}1")
    c = ws["A1"]
    c.value = text
    c.font = Font(name=FONT, size=16, bold=True, color=WHITE)
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 30


def section(ws, row, text, ncols):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(name=FONT, size=11, bold=True, color=NAVY)
    c.fill = PatternFill("solid", fgColor=LBLUE)
    c.alignment = Alignment(horizontal="left", vertical="center")
    for col in range(1, ncols + 1):
        ws.cell(row=row, column=col).border = BORDER


def header(ws, row, values):
    for i, v in enumerate(values, 1):
        c = ws.cell(row=row, column=i, value=v)
        c.font = Font(name=FONT, size=9, bold=True, color=WHITE)
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
    ws.row_dimensions[row].height = 30


def put(ws, row, col, value, fmt=None, fill=None, bold=False, wrap=True, align="left", size=9):
    c = ws.cell(row=row, column=col, value=value)
    c.font = Font(name=FONT, size=size, bold=bold)
    if fmt:
        c.number_format = fmt
    if fill:
        c.fill = PatternFill("solid", fgColor=fill)
    c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
    c.border = BORDER
    return c


def _cn(v):
    return "是" if v else "否"


def build_workbook(spec, result, out_path):
    n_fs, n_enc = len(spec["frame_sources"]), len(spec["encoders"])
    fs_start = 46
    fs_rows = list(range(fs_start, fs_start + n_fs))
    enc_sec = fs_start + n_fs + 1
    enc_hdr = enc_sec + 1
    enc_start = enc_hdr + 1
    enc_rows = list(range(enc_start, enc_start + n_enc))

    wb = Workbook()

    # ============ 说明 ============
    ws = wb.active
    ws.title = "说明"
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGHI", [4, 24, 34, 14, 14, 14, 14, 14, 34]):
        ws.column_dimensions[col].width = w
    title(ws, f"{spec.get('platform', 'T32Pro')} / {spec['project']} 内存预估工具", "I")
    ws.merge_cells("B2:I2")
    put(ws, 2, 2, f"平台：{spec.get('platform', 'T32Pro')}；按分辨率、码率、缓存时长与连接数估算媒体管线 / SDK 内存；支持单摄与多摄（FS 按 sensor 分组）。", size=10)
    ws.row_dimensions[2].height = 30

    section(ws, 4, "一、核心公式（单位：字节）", 9)
    header(ws, 5, ["", "内存项", "公式", "", "", "", "", "", "说明"])
    ws.merge_cells("C5:H5")
    formulas = [
        ("单帧 NV12", "F = W × H × 1.5", "4K 单帧 12,441,600 B"),
        ("编码器配置", "分辨率 / 编码流数 / 编码器配置", "码率不进 VENC pool；码率只影响 ring"),
        ("编码器 pool", "P = pool_a × F + pool_b", "只与分辨率/流数有关，与码率无关"),
        ("IVDC 帧源 VB", "V = ivdc_mem_line × W × 1.5", "只分配行缓存，不分配整帧"),
        ("普通帧源 VB", "V = nrVBs × F", "非 IVDC 通道"),
        ("JPEG 抓拍 pool", "J = k_jpeg × F + overhead", "<项目B> 实测 832×480 = 609,536 B"),
        ("ISP MDNS", "M_s = k_isp × max(F of sensor s)", "每个 sensor 独立一份"),
        ("SDK 流缓存 ring", "R = ring 码率(kbps) × 128 × 缓存秒数", "libmike.a 反汇编坐实；ring 码率可不同于 VENC"),
        ("P2P 缓冲", "B = 客户端 × (视频块 + 音频块×2)", "音频按收发双向"),
        ("MTP SDK（可选）", "M = 固定 + 会话 × ((wnd_rcv+wnd_snd)×MTU + 开销)", "KCP 128/32/920；<项目B> 支持，实测优先"),
    ]
    for r, (name, f, note) in enumerate(formulas, 6):
        put(ws, r, 2, name, bold=True)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=8)
        put(ws, r, 3, f, fill=GREY, align="center")
        for cc in range(4, 9):
            ws.cell(row=r, column=cc).border = BORDER
        put(ws, r, 9, note)
        ws.row_dimensions[r].height = 24

    section(ws, 16, "二、数据来源", 9)
    sources = [
        ("流参数", "项目 config.h + video_hal_config.h 的 PROJECT_<project> 分支"),
        ("SDK ring", "qwiki 20261016-c-mi-ipc-memory-profile.md §八（反汇编）"),
        ("rmem 分项", "<项目B> 2026-11 continuous_mem_info（当前默认）；<项目A> 2026-06 28MB 场景为历史参考"),
        ("JPEG pool", "qwiki 20261016-jpeg-snap-ondemand.md（607,360 B）"),
        ("P2P 参数", "mike_p2p.c（音频 32KB；视频发送块按像素档）"),
        ("固定项基线", "项目内存卡片 / CLAUDE.md；无则用模板默认"),
    ]
    r = 17
    for k, v in sources:
        put(ws, r, 2, k, bold=True)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=9)
        put(ws, r, 3, v)
        for cc in range(4, 10):
            ws.cell(row=r, column=cc).border = BORDER
        r += 1

    section(ws, 24, "三、使用步骤", 9)
    steps = [
        "1. 在『输入参数』改分辨率/码率/nrVBs/IVDC/缓存/P2P；黄色格可编辑。",
        "2. 看『内存估算』：rmem 按 sensor 分组，Linux 动态与固定项分开列。",
        "3. 新项目/新构建：按『标定校验』采集实测，更新 pool_a / pool_b 与 fixed 基线。",
        "4. 横比方案：看『速查表』。",
    ]
    r = 25
    for s in steps:
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=9)
        put(ws, r, 2, s)
        r += 1

    section(ws, 30, "四、口径与限制", 9)
    caveats = [
        "① 默认系数来自 <项目B> 2026-11 实测（T32Pro / isp_memopt=0x7）；<项目A> 2026-06 的 28MB 场景是历史参考。换项目仍须实机重标。",
        "② rmem 在 /proc/meminfo 不可见；用 impdbg 或 /tmp/continuous_mem_info 验证。",
        "③ 双摄/多摄：MDNS 按 sensor 分组，每组取该组最大帧；不要把单摄账直接乘 2，也不要把同组多个 FS 帧相加。",
        "④ pool_a / pool_b 由同产品两档实测反解（4K+480P）；换产品族/分辨率需重标。旧式 k_recon/k_stream 仅作回退。",
        "⑤ P2P 单 client 实测 1,028KB 视频 + 128KB 音频（估）；并发按 client 线性叠加。",
        "⑥ 帧率 / GOP 不进内存公式（只影响 CPU 与码流波动）。",
        "⑦ 两本账：账面 = 五项合计；硬占用 = 账面 − file_cache；判定看硬余量。Slab 只计 SUnreclaim；SReclaimable、Buffers、file-backed text 不计；zram 按压力上界计入硬账。",
        "⑧ MTP 是小米 MTP SDK（KCP），不是 USB MTP；默认按 KCP 窗口估算，重要项目用 smaps 实测覆盖 B35。",
    ]
    r = 31
    for s in caveats:
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=9)
        put(ws, r, 2, s, fill=RED if r == 31 else WHITE)
        ws.row_dimensions[r].height = 30
        r += 1

    # ============ 输入参数 ============
    ws = wb.create_sheet("输入参数")
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGHIJ", [8, 22, 8, 8, 10, 10, 11, 11, 11, 28]):
        ws.column_dimensions[col].width = w
    title(ws, "输入参数（黄色单元格可编辑）", "I")
    ws.merge_cells("A2:I2")
    put(ws, 2, 1, "改这里 → 『内存估算』自动重算；多摄时 FS 表的 Sensor 列填 0/1/…", size=9)

    section(ws, 4, "一、全局参数", 9)
    header(ws, 5, ["参数", "值", "单位", "说明 / 来源"])
    ws.merge_cells("D5:I5")
    globals_ = [
        (6, "流缓存时长", spec["buffer_seconds"], "s", "MIKE_STREAM_BUFFER_MAX_SECONDS"),
        (7, "P2P 最大客户端数", spec["p2p"]["clients"], "个", "MAX_P2P_CLIENTS"),
        (8, "P2P 视频发送块", spec["p2p"]["video_kb"], "KB", "推荐：8MP 1024KB，5MP 以下 512KB"),
        (9, "P2P 音频块", spec["p2p"]["audio_kb"], "KB", "mike_p2p.c 推荐 32KB"),
        (10, "P2P 每客户端音频块数", spec["p2p"]["audio_bufs"], "块", "估算：发送 + 接收"),
        (11, "P2P 每客户端视频块数", spec["p2p"]["video_bufs"], "块", "估算"),
        (12, "ivdc_mem_line", spec["ivdc_mem_line"], "行", "init.sh insmod tx-isp 参数"),
        (13, "k_isp", spec["coefficients"]["k_isp"], "×单帧", "MDNS 系数；双摄每个 sensor 各乘一次"),
        (14, "k_recon", spec["coefficients"]["k_recon"], "×单帧", "编码器重建帧系数（标定拟合）"),
        (15, "k_stream", spec["coefficients"]["k_stream"], "×maxI", "编码器码流缓冲系数（标定拟合）"),
        (16, "maxI 系数", spec["coefficients"]["maxi_factor"], "×码率", "maxIPictureSize = maxBitRate × 5/2"),
        (17, "k_jpeg", spec["coefficients"]["k_jpeg"], "×单帧", "JPEG 码流缓冲系数"),
        (18, "JPEG 固定开销", spec["coefficients"]["jpeg_overhead"], "B", "607,360 − 599,040"),
        (19, "音频码率", spec["audio"]["bitrate"], "kbps", "MIKE_STREAM_AUDIO_BITRATE"),
        (20, "音频通道数", spec["audio"]["channels"], "路", "音频 ring 路数"),
        (21, "SDK 预留块", spec["sdk_reserved"], "B", "2 × 512,000 reuse=FREE"),
        (22, "rmem 需求", "自动", "MB", "由 MDNS/FS VB/VENC/JPEG/SDK 自动计算，不做输入"),
        (23, "总 DRAM", spec["dram_mb"], "MB", "硬件"),
        (24, "1 MB", MIB, "B", "单位换算"),
        (25, "h264_factor", spec["coefficients"].get("h264_factor", 1.22), "×H265模型", "H264 无 RFC，按 H265 模型乘此系数"),
        (42, "pool_a", spec["coefficients"].get("pool_a", 0), "×单帧", "码率无关 pool 系数（与 pool_b 配对）；0 = 回退旧式 k_recon/k_stream"),
        (43, "pool_b", spec["coefficients"].get("pool_b", 0), "B", "pool 固定项（B）"),
    ]
    for row, name, val, unit, note in globals_:
        put(ws, row, 1, name, bold=True)
        put(ws, row, 2, val, fmt="0.0000" if isinstance(val, float) else "#,##0", fill=YELLOW, align="center")
        put(ws, row, 3, unit, align="center")
        ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=9)
        put(ws, row, 4, note)
        for cc in range(5, 10):
            ws.cell(row=row, column=cc).border = BORDER
        ws.row_dimensions[row].height = 22

    mtp = spec.get("features", {}).get("mtp", {})
    section(ws, 27, "二、功能 SDK（MTP / WebRTC，可选）", 9)
    header(ws, 28, ["参数", "值", "单位", "说明 / 来源"])
    ws.merge_cells("D28:I28")
    mtp_rows = [
        (29, "启用 MTP", _cn(mtp.get("enabled")), "", "是/否；<项目B> 选是"),
        (30, "MTP 会话数", mtp.get("sessions", 3), "个", "默认 max(3, P2P clients+1)"),
        (31, "MTP 固定开销", mtp.get("fixed_kb", 128), "KB", "SDK 池/管理器"),
        (32, "KCP wnd_rcv", mtp.get("wnd_rcv", 128), "段", "SDK 常量 MTP_IKCP_WND_RCV=128"),
        (33, "KCP wnd_snd", mtp.get("wnd_snd", 32), "段", "SDK 常量 MTP_IKCP_WND_SND=32"),
        (34, "KCP MTU", mtp.get("mtu_bytes", 920), "B", "SDK 常量 MTP_IKCP_MTU_DEF=920"),
        (35, "MTP 每会话开销", mtp.get("overhead_kb_per_session", 64), "KB", "会话/MBuffer/加密等，估算"),
        (36, "MTP 实测覆盖", mtp.get("measured_bytes", 0), "B", ">0 时优先于公式"),
    ]
    for row, name, val, unit, note in mtp_rows:
        put(ws, row, 1, name, bold=True)
        fmt = "#,##0" if isinstance(val, int) else "0"
        put(ws, row, 2, val, fmt=fmt, fill=YELLOW, align="center")
        put(ws, row, 3, unit, align="center")
        ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=9)
        put(ws, row, 4, note)
        for cc in range(5, 10):
            ws.cell(row=row, column=cc).border = BORDER
        ws.row_dimensions[row].height = 22
    dv_mtp = DataValidation(type="list", formula1='"是,否"', allow_blank=False)
    ws.add_data_validation(dv_mtp); dv_mtp.add("B29")

    web = spec.get("features", {}).get("webrtc", {})
    web_rows = [
        (37, "启用 WebRTC", _cn(web.get("enabled")), "", "是/否；<项目C> B01 / HWYK 选是"),
        (38, "WebRTC 会话数", web.get("sessions", 1), "个", "HWYK 独占 1 路"),
        (39, "WebRTC 固定开销", web.get("fixed_kb", 128), "KB", "SDK/信令/初始化"),
        (40, "每会话开销", web.get("per_session_kb", 1536), "KB", "ICE/DTLS/SCTP/RTP/jitter，估算"),
        (41, "WebRTC 实测覆盖", web.get("measured_bytes", 0), "B", ">0 时优先于公式"),
    ]
    for row, name, val, unit, note in web_rows:
        put(ws, row, 1, name, bold=True)
        put(ws, row, 2, val, fmt="#,##0", fill=YELLOW, align="center")
        put(ws, row, 3, unit, align="center")
        ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=9)
        put(ws, row, 4, note)
        for cc in range(5, 10):
            ws.cell(row=row, column=cc).border = BORDER
        ws.row_dimensions[row].height = 22
    dv_web = DataValidation(type="list", formula1='"是,否"', allow_blank=False)
    ws.add_data_validation(dv_web); dv_web.add("B37")

    section(ws, 44, "三、帧源通道 FrameSource / VB（sensor 分组）", 9)
    header(ws, 45, ["启用", "名称", "Sensor", "宽 W", "高 H", "nrVBs", "IVDC直通", "备注"])
    ws.merge_cells("H45:I45")
    for i, r in enumerate(fs_rows):
        x = spec["frame_sources"][i]
        put(ws, r, 1, _cn(x.get("enabled")), fill=YELLOW, align="center")
        put(ws, r, 2, x.get("name", ""), bold=True)
        put(ws, r, 3, int(x.get("sensor", 0)), fmt="0", fill=YELLOW, align="center")
        put(ws, r, 4, x.get("w", 0), fmt="#,##0", fill=YELLOW, align="center")
        put(ws, r, 5, x.get("h", 0), fmt="#,##0", fill=YELLOW, align="center")
        put(ws, r, 6, x.get("nrvbs", 0), fmt="0", fill=YELLOW, align="center")
        put(ws, r, 7, _cn(x.get("ivdc")), fill=YELLOW, align="center")
        ws.merge_cells(start_row=r, start_column=8, end_row=r, end_column=9)
        put(ws, r, 8, "IVDC：VB = ivdc_mem_line × W × 1.5" if x.get("ivdc") else "VB = nrVBs × W × H × 1.5")
        ws.cell(row=r, column=9).border = BORDER
        ws.row_dimensions[r].height = 22

    section(ws, enc_sec, "四、编码通道（VENC 码率与 SDK ring 码率分开填）", 9)
    header(ws, enc_hdr, ["启用", "名称", "Sensor", "类型", "宽 W", "高 H", "VENC kbps", "ring kbps", "流缓存 ring", "备注"])
    for i, r in enumerate(enc_rows):
        x = spec["encoders"][i]
        put(ws, r, 1, _cn(x.get("enabled")), fill=YELLOW, align="center")
        put(ws, r, 2, x.get("name", ""), bold=True)
        put(ws, r, 3, int(x.get("sensor", 0)), fmt="0", fill=YELLOW, align="center")
        put(ws, r, 4, x.get("type", "H265"), fill=YELLOW, align="center")
        put(ws, r, 5, x.get("w", 0), fmt="#,##0", fill=YELLOW, align="center")
        put(ws, r, 6, x.get("h", 0), fmt="#,##0", fill=YELLOW, align="center")
        put(ws, r, 7, x.get("bitrate", 0), fmt="#,##0", fill=YELLOW, align="center")
        put(ws, r, 8, x.get("ring_bitrate", x.get("bitrate", 0)), fmt="#,##0", fill=YELLOW, align="center")
        put(ws, r, 9, _cn(x.get("ring")), fill=YELLOW, align="center")
        default = "JPEG 按需建拆则稳态不计" if str(x.get("type", "")).upper() == "JPEG" else ""
        put(ws, r, 10, x.get("note", default))
        ws.row_dimensions[r].height = 22

    dv = DataValidation(type="list", formula1='"是,否"', allow_blank=False)
    ws.add_data_validation(dv)
    for rng in (f"A{fs_rows[0]}:A{fs_rows[-1]}", f"G{fs_rows[0]}:G{fs_rows[-1]}",
                f"A{enc_rows[0]}:A{enc_rows[-1]}", f"I{enc_rows[0]}:I{enc_rows[-1]}"):
        dv.add(rng)
    dv2 = DataValidation(type="list", formula1='"H265,H264,JPEG"', allow_blank=False)
    ws.add_data_validation(dv2)
    dv2.add(f"D{enc_rows[0]}:D{enc_rows[-1]}")

    for name, ref in {
        "BUF_SEC": "$B$6", "P2P_CLIENTS": "$B$7", "P2P_VID_KB": "$B$8", "P2P_AUD_KB": "$B$9",
        "P2P_AUD_BUFS": "$B$10", "P2P_VID_BUFS": "$B$11", "IVDC_LINE": "$B$12",
        "K_ISP": "$B$13", "K_RECON": "$B$14", "K_STREAM": "$B$15", "MAXI_FACTOR": "$B$16",
        "K_JPEG": "$B$17", "JPEG_OVERHEAD": "$B$18", "AUDIO_KBPS": "$B$19", "AUDIO_CH": "$B$20",
        "SDK_RESERVED": "$B$21", "DRAM_MB": "$B$23", "BYTES_PER_MB": "$B$24",
        "H264_FACTOR": "$B$25",
        "POOL_A": "$B$42", "POOL_B": "$B$43",
        "MTP_ENABLED": "$B$29", "MTP_SESSIONS": "$B$30", "MTP_FIXED_KB": "$B$31",
        "MTP_WND_RCV": "$B$32", "MTP_WND_SND": "$B$33", "MTP_MTU": "$B$34",
        "MTP_OVERHEAD_KB": "$B$35", "MTP_MEASURED": "$B$36",
        "WEBRTC_ENABLED": "$B$37", "WEBRTC_SESSIONS": "$B$38", "WEBRTC_FIXED_KB": "$B$39",
        "WEBRTC_PER_SESSION_KB": "$B$40", "WEBRTC_MEASURED": "$B$41",
    }.items():
        wb.defined_names.add(DefinedName(name, attr_text=f"'输入参数'!{ref}"))

    # ============ 内存估算 ============
    ws = wb.create_sheet("内存估算")
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDE", [30, 62, 16, 12, 32]):
        ws.column_dimensions[col].width = w
    title(ws, "内存估算结果（公式自动计算）", "E")
    ws.merge_cells("A2:E2")
    put(ws, 2, 1, "rmem 与 Linux 是两本独立账；rmem 超出不能拿 Linux 余量补。", size=9)

    section(ws, 3, "一、媒体保留区 rmem（分辨率主导，MDNS 按 sensor 分组）", 5)
    header(ws, 4, ["项目", "计算依据", "字节", "MB", "备注"])
    r = 5

    sensors = list(result["rmem"]["mdns_by_sensor"].keys())
    for s in sensors:
        terms = ",".join(
            f"IF(AND('输入参数'!$A${x}=\"是\",'输入参数'!$C${x}={s}),"
            f"'输入参数'!$D${x}*'输入参数'!$E${x}*1.5,0)"
            for x in fs_rows
        )
        put(ws, r, 1, f"ISP MDNS · Sensor {s}", bold=True)
        put(ws, r, 2, "k_isp × 该 sensor 的最大帧字节")
        put(ws, r, 3, f"=K_ISP*MAX({terms})", fmt="#,##0", fill=GREY)
        put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 5, "每 sensor 一份，不是全机一份")
        r += 1

    fs_vb_rows = []
    for i, x in enumerate(fs_rows):
        fs_vb_rows.append(r)
        name = spec["frame_sources"][i].get("name", f"FS{i}")
        put(ws, r, 1, f"{name} VB", bold=True)
        put(ws, r, 2, "IVDC：ivdc_mem_line×W×1.5；否则 nrVBs×W×H×1.5")
        put(ws, r, 3, f"=IF('输入参数'!$A${x}=\"否\",0,IF('输入参数'!$G${x}=\"是\","
                      f"IVDC_LINE*'输入参数'!$D${x}*1.5,"
                      f"'输入参数'!$F${x}*'输入参数'!$D${x}*'输入参数'!$E${x}*1.5))",
            fmt="#,##0", fill=GREY)
        put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 5, f"Sensor {spec['frame_sources'][i].get('sensor', 0)}")
        r += 1

    for i, x in enumerate(enc_rows):
        put(ws, r, 1, f"{spec['encoders'][i].get('name', f'enc{i}')} VENC pool", bold=True)
        put(ws, r, 2, "JPEG：k_jpeg×F+overhead；否则 pool_a×F+pool_b（码率无关），pool_a=0 时回退 k_recon/k_stream")
        put(ws, r, 3, f"=IF('输入参数'!$A${x}=\"否\",0,IF('输入参数'!$D${x}=\"JPEG\","
                      f"K_JPEG*'输入参数'!$E${x}*'输入参数'!$F${x}*1.5+JPEG_OVERHEAD,"
                      f"IF(POOL_A>0,"
                      f"(POOL_A*'输入参数'!$E${x}*'输入参数'!$F${x}*1.5+POOL_B)*"
                      f"IF('输入参数'!$D${x}=\"H264\",H264_FACTOR,1),"
                      f"(K_RECON*'输入参数'!$E${x}*'输入参数'!$F${x}*1.5+"
                      f"K_STREAM*('输入参数'!$G${x}*MAXI_FACTOR*1000/8))*"
                      f"IF('输入参数'!$D${x}=\"H264\",H264_FACTOR,1))))",
            fmt="#,##0", fill=GREY)
        put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 5, f"Sensor {spec['encoders'][i].get('sensor', 0)}")
        r += 1

    put(ws, r, 1, "SDK 预留块", bold=True)
    put(ws, r, 2, "2 × 512,000 B（reuse=FREE）")
    put(ws, r, 3, "=SDK_RESERVED", fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
    r += 1

    rmem_first = 5
    rmem_subtotal = r
    put(ws, r, 1, "rmem 估算合计", bold=True)
    put(ws, r, 2, f"SUM(C{rmem_first}:C{r-1})")
    put(ws, r, 3, f"=SUM(C{rmem_first}:C{r-1})", fmt="#,##0", fill=GREEN, bold=True)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREEN, bold=True)
    put(ws, r, 5, "")
    suggest_row = r + 1
    put(ws, suggest_row, 1, "建议 rmem carve-out", bold=True)
    put(ws, suggest_row, 2, "rmem 需求向上取整到 1MB；直接写 bootargs")
    put(ws, suggest_row, 3, f"=CEILING(C{rmem_subtotal}/BYTES_PER_MB,1)", fmt="0.00", fill=GREEN)
    put(ws, suggest_row, 4, "MB", align="center")
    put(ws, suggest_row, 5, "rmem 由管线计算，不做输入")
    r = suggest_row + 2
    section(ws, r, "二、Linux 侧动态内存（码率 / 连接数主导）", 5); r += 1
    header(ws, r, ["项目", "计算依据", "字节", "MB", "备注"]); r += 1
    linux_first = r
    for i, x in enumerate(enc_rows):
        put(ws, r, 1, f"{spec['encoders'][i].get('name', f'enc{i}')} 流缓存 ring", bold=True)
        put(ws, r, 2, "码率 × 128 × 缓存秒数（启用且勾 ring、非 JPEG）")
        put(ws, r, 3, f"=IF(AND('输入参数'!$A${x}=\"是\",'输入参数'!$I${x}=\"是\","
                      f"'输入参数'!$D${x}<>\"JPEG\"),'输入参数'!$H${x}*128*BUF_SEC,0)",
            fmt="#,##0", fill=GREY)
        put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 5, "SDK 堆，开机一次性 calloc")
        r += 1
    put(ws, r, 1, "音频流缓存 ring", bold=True)
    put(ws, r, 2, "音频码率 × 128 × 缓存秒数 × 通道数")
    put(ws, r, 3, "=AUDIO_KBPS*128*BUF_SEC*AUDIO_CH", fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
    r += 1
    put(ws, r, 1, "P2P 收发缓冲", bold=True)
    put(ws, r, 2, "客户端 ×（视频块×块数 + 音频块×块数×2）")
    put(ws, r, 3, "=P2P_CLIENTS*(P2P_VID_KB*1024*P2P_VID_BUFS+P2P_AUD_KB*1024*P2P_AUD_BUFS*2)",
        fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
    put(ws, r, 5, "块数为估计值，需实测")
    r += 1
    put(ws, r, 1, "MTP SDK（可选）", bold=True)
    put(ws, r, 2, "固定 + sessions × ((wnd_rcv+wnd_snd)×MTU + 每会话开销)")
    put(ws, r, 3, "=IF(MTP_ENABLED=\"否\",0,IF(MTP_MEASURED>0,MTP_MEASURED,"
                  "MTP_FIXED_KB*1024+MTP_SESSIONS*((MTP_WND_RCV+MTP_WND_SND)*MTP_MTU+MTP_OVERHEAD_KB*1024)))",
        fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
    put(ws, r, 5, "KCP 窗口估算；有实测填 B36")
    r += 1
    put(ws, r, 1, "海外语控 SDK（WebRTC）", bold=True)
    put(ws, r, 2, "固定 + 会话数 × 每会话开销")
    put(ws, r, 3, "=IF(WEBRTC_ENABLED=\"否\",0,IF(WEBRTC_MEASURED>0,WEBRTC_MEASURED,"
                  "WEBRTC_FIXED_KB*1024+WEBRTC_SESSIONS*WEBRTC_PER_SESSION_KB*1024))",
        fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
    put(ws, r, 5, "统称 WebRTC/Alexa/Google；有实测填 B41")
    r += 1
    net = spec.get("network", {})
    net_bytes = 0
    nt = str(net.get("type", "wifi")).lower()
    if nt in ("wifi", "wifi+4g"):
        net_bytes += net.get("wifi_supplicant_kb", 0) * 1024
    if nt in ("4g", "wifi+4g"):
        net_bytes += (net.get("lte_supplicant_kb", 0) + net.get("lte_module_kb", 0)) * 1024
    if net_bytes:
        put(ws, r, 1, "网络（WiFi/4G）", bold=True)
        put(ws, r, 2, "wpa_supplicant / LTE supplicant / 模块（spec 配置）")
        put(ws, r, 3, net_bytes, fmt="#,##0", fill=GREY)
        put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 5, "WiFi 机型填 wifi_supplicant_kb，未填按 0 计")
        r += 1
    bt = spec.get("features", {}).get("bluetooth", {})
    if bt.get("enabled"):
        put(ws, r, 1, "蓝牙用户态", bold=True)
        put(ws, r, 2, "miio_bt 等；内核 bluetooth.ko/aic_btusb 在 fixed.modules")
        put(ws, r, 3, bt.get("userspace_kb", 868) * 1024, fmt="#,##0", fill=GREY)
        put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 5, "<参考机> 实测 868KB")
        r += 1
    sl = spec.get("features", {}).get("slack", {})
    if float(sl.get("extra_threads", 0)) > 0:
        put(ws, r, 1, "新增线程栈", bold=True)
        put(ws, r, 2, "新增线程数 × 每线程栈（物理触底）")
        put(ws, r, 3, float(sl["extra_threads"]) * float(sl.get("thread_stack_kb", 64)) * 1024, fmt="#,##0", fill=GREY)
        put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 5, "常见配置 64KB/线程；基准线程已含在 app_base")
        r += 1
    linux_subtotal = r
    put(ws, r, 1, "Linux 动态合计", bold=True)
    put(ws, r, 2, f"SUM(C{linux_first}:C{r-1})")
    put(ws, r, 3, f"=SUM(C{linux_first}:C{r-1})", fmt="#,##0", fill=GREEN, bold=True)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREEN, bold=True)

    r = linux_subtotal + 2
    section(ws, r, "三、固定 / 平台项（历史实测基线，可编辑）", 5); r += 1
    header(ws, r, ["项目", "说明", "字节", "MB", "口径"]); r += 1
    fixed_labels = [
        ("kernel", "内核代码+数据", "text/data/bss/init"),
        ("modules", "内核模块", "tx-isp + WiFi + 其它"),
        ("cma", "CMA / TNPU", "CONFIG_CMA_SIZE_MBYTES"),
        ("slab", "Slab（SUnreclaim）", "内核 slab 经验值；可回收部分不计"),
        ("app_base", "c_mi_ipc 基础 anon", "不含 ring/P2P 的堆与栈"),
        ("miio_client", "miio_client 进程", "独立 IoT 客户端 RSS"),
        ("miio_ot", "miio_ot 进程", "如有独立进程，按实测 RSS"),
        ("other_procs", "其他进程", "init/log/adbd/storage"),
        ("file_cache", "file cache", "squashfs 代码页，可回收"),
        ("zram", "zram 实际占用", "zsmalloc 压缩后"),
    ]
    fixed_first = r
    for key, label, note in fixed_labels:
        put(ws, r, 1, label, bold=True)
        put(ws, r, 2, note)
        put(ws, r, 3, spec["fixed"].get(key, 0), fmt="#,##0", fill=YELLOW)
        put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 5, "可编辑")
        r += 1
    fixed_subtotal = r
    put(ws, r, 1, "固定项合计", bold=True)
    put(ws, r, 2, f"SUM(C{fixed_first}:C{r-1})")
    put(ws, r, 3, f"=SUM(C{fixed_first}:C{r-1})", fmt="#,##0", fill=GREEN, bold=True)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREEN, bold=True)

    r = fixed_subtotal + 2
    section(ws, r, "四、汇总与预算判断", 5); r += 1
    header(ws, r, ["项目", "计算依据", "字节", "MB", "判定"]); r += 1
    summary_first = r
    put(ws, r, 1, "媒体 rmem 估算", bold=True); put(ws, r, 2, "rmem 小计")
    put(ws, r, 3, f"=C{rmem_subtotal}", fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY); r += 1
    put(ws, r, 1, "Linux 动态", bold=True); put(ws, r, 2, "Linux 动态小计")
    put(ws, r, 3, f"=C{linux_subtotal}", fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY); r += 1
    put(ws, r, 1, "Linux 固定项", bold=True); put(ws, r, 2, "固定项小计")
    put(ws, r, 3, f"=C{fixed_subtotal}", fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY); r += 1
    linux_total_row = r
    put(ws, r, 1, "Linux 合计", bold=True); put(ws, r, 2, "动态 + 固定")
    put(ws, r, 3, f"=C{r-2}+C{r-1}", fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY); r += 1
    visible_row = r
    put(ws, r, 1, "Linux 可见", bold=True); put(ws, r, 2, "总 DRAM − rmem 需求")
    put(ws, r, 3, f"=DRAM_MB*BYTES_PER_MB-C{rmem_subtotal}", fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY); r += 1
    linux_margin_row = r
    put(ws, r, 1, "Linux 余量", bold=True); put(ws, r, 2, "可见 − 合计")
    put(ws, r, 3, f"=C{visible_row}-C{linux_total_row}", fmt="#,##0", fill=GREY)
    put(ws, r, 4, f"=C{r}/BYTES_PER_MB", fmt="0.00", fill=GREY); r += 1
    put(ws, r, 1, "判定 · Linux", bold=True); put(ws, r, 2, "Linux 合计 ≤ 可见？")
    put(ws, r, 3, f"=IF(C{linux_total_row}<=C{visible_row},\"OK\",\"超支\")", fill=GREY); r += 1
    put(ws, r, 1, "结论（Linux）", bold=True)
    put(ws, r, 2, f"=IF(C{linux_total_row}<=C{visible_row},"
                  f"\"✅ Linux 预算可行；最终预留见『系统汇总』\","
                  f"\"⚠️ Linux 超支 \"&TEXT((C{linux_total_row}-C{visible_row})/BYTES_PER_MB,\"0.00\")&\" MB\")", bold=True)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
    ws.row_dimensions[r].height = 28

    # ============ 标定校验 ============
    ws = wb.create_sheet("标定校验")
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDEF", [8, 34, 16, 42, 18, 12]):
        ws.column_dimensions[col].width = w
    title(ws, "实测标定与校验", "F")
    ws.merge_cells("A2:F2")
    put(ws, 2, 1, f"平台：{spec.get('platform', 'T32Pro')}；默认基线 = <项目B> 2026-11；<项目A> 2026-06 为历史参考。项目 {spec['project']} 请按第三节重标。", size=9)
    section(ws, 4, "一、已采集数据点（<项目A>，2026-06 ~ 2026-10）", 6)
    header(ws, 5, ["#", "数据点", "实测字节", "来源", "模型预测字节", "误差"])
    points = [
        ("ISP MDNS @4K", 10436608, "rmem_optimization_analysis.md:16", "=K_ISP*3840*2160*1.5"),
        ("FS0 IVDC VB", 1152000, "rmem_optimization_analysis.md:21", "=IVDC_LINE*3840*1.5"),
        ("FS1 VB (nrVBs=2)", 1198080, "rmem_optimization_analysis.md:22", "=2*832*480*1.5"),
        ("VENC pool 4K H265", 14312448, "rmem_optimization_analysis.md:19",
         "=K_RECON*3840*2160*1.5+K_STREAM*(2000*MAXI_FACTOR*1000/8)"),
        ("VENC pool 832×480 H265", 789248, "rmem_optimization_analysis.md:20",
         "=K_RECON*832*480*1.5+K_STREAM*(350*MAXI_FACTOR*1000/8)"),
        ("JPEG pool 832×480", 607360, "20261016-jpeg-snap-ondemand.md",
         "=K_JPEG*832*480*1.5+JPEG_OVERHEAD"),
        ("SDK ring 主码流 2000kbps×6s", 1536000, "c-mi-ipc-memory-profile.md §八", "=2000*128*6"),
        ("SDK ring 音频 100kbps×6s", 76800, "c-mi-ipc-memory-profile.md §八", "=100*128*6"),
        ("SDK 预留块 2×512KB", 1024000, "rmem_optimization_analysis.md:17-18", "=SDK_RESERVED"),
    ]
    for i, (name, meas, src, pred) in enumerate(points):
        row = 6 + i
        put(ws, row, 1, i + 1, align="center")
        put(ws, row, 2, name, bold=True)
        put(ws, row, 3, meas, fmt="#,##0", fill=YELLOW)
        put(ws, row, 4, src, size=8)
        put(ws, row, 5, pred, fmt="#,##0", fill=GREY)
        put(ws, row, 6, f"=(E{row}-C{row})/C{row}", fmt="0.000%", fill=GREY)
        ws.row_dimensions[row].height = 22

    section(ws, 16, "二、历史双点拟合 k_recon / k_stream（已废弃，仅供对照；现行用 pool_a/pool_b）", 6)
    header(ws, 17, ["", "参数", "值", "说明"])
    fit_rows = [
        (18, "Pm 主码流实测 pool", 14312448, "4K H265 编码器 pool"),
        (19, "Fm 主码流单帧", "=3840*2160*1.5", "12,441,600 B"),
        (20, "Im 主码流 maxI 字节", "=2000*MAXI_FACTOR*1000/8", "625,000 B"),
        (21, "Ps 次码流实测 pool", 789248, "832×480 H265 编码器 pool"),
        (22, "Fs 次码流单帧", "=832*480*1.5", "599,040 B"),
        (23, "Is 次码流 maxI 字节", "=350*MAXI_FACTOR*1000/8", "109,375 B"),
        (24, "k_recon 拟合值", "=(C18*C23-C21*C20)/(C19*C23-C22*C20)", "F 的系数"),
        (25, "k_stream 拟合值", "=(C19*C21-C22*C18)/(C19*C23-C22*C20)", "maxI 的系数"),
        (26, "当前使用 k_recon", "=K_RECON", "输入参数 B14"),
        (27, "当前使用 k_stream", "=K_STREAM", "输入参数 B15"),
    ]
    for row, name, val, note in fit_rows:
        put(ws, row, 2, name, bold=True)
        fmt = "0.0000" if ("k_" in name) else "#,##0"
        put(ws, row, 3, val, fmt=fmt, fill=GREY, align="center")
        ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=6)
        put(ws, row, 4, note)
        for cc in (5, 6):
            ws.cell(row=row, column=cc).border = BORDER
    put(ws, 29, 2, "拟合校验", bold=True)
    put(ws, 29, 3, "=IF(AND(ABS(C24-C26)<0.001,ABS(C25-C27)<0.001),"
                  "\"✅ 当前系数与拟合一致\",\"⚠ 系数不同：把 C24/C25 回填『输入参数』B14/B15\")",
        fill=GREEN)
    ws.merge_cells("C29:F29")

    section(ws, 31, "三、实机采集命令（新构建 / 新项目重标时用）", 6)
    cmds = [
        "① 确认 rmem：cat /proc/cmdline",
        "② 媒体拓扑：impdbg --fs_info / --enc_info（enc_info 第二个 GROUP 在 25 行之后）",
        "③ rmem 明细：cat /tmp/continuous_mem_info（SDK continuous_alloc dump）",
        "④ Linux：cat /proc/meminfo ; cat /proc/buddyinfo",
        "⑤ app：P=$(ps | awk '/[c]_mi_ipc/{print $1; exit}'); awk '/^Rss:/{R+=$2}/^Anonymous:/{A+=$2}/^Swap:/{W+=$2} END{printf \"RSS=%d anon=%d file=%d swap=%d kB\\n\",R,A,R-A,W}' /proc/$P/smaps",
        "⑥ 写 SD 卡再取，不要写 /tmp；串口不要 cat 大文件。",
    ]
    for i, s in enumerate(cmds):
        row = 32 + i
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        put(ws, row, 1, s, size=9)
        for cc in range(2, 7):
            ws.cell(row=row, column=cc).border = BORDER
        ws.row_dimensions[row].height = 24

    section(ws, 40, "四、<项目B> 实测（2026-11-09，rmem 24,064K / isp_memopt=0x7）", 6)
    header(ws, 41, ["#", "数据点", "实测字节", "来源", "模型预测", "误差"])
    hm_points = [
        ("ISP MDNS @4K", 9433088, "continuous_mem_info", "=K_ISP*3840*2160*1.5"),
        ("FS0 IVDC VB", 1152000, "continuous_mem_info", "=IVDC_LINE*3840*1.5"),
        ("FS1 VB (nrVBs=2)", 1198080, "continuous_mem_info", "=2*832*480*1.5"),
        ("VENC main 4K/1500", 10579968, "continuous_mem_info",
         "=K_RECON*3840*2160*1.5+K_STREAM*(1500*MAXI_FACTOR*1000/8)"),
        ("VENC sub 480P/350", 1115136, "continuous_mem_info",
         "=K_RECON*832*480*1.5+K_STREAM*(350*MAXI_FACTOR*1000/8)"),
        ("JPEG pool 832×480", 609536, "continuous_mem_info", "=K_JPEG*832*480*1.5+JPEG_OVERHEAD"),
        ("SDK small blocks", 113152, "continuous_mem_info", "=SDK_RESERVED"),
        ("rmem 合计", 24200960, "continuous_mem_info", "=SUM(E42:E48)"),
    ]
    for i, (name, meas, src, pred) in enumerate(hm_points):
        row = 42 + i
        put(ws, row, 1, i + 1, align="center")
        put(ws, row, 2, name, bold=True)
        put(ws, row, 3, meas, fmt="#,##0", fill=YELLOW)
        put(ws, row, 4, src, size=8)
        put(ws, row, 5, pred, fmt="#,##0", fill=GREY)
        put(ws, row, 6, f"=(E{row}-C{row})/C{row}", fmt="0.000%", fill=GREY)
        ws.row_dimensions[row].height = 22

    section(ws, 51, "五、<项目B> 双点拟合（4K 1500kbps + 480P 350kbps）", 6)
    header(ws, 52, ["", "参数", "值", "说明"])
    hm_fit = [
        (53, "Pm 主码流实测 pool", 10579968, "4K H265/1500"),
        (54, "Fm 主码流单帧", "=3840*2160*1.5", "12,441,600 B"),
        (55, "Im 主码流 maxI 字节", "=1500*MAXI_FACTOR*1000/8", "468,750 B"),
        (56, "Ps 次码流实测 pool", 1115136, "832×480 H265/350"),
        (57, "Fs 次码流单帧", "=832*480*1.5", "599,040 B"),
        (58, "Is 次码流 maxI 字节", "=350*MAXI_FACTOR*1000/8", "109,375 B"),
        (59, "k_recon 拟合值", "=(C53*C58-C56*C55)/(C54*C58-C57*C55)", "F 的系数"),
        (60, "k_stream 拟合值", "=(C54*C56-C57*C53)/(C54*C58-C57*C55)", "maxI 的系数"),
        (61, "当前使用 k_recon", "=K_RECON", "输入参数 B14"),
        (62, "当前使用 k_stream", "=K_STREAM", "输入参数 B15"),
    ]
    for row, name, val, note in hm_fit:
        put(ws, row, 2, name, bold=True)
        fmt = "0.0000" if ("k_" in name) else "#,##0"
        put(ws, row, 3, val, fmt=fmt, fill=GREY, align="center")
        ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=6)
        put(ws, row, 4, note)
        for cc in (5, 6):
            ws.cell(row=row, column=cc).border = BORDER
    put(ws, 64, 2, "拟合校验", bold=True)
    put(ws, 64, 3, "=IF(AND(ABS(C59-C61)<0.001,ABS(C60-C62)<0.001),"
                   "\"✅ <项目B> 系数一致\",\"⚠ 当前 spec 未用 <项目B> 系数，误差大属正常\")",
        fill=GREEN)
    ws.merge_cells("C64:F64")

    # ============ 速查表 ============
    ws = wb.create_sheet("速查表")
    ws.sheet_view.showGridLines = False
    widths = [18, 9, 9, 12, 8, 8, 11, 11, 11, 12, 12, 14, 14, 14]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    title(ws, "分辨率 × 码率 速查（单视频通道 + 单帧源）", "N")
    ws.merge_cells("A2:N2")
    put(ws, 2, 1, "不含固定项；rmem = VENC + FS VB + MDNS + SDK 预留；Linux = ring + P2P。"
                  "双摄请对每个 sensor 各算一行后再相加（MDNS 分开）；速查表不含 MTP。", size=9)
    header(ws, 4, ["分辨率", "W", "H", "码率 kbps", "帧率", "IVDC", "单帧 KB", "VENC MB",
                   "FS VB MB", "ISP MDNS MB", "SDK ring MB", "rmem 合计 MB", "Linux 动态 MB", "合计动态 MB"])
    presets = [
        ("640×360", 640, 360, 512, 15, "否"), ("1280×720", 1280, 720, 1024, 20, "否"),
        ("1920×1080", 1920, 1080, 2048, 20, "否"), ("1920×1080", 1920, 1080, 4096, 20, "否"),
        ("2560×1440", 2560, 1440, 4096, 20, "否"), ("2880×1620", 2880, 1620, 2000, 20, "否"),
        ("2880×1620", 2880, 1620, 4096, 20, "否"), ("3840×2160", 3840, 2160, 2000, 20, "是"),
        ("3840×2160", 3840, 2160, 4096, 20, "是"), ("3840×2160", 3840, 2160, 6000, 20, "是"),
        ("3840×2160", 3840, 2160, 2000, 15, "是"), ("3840×2160", 3840, 2160, 8000, 25, "是"),
    ]
    for i, (label, w, h, br, fps, ivdc) in enumerate(presets):
        row = 5 + i
        put(ws, row, 1, label, bold=True, fill=YELLOW)
        put(ws, row, 2, w, fmt="#,##0", fill=YELLOW, align="center")
        put(ws, row, 3, h, fmt="#,##0", fill=YELLOW, align="center")
        put(ws, row, 4, br, fmt="#,##0", fill=YELLOW, align="center")
        put(ws, row, 5, fps, fmt="0", fill=YELLOW, align="center")
        put(ws, row, 6, ivdc, fill=YELLOW, align="center")
        put(ws, row, 7, f"=B{row}*C{row}*1.5/1024", fmt="#,##0", fill=GREY)
        put(ws, row, 8, f"=(K_RECON*B{row}*C{row}*1.5+K_STREAM*(D{row}*MAXI_FACTOR*1000/8))/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, row, 9, f"=IF(F{row}=\"是\",IVDC_LINE*B{row}*1.5,2*B{row}*C{row}*1.5)/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, row, 10, f"=K_ISP*B{row}*C{row}*1.5/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, row, 11, f"=D{row}*128*BUF_SEC/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, row, 12, f"=(K_RECON*B{row}*C{row}*1.5+K_STREAM*(D{row}*MAXI_FACTOR*1000/8)+"
                         f"IF(F{row}=\"是\",IVDC_LINE*B{row}*1.5,2*B{row}*C{row}*1.5)+"
                         f"K_ISP*B{row}*C{row}*1.5+SDK_RESERVED)/BYTES_PER_MB", fmt="0.00", fill=GREEN)
        put(ws, row, 13, f"=(D{row}*128*BUF_SEC+P2P_CLIENTS*(P2P_VID_KB*1024*P2P_VID_BUFS+"
                         f"P2P_AUD_KB*1024*P2P_AUD_BUFS*2))/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, row, 14, f"=L{row}+M{row}", fmt="0.00", fill=GREEN)
        ws.row_dimensions[row].height = 22
    ws.merge_cells("A18:N19")
    put(ws, 18, 1, "注：默认系数来自 <项目B> 2026-11 实测；<项目A> 2026-06 的 28MB 场景为历史参考。"
                   "其他项目请先按『标定校验』重新拟合。双摄的 MDNS 要按 sensor 各算一次。",
        size=9, fill=RED)
    ws.row_dimensions[18].height = 30

    # ============ 系统汇总 ============
    ws = wb.create_sheet("系统汇总")
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCD", [30, 18, 12, 62]):
        ws.column_dimensions[col].width = w
    title(ws, f"{spec['project']} 系统内存汇总", "D")
    sy = result["system"]
    lin = result["linux"]
    ap = sy["apps"]
    tp = sy["top"]
    rmem = result["rmem"]
    venc_all = sum(x["bytes"] for x in rmem["venc"])
    jpeg_bytes = sum(x["bytes"] for x in rmem["venc"] if x.get("type") == "JPEG")
    section(ws, 3, "一、五项内存占用与最终预留", 4)
    header(ws, 4, ["项目", "字节", "MB", "说明"])
    put(ws, 2, 1, "本表为脚本生成的快照；改『输入参数』后请重跑脚本更新。", size=9)
    ws.merge_cells("A2:D2")
    rows = [
        ("DRAM 总量", sy["dram"], "硬件"),
        ("1. RMEM", tp["rmem"], "媒体 carve-out，自动计算"),
        ("  · ISP MDNS", rmem["mdns"], "按 sensor 分组"),
        ("  · FS VB", rmem["fs_vb"], "IVDC 行缓存 / nrVBs×帧"),
        ("  · VENC（不含 JPEG）", venc_all - jpeg_bytes, "H265/H264 编码器 pool"),
        ("  · JPEG", jpeg_bytes, "抓拍 pool"),
        ("  · SDK 预留", rmem["sdk_reserved"], "continuous_alloc 小块"),
        ("2. Linux 系统", tp["linux_system"], "kernel/modules/CMA(非算法)/cache/zram"),
        ("  · kernel + modules", tp["kernel"] + tp["modules"], "内核代码/数据/模块"),
        ("  · CMA（非算法）", tp["platform_cma"], "总 CMA − NPU/算法"),
        ("  · Slab", tp.get("slab", 0), "SUnreclaim 经验值；可回收部分不计"),
        ("  · file cache", tp["file_cache"], "squashfs 代码页，可回收"),
        ("  · zram", tp["zram"], "zsmalloc 压缩后"),
        ("3. Linux 应用", tp["linux_apps"], "MIKE SDK + miio_client/ot"),
        ("  · MIKE SDK（c_mi_ipc）", ap["mike_total"], "基础 anon + 动态 SDK"),
        ("    · 流缓存 ring", lin["rings"], "码率×128×缓存秒数"),
        ("    · 音频 ring", lin["audio_ring"], "音频码率×128×缓存秒数"),
        ("    · P2P", lin["p2p"], "客户端×(视频块+音频块×2)"),
        ("    · MTP", lin.get("mtp", 0), "KCP 窗口估算/实测"),
        ("    · 海外语控 SDK", lin.get("webrtc", 0), "统称 WebRTC/Alexa/Google"),
        ("    · 基础 anon", ap["mike_base"], "不含 ring/P2P 的堆与栈"),
        ("  · miio_client / miio_ot", ap["miio_total"], "独立 IoT 客户端 RSS"),
        ("4. 算法", tp["algorithm"], "NPU 池 + 算法模型"),
        ("  · NPU 池", sy["npu"], "TNPU/CMA 池"),
        ("  · 算法模型", sy["algo"], "算法工作内存预留"),
        ("5. 其他", tp["other"], "other_procs + 网络（WiFi/4G）"),
        ("  · other_procs", ap["other_procs"], "adbd/storage/log 等"),
        ("  · 网络（WiFi/4G）", ap["network"], "wpa_supplicant / LTE supplicant"),
        ("  · 蓝牙", ap.get("bluetooth", 0), "miio_bt 用户态（模块在 OS 层）"),
        ("五项合计", tp["total"], "RMEM + Linux系统 + Linux应用 + 算法 + 其他"),
        ("最终预留（账面）", tp["final_reserve"], "DRAM − 五项合计"),
        ("硬占用", sy.get("hard_total", tp["total"]), "五项合计 − file_cache（可回收）"),
        ("硬余量", sy.get("hard_reserve", tp["final_reserve"]), "DRAM − 硬占用；判定看这一行"),
        ("按 bootargs rmem 的运行时余量", sy.get("runtime_reserve", tp["final_reserve"]),
         "最终预留 − max(0, bootargs rmem − rmem 需求)"),
    ]
    r = 5
    idx = {}
    for name, val, note in rows:
        idx[name] = r
        put(ws, r, 1, name, bold=True)
        put(ws, r, 2, val, fmt="#,##0", fill=GREY)
        put(ws, r, 3, f"=B{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 4, note)
        ws.row_dimensions[r].height = 22
        r += 1
    verdict_row = r + 1
    put(ws, verdict_row, 1, "判定", bold=True)
    put(ws, verdict_row, 2,
        f"=IF(B{idx['按 bootargs rmem 的运行时余量']}>=0,\"✅ 预算可行\",\"⚠️ 预算超支\")", fill=GREEN)
    ws.merge_cells(start_row=verdict_row, start_column=2, end_row=verdict_row, end_column=4)
    put(ws, verdict_row + 2, 1, "口径：五项合计 = RMEM + Linux系统 + Linux应用 + 算法 + 其他；最终预留 = DRAM − 五项合计。NPU/算法含 CMA 时从 Linux 系统 CMA 中扣除，避免双算。", size=9)
    ws.merge_cells(start_row=verdict_row + 2, start_column=1, end_row=verdict_row + 2, end_column=4)
    ws.cell(row=verdict_row + 2, column=1).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[verdict_row + 2].height = 30

    for name in wb.sheetnames:
        s = wb[name]
        s.page_setup.orientation = "landscape"
        s.page_setup.fitToWidth = 1
        s.page_setup.fitToHeight = 0
        s.sheet_properties.pageSetUpPr.fitToPage = True
        s.print_options.horizontalCentered = True
        s.page_margins.left = s.page_margins.right = 0.3
        s.page_margins.top = s.page_margins.bottom = 0.4
    wb["输入参数"].freeze_panes = "A6"
    wb["内存估算"].freeze_panes = "A5"
    wb["标定校验"].freeze_panes = "A6"
    wb["速查表"].freeze_panes = "A5"

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="membudget_")
    raw = os.path.join(tmpdir, os.path.basename(out_path))
    wb.save(raw)
    return raw, tmpdir, {
        "rmem_total": f"'内存估算'!C{rmem_subtotal}",
        "linux_dyn": f"'内存估算'!C{linux_subtotal}",
        "fixed": f"'内存估算'!C{fixed_subtotal}",
    }


def recalc(raw, out_path):
    lo = shutil.which("libreoffice") or shutil.which("soffice")
    if not lo:
        shutil.move(raw, out_path)
        return False
    if os.path.exists(out_path):
        os.remove(out_path)
    tmpout = tempfile.mkdtemp(prefix="membudget_out_")
    try:
        subprocess.run([lo, "--headless", "--calc", "--convert-to", "xlsx", "--outdir", tmpout, raw],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        produced = os.path.join(tmpout, os.path.basename(raw))
        if not os.path.exists(produced):
            raise SystemExit("LibreOffice 重算失败：未生成输出文件")
        shutil.move(produced, out_path)
    finally:
        shutil.rmtree(tmpout, ignore_errors=True)
    return True


def verify(out_path, result, layout):
    wb = load_workbook(out_path, data_only=True)
    checks = {
        "rmem": (wb["内存估算"]["C" + layout["rmem_total"].split("C")[-1]].value, result["rmem"]["total"]),
        "linux_dyn": (wb["内存估算"]["C" + layout["linux_dyn"].split("C")[-1]].value, result["linux"]["dynamic"] + result["system"]["network"] + result["system"].get("bluetooth", 0)),
        "fixed": (wb["内存估算"]["C" + layout["fixed"].split("C")[-1]].value, result["linux"]["fixed"]),
    }
    for name, (got, want) in checks.items():
        if got is None:
            raise SystemExit(f"校验失败：{name} 未算出缓存值（LibreOffice 重算异常）")
        if abs(float(got) - float(want)) > max(1.0, abs(want) * 0.005):
            raise SystemExit(f"校验失败：{name} Excel={got} Python={want:.0f}（公式与脚本模型漂移）")


def summary_text(r):
    def m(x):
        return x / MIB
    v = r["verdict"]
    rmem, linux = r["rmem"], r["linux"]
    mdns = " | ".join(f"S{s} {m(val):.2f}" for s, val in rmem["mdns_by_sensor"].items())
    lines = [
        f"项目: {r['project']} ({r['platform']})    rmem 需求: {m(rmem['need']):.2f} MB",
        f"rmem : MDNS {mdns} | FS VB {m(rmem['fs_vb']):.2f} | "
        f"VENC {m(sum(x['bytes'] for x in rmem['venc'])):.2f} | SDK {m(rmem['sdk_reserved']):.2f} "
        f"= {m(rmem['total']):.2f} MB",
        f"Linux: ring {m(linux['rings']):.2f} | audio {m(linux['audio_ring']):.2f} | "
        f"P2P {m(linux['p2p']):.2f} | MTP {m(linux.get('mtp', 0)):.2f} | "
        f"海外语控 {m(linux.get('webrtc', 0)):.2f} | 固定 {m(linux['fixed']):.2f} | "
        f"网络(WiFi/4G) {m(r.get('system', {}).get('network', 0)):.2f} | "
        f"蓝牙 {m(r.get('system', {}).get('bluetooth', 0)):.2f} "
        f"= {m(linux['total']):.2f} MB (可见 {m(linux['visible']):.2f})",
        f"判定 : Linux {'OK' if v['linux_ok'] else '超 ' + format(-linux['headroom_hard']/MIB, '.2f') + ' MB'}"
        f" ; Linux 硬余 {m(linux['headroom_hard']):.2f} MB（账面 {m(linux['headroom']):.2f}）/ 可见 {m(linux['visible']):.2f}"
        f" ; 账面预留 {m(r.get('system', {}).get('final_reserve', 0)):.2f} MB"
        f" ; 硬占用 {m(r.get('system', {}).get('hard_total', 0)):.2f} MB"
        f" ; 硬余量 {m(r.get('system', {}).get('hard_reserve', 0)):.2f} MB",
    ]
    sy = r.get("system")
    if sy:
        tp = sy.get("top", {})
        lines.append(
            f"五项 : RMEM {m(tp.get('rmem', 0)):.2f}"
            f" | Linux系统 {m(tp.get('linux_system', 0)):.2f}"
            f" | Linux应用 {m(tp.get('linux_apps', 0)):.2f}"
            f" | 算法 {m(tp.get('algorithm', 0)):.2f}"
            f" | 其他 {m(tp.get('other', 0)):.2f}"
            f" = {m(tp.get('total', 0)):.2f} MB"
        )
        lines.append(
            f"系统 : DRAM {m(sy['dram']):.0f} | rmem {m(sy['rmem_need']):.2f}"
            f" | Linux {m(sy['linux_total']):.2f}/{m(sy['linux_visible']):.2f}"
            f" | NPU+算法 {m(sy['npu_algo']):.2f}{'(含CMA)' if sy['inside_cma'] else ''}"
        )
        lines.append(f"预留 : 账面占用 {m(sy['system_used']):.2f} MB，账面预留 {m(sy['final_reserve']):.2f} MB"
                     f" ; 硬占用 {m(sy.get('hard_total', sy['system_used'])):.2f} MB，硬余量 {m(sy.get('hard_reserve', sy['final_reserve'])):.2f} MB")
        if sy.get("bootargs_rmem"):
            lines.append(f"bootargs rmem {m(sy['bootargs_rmem']):.2f} MB ⇒ 运行时余量 {m(sy['runtime_reserve']):.2f} MB")
    if r.get("warnings"):
        lines.append("注意 : " + "；".join(r["warnings"]))
    return "\n".join(lines)


def run(spec, out_path, do_xlsx=True):
    result = compute(spec)
    if not do_xlsx:
        return result, None
    raw, tmpdir, layout = build_workbook(spec, result, out_path)
    try:
        recalculated = recalc(raw, out_path)
        if recalculated:
            verify(out_path, result, layout)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return result, recalculated


def self_test():
    tmp = tempfile.mkdtemp(prefix="membudget_test_")
    try:
        out = os.path.join(tmp, "selftest.xlsx")
        result, recalculated = run(merge(DEFAULTS, {}), out)
        assert os.path.exists(out) and os.path.getsize(out) > 10000, "输出文件异常"
        rmem_mib = result["rmem"]["total"] / MIB
        assert 23.0 < rmem_mib < 26.0, f"单摄 rmem 基线异常: {rmem_mib:.2f} MiB"
        assert result["linux"]["dynamic"] > 0
        tp = result["system"]["top"]
        assert abs((tp["rmem"] + tp["linux_system"] + tp["linux_apps"] + tp["algorithm"] + tp["other"]) - tp["total"]) < 1, "五项合计不等于各项之和"
        # 双摄：同一套系数下 MDNS 应为单摄的 2 倍（两个同分辨率 sensor）
        dual = merge(DEFAULTS, {
            "project": "dual-test",
            "frame_sources": [
                {"enabled": True, "sensor": 0, "name": "FS0", "w": 2880, "h": 1620, "nrvbs": 1, "ivdc": True},
                {"enabled": True, "sensor": 1, "name": "FS3", "w": 2880, "h": 1620, "nrvbs": 1, "ivdc": True},
            ],
            "encoders": [
                {"enabled": True, "sensor": 0, "name": "enc0", "type": "H265", "w": 2880, "h": 1620, "bitrate": 1800, "ring": True},
                {"enabled": True, "sensor": 1, "name": "enc3", "type": "H265", "w": 2880, "h": 1620, "bitrate": 1800, "ring": True},
            ],
        })
        # <项目B> 实测精度验收：rmem / MIKE SDK / miio_client 偏差 ≤1MB
        hm_path = os.path.join(HERE, "..", "templates", "spec-4k-4g.json")
        if os.path.exists(hm_path):
            with open(hm_path, encoding="utf-8") as fh:
                hm = merge(DEFAULTS, json.load(fh))
            hr = compute(hm)
            assert abs(hr["rmem"]["total"] - 24200960) < MIB, "<项目B> rmem 偏差 >1MB"
            mike_anon = hr["linux"]["dynamic"] + float(hm["fixed"].get("app_base", 0))
            assert abs(mike_anon / 1024.0 - 6652) < 1024, "<项目B> MIKE SDK 偏差 >1MB"
            assert abs(hr["system"]["apps"]["miio_total"] - 1708032) < MIB, "miio_client 偏差 >1MB"

        # <项目C> 实测：五项合计/最终预留偏差 ≤1MB
        h6_path = os.path.join(HERE, "..", "templates", "spec-dual-wifi.json")
        if os.path.exists(h6_path):
            with open(h6_path, encoding="utf-8") as fh:
                h6 = merge(DEFAULTS, json.load(fh))
            r6 = compute(h6)["system"]["top"]
            assert abs(r6["total"] - (117077760 + h6["fixed"].get("slab", 0))) < MIB, "<项目C> 五项合计偏差 >1MB"
            assert abs(r6["final_reserve"] - (17139968 - h6["fixed"].get("slab", 0))) < MIB, "<项目C> 最终预留偏差 >1MB"

        dr = compute(dual)
        assert len(dr["rmem"]["mdns_by_sensor"]) == 2, "双摄 MDNS 未按 sensor 分组"
        assert dr["rmem"]["mdns"] > 2 * 5.0 * MIB, "双摄 MDNS 量级异常"
        print(f"SELFTEST PASS  single rmem={rmem_mib:.2f} MiB  dual mdns={dr['rmem']['mdns']/MIB:.2f} MiB  "
              f"out={os.path.getsize(out)}B  recalc={'yes' if recalculated else 'no-libreoffice'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="内存预算估算并生成 Excel（支持多摄）")
    ap.add_argument("--spec", help="规格 JSON 路径")
    ap.add_argument("--out", help="输出 xlsx 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    ap.add_argument("--quiet", action="store_true", help="不打印文本摘要")
    ap.add_argument("--no-xlsx", action="store_true", help="只计算并打印，不生成 Excel")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return
    if not args.spec:
        ap.print_usage()
        sys.exit(2)

    with open(args.spec, encoding="utf-8") as fh:
        spec = merge(DEFAULTS, json.load(fh))
    if args.no_xlsx:
        result = compute(spec)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif not args.quiet:
            print(summary_text(result))
        return

    out = args.out or f"memory_estimate_{spec['project']}.xlsx"
    result, recalculated = run(spec, out)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(summary_text(result))
        if not recalculated:
            print("⚠ 未找到 LibreOffice：Excel 公式已写入但无缓存值，用 Excel 打开会自动重算。")


if __name__ == "__main__":
    main()
