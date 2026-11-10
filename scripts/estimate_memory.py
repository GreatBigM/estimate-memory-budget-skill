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
from openpyxl.worksheet.pagebreak import Break

HERE = os.path.dirname(os.path.abspath(__file__))
MIB = 1024 * 1024
FIXED_KEYS = ("kernel", "modules", "cma", "slab", "app_base", "miio_client", "miio_ot", "other_procs", "file_cache", "zram")

DEFAULTS = {
    "project": "4k-wifi",
    "platform": "T32Pro",
    "network": {"type": "wifi", "wifi_supplicant_kb": 0, "lte_supplicant_kb": 0, "lte_module_kb": 0},
    "media": {"source": "formula", "cap_bytes": 0, "blocks": []},
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


def platform_family(spec):
    """君正 T32Pro 走 rmem 公式；SigmaStar 走 MMA 堆实测块；其它未知平台只给量级。"""
    p = str(spec.get("platform", "")).replace(" ", "").lower()
    if "sigma" in p or p in ("ssc303", "ssd303", "ihalo"):
        return "sigmastar"
    return "t32pro" if p == "t32pro" else "other"


def is_measured(spec):
    return (spec.get("media") or {}).get("source") == "measured"


def _warnings(spec):
    warns = []
    fam, measured = platform_family(spec), is_measured(spec)
    if measured:
        media = spec.get("media") or {}
        if fam != "sigmastar":
            warns.append(f"media.source=measured 但平台 {spec.get('platform')} 不是 SigmaStar：确认口径")
        if not media.get("blocks") or sum(float(b.get("bytes", 0)) for b in media["blocks"]) == 0:
            warns.append("media.blocks 为空或全为 0：媒体侧按 0 计，请填 mma_heap_name0 实测块")
        if not media.get("cap_bytes"):
            warns.append("未填 media.cap_bytes（媒体堆容量）：Linux 可见按 used 计，会低估 carve-out")
    elif fam != "t32pro":
        warns.append(f"平台 {spec.get('platform')} 非 T32Pro：系数按 T32Pro 标定，需重标；SigmaStar 请改用 media.source=measured")
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
        warns.append("WiFi 机型未填 network.wifi_supplicant_kb：wpa_supplicant 可能已含在 other_procs 快照，也可能漏计（4K 样机 实测 wpa 1,608KB + udhcpc 468KB）")
    if spec.get("features", {}).get("bluetooth", {}).get("enabled"):
        warns.append("蓝牙按 miio_bt 用户态 868KB 计（4K 样机 实测）；内核模块 bluetooth.ko + aic_btusb 约 240KB 应在 fixed.modules 基线内，若基线未含需自行加回")
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
    media = spec.get("media") or {}
    measured = is_measured(spec)
    blocks = media.get("blocks") or []
    fs_enabled = [x for x in spec["frame_sources"] if x.get("enabled")]

    if measured:
        # SigmaStar：媒体侧按 mma_heap_name0 实测块，不走 rmem 公式
        mdns_by_sensor, mdns, fs_vb = OrderedDict(), 0.0, 0.0
        venc_items, rings = [], 0.0
        media_algo = sum(float(b.get("bytes", 0)) for b in blocks if b.get("group") == "算法")
        rmem = sum(float(b.get("bytes", 0)) for b in blocks
                   if b.get("group") != "算法") + float(spec.get("sdk_reserved", 0) or 0)
    else:
        media_algo = 0.0
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
        rmem = mdns + fs_vb + sum(x["bytes"] for x in venc_items) + spec["sdk_reserved"]

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

    media_cap = float(media.get("cap_bytes", 0) or 0)
    linux_dyn = rings + audio_ring + p2p + mtp + webrtc
    linux_visible = spec["dram_mb"] * MIB - (media_cap if media_cap > 0 else rmem + media_algo)
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
    algorithm = npu_algo_bytes + media_algo   # SigmaStar 的 IPU 块在媒体堆内，划到算法块
    other = apps["other_total"]
    top_total = rmem + linux_system + linux_apps + algorithm + other
    system_used = top_total
    final_reserve = spec["dram_mb"] * MIB - top_total
    file_cache = float(spec["fixed"].get("file_cache", 0))
    linux_total_hard = linux_total_full - file_cache
    hard_total = top_total - file_cache
    hard_reserve = spec["dram_mb"] * MIB - hard_total

    return {
        "project": spec["project"],
        "platform": spec.get("platform", "T32Pro"),
        "warnings": _warnings(spec),
        "system": {
            "dram": spec["dram_mb"] * MIB,
            "rmem_need": rmem,
            "media_cap": media_cap,
            "media_used": rmem + media_algo,
            "measured": measured,
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
        },
        "rmem": {
            "mdns_by_sensor": mdns_by_sensor, "mdns": mdns, "fs_vb": fs_vb,
            "venc": venc_items, "sdk_reserved": spec["sdk_reserved"],
            "total": rmem, "need": rmem,
            "measured": measured, "blocks": blocks,
            "cap": media_cap if media_cap > 0 else rmem + media_algo,
            "usage": (rmem + media_algo) / media_cap if media_cap > 0 else 0.0,
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
    wb = Workbook()
    feat = spec.get("features", {})
    fx = spec["fixed"]
    net = spec.get("network", {})
    nt_raw = str(net.get("type", "wifi")).lower()
    nt_display = {"wifi": "WiFi", "4g": "4G", "wifi+4g": "WiFi+4G",
                  "none": "无（网络栈在对端 MCU/RTOS）"}.get(nt_raw, "WiFi")
    bt = feat.get("bluetooth", {})
    mtp = feat.get("mtp", {})
    web = feat.get("webrtc", {})
    na = feat.get("npu_algo", {})
    sl = feat.get("slack", {})
    media = spec.get("media") or {}
    measured = is_measured(spec)
    mblocks = media.get("blocks") or []

    # ==================== 说明 ====================
    ws = wb.active
    ws.title = "说明"
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGHI", [4, 24, 34, 14, 14, 14, 14, 14, 34]):
        ws.column_dimensions[col].width = w
    title(ws, f"{spec.get('platform', 'T32Pro')} / {spec['project']} 内存预估工具", "I")
    ws.merge_cells("B2:I2")
    put(ws, 2, 2, f"平台：{spec.get('platform', 'T32Pro')}；按五大块估算整机内存：RMEM / Linux 系统 / Linux 应用 / 算法 / 其他。", size=10)
    ws.row_dimensions[2].height = 30

    r = 3
    section(ws, r, "一、内存构成与逐项口径（五大块）", 9); r += 1
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=9)
    put(ws, r, 2, "F = 单帧 NV12 字节 = W × H × 1.5；1 MiB = 1,048,576 B。『经验值』的来源与历史取值见『速查表』附录，本项目实际取值与分项见『详细表』。", size=9)
    r += 1
    header(ws, r, ["", "块", "组成项", "公式 / 取值", "", "", "", "", "类型"])
    ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=8)
    for cc in range(5, 9):
        ws.cell(row=r, column=cc).border = BORDER
    r += 1
    if measured:
        media_items = [(b.get("name", "块"), f"实测（{b.get('group', '')}）：mma_heap_name0", "实测")
                       for b in mblocks if b.get("group") != "算法"]
        if spec.get("sdk_reserved"):
            media_items.append(("SDK 预留块", "spec.sdk_reserved", "常量"))
        media_items.append(("媒体堆容量 cap", "bootargs mma_heap=...,sz=；Linux 可见 = DRAM − cap", "配置值"))
    else:
        media_items = [
            ("ISP MDNS", "k_isp × max(F of sensor s)", "公式"),
            ("FS VB", "IVDC：ivdc_mem_line × W × 1.5；否则 nrVBs × W × H × 1.5", "公式"),
            ("VENC pool", "pool_a × F + pool_b；H.264 再 × h264_factor", "公式"),
            ("JPEG 抓拍", "k_jpeg × F + jpeg_overhead", "公式"),
            ("SDK 预留块", "2 × 512,000 B（reuse=FREE）", "常量"),
        ]
    comp = [
        ("1. RMEM", media_items),
        ("2. Linux 系统", [
            ("kernel", "历史实测基线（vmlinux 段复核）", "经验值"),
            ("modules", "历史实测（/proc/modules 汇总）", "经验值"),
            ("CMA（非算法）", "CONFIG_CMA_SIZE_MBYTES − NPU/算法", "配置值"),
            ("slab", "仅 SUnreclaim，历史实测", "经验值"),
            ("file cache", "squashfs 代码页，历史实测", "经验值"),
            ("zram", "zsmalloc 压缩后，历史实测", "经验值"),
        ]),
        ("3. Linux 应用", [
            ("流缓存 ring", "ring 码率(kbps) × 128 × 缓存秒数", "公式"),
            ("音频 ring", "音频码率(kbps) × 128 × 秒 × 通道数", "公式"),
            ("P2P", "clients × (视频块 × 块数 + 音频块 × 块数 × 2)", "公式＋经验块数"),
            ("MTP", "fixed + sessions × ((wnd_rcv+wnd_snd) × MTU + overhead)", "公式"),
            ("海外语控", "fixed + sessions × per_session（默认 1,024KB/会话）", "经验值"),
            ("c_mi_ipc 基础 anon", "历史实测（smaps 残差）", "经验值"),
            ("miio_client / miio_ot", "历史实测 RSS", "经验值"),
        ]),
        ("4. 算法", [
            ("NPU 池", "init.sh cma_pool_size（4K 系列 3,584KB；双摄-1440p 系列 5,120KB）", "配置值"),
            ("算法模型", "查 references/algo-models.md（常电人/车 1,229KB；双摄-1440p 系列已含池内）", "查表"),
        ]),
        ("5. 其他", [
            ("other_procs", "历史实测（init / log / adbd / storage）", "经验值"),
            ("网络", "WiFi：wpa_supplicant + udhcpc 实测 2,076KB；4G：supplicant 默认 512KB", "经验值"),
            ("蓝牙", "miio_bt 868KB（4K 样机 实测）；内核模块在 modules 基线", "经验值"),
        ]),
    ]
    for block, items in comp:
        b0 = r
        for i, (item, formula, kind) in enumerate(items):
            put(ws, r, 2, block if i == 0 else None, bold=True)
            put(ws, r, 3, item)
            ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=8)
            put(ws, r, 4, formula)
            for cc in range(5, 9):
                ws.cell(row=r, column=cc).border = BORDER
            put(ws, r, 9, kind, align="center")
            ws.row_dimensions[r].height = 28
            r += 1
        ws.merge_cells(start_row=b0, start_column=2, end_row=r - 1, end_column=2)
        ws.cell(row=b0, column=2).alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    r += 1
    section(ws, r, "二、两本账与判定口径", 9); r += 1
    rules = [
        "两本账不相加：rmem 需求先从 DRAM 扣掉，Linux 可见 = DRAM − rmem；rmem 不能拿 Linux 余量补。",
        "账面占用 = 五项合计；硬占用 = 五项合计 − file_cache（可回收）；判定看硬余量 = DRAM − 硬占用。",
        "默认最大场景：常驻组件全在线 + 全流 + P2P 满客户端 + 缓存秒数取最大 + JPEG 抓拍瞬时。",
        "单位统一 MiB（1,048,576 B）；对外说「MB」时按 1024 换算。",
    ]
    for s in rules:
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=9)
        put(ws, r, 2, s)
        ws.row_dimensions[r].height = 24
        r += 1

    r += 1
    section(ws, r, "三、数据来源", 9); r += 1
    sources = [
        ("流参数", "项目 config.h + video_hal_config.h 的 PROJECT_<project> 分支"),
        ("SDK ring", "qwiki 20261016-c-mi-ipc-memory-profile.md §八（反汇编）"),
        ("rmem 分项", "4K-4G 2026-11 continuous_mem_info（当前默认）；4K-WiFi 2026-06 28MB 场景为历史参考"),
        ("JPEG pool", "qwiki 20261016-jpeg-snap-ondemand.md（607,360 B）"),
        ("P2P 参数", "mike_p2p.c（音频 32KB；视频发送块按像素档）"),
        ("经验值基线", "『速查表』附录·历史实测基线（kernel/modules/slab/cache/zram/网络/蓝牙等）"),
    ]
    for k, v in sources:
        put(ws, r, 2, k, bold=True)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=9)
        put(ws, r, 3, v)
        for cc in range(4, 10):
            ws.cell(row=r, column=cc).border = BORDER
        r += 1

    r += 1
    section(ws, r, "四、使用步骤", 9); r += 1
    steps = [
        "1. 『输入详细』确认规格（黄色可改，备注列写明默认值来源）；FS/ENC 行数变化需重跑脚本。",
        "2. 『详细表』逐项核对五大块，Linux 应用块按 c_mi_ipc 与 miio 分开展开。",
        "3. 『汇总』看五项合计 / 账面预留 / 硬占用 / 硬余量 / 判定。",
        "4. 横比方案看『速查表』主表；经验值与历史实测基线看附录；新项目按实测点重标。",
    ]
    for s in steps:
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=9)
        put(ws, r, 2, s)
        r += 1

    r += 1
    section(ws, r, "五、口径与限制", 9); r += 1
    caveats = [
        "① 默认系数来自 4K-4G 2026-11 实测（T32Pro / isp_memopt=0x7）；4K-WiFi 2026-06 的 28MB 场景是历史参考。换项目仍须实机重标。",
        "② rmem 在 /proc/meminfo 不可见；用 impdbg 或 /tmp/continuous_mem_info 验证。",
        "③ 双摄/多摄：MDNS 按 sensor 分组，每组取该组最大帧；不要把单摄账直接乘 2，也不要把同组多个 FS 帧相加。",
        "④ 帧率 / GOP 不进内存公式（只影响 CPU 与码流波动）。",
        "⑤ Slab 只计 SUnreclaim；SReclaimable、Buffers、file-backed text 不计；zram 按压力上界计入硬账。",
        "⑥ MTP 是小米 MTP SDK（KCP），不是 USB MTP；重要项目用 smaps 实测覆盖公式值。",
    ]
    for s in caveats:
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=9)
        put(ws, r, 2, s)
        ws.row_dimensions[r].height = 30
        r += 1

    # ==================== 输入详细 ====================
    ws = wb.create_sheet("输入详细")
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGHIJ", [26, 14, 10, 10, 10, 10, 10, 10, 10, 28]):
        ws.column_dimensions[col].width = w
    title(ws, "输入详细（黄色单元格可编辑；备注列为默认值来源）", "J")
    ws.merge_cells("A2:J2")
    put(ws, 2, 1, "常用项在前，标定系数/常量集中在最后一节；改黄色格 → 『详细表』『汇总』自动重算。", size=9)

    inp = {}

    def add_param(row, key, name, value, unit, note, fmt=None):
        inp[key] = row
        put(ws, row, 1, name, bold=True)
        put(ws, row, 2, value, fmt=fmt, fill=YELLOW, align="center")
        put(ws, row, 3, unit, align="center")
        ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=10)
        put(ws, row, 4, note)
        for cc in range(5, 11):
            ws.cell(row=row, column=cc).border = BORDER
        ws.row_dimensions[row].height = 22

    def add_section(row, title_text):
        section(ws, row, title_text, 10)
        header(ws, row + 1, ["参数", "值", "单位", "说明 / 来源"])
        ws.merge_cells(start_row=row + 1, start_column=4, end_row=row + 1, end_column=10)
        for cc in range(5, 11):
            ws.cell(row=row + 1, column=cc).border = BORDER

    # ---- 一、视频与连接（常用） ----
    r = 4
    add_section(r, "一、视频与连接（常用）"); r += 2
    for key, name, val, unit, note, fmt in [
        ("BUF_SEC", "流缓存时长", spec["buffer_seconds"], "s", "MIKE_STREAM_BUFFER_MAX_SECONDS", "0"),
        ("P2P_CLIENTS", "P2P 最大客户端数", spec["p2p"]["clients"], "个", "MAX_P2P_CLIENTS", "0"),
        ("P2P_VID_KB", "P2P 视频发送块", spec["p2p"]["video_kb"], "KB", "8MP 1024KB，5MP 以下 512KB", "#,##0"),
        ("P2P_VID_BUFS", "每客户端视频块数", spec["p2p"].get("video_bufs", 1), "块", "估算，通常 1", "0"),
        ("P2P_AUD_KB", "P2P 音频块", spec["p2p"]["audio_kb"], "KB", "mike_p2p.c 推荐 32KB", "#,##0"),
        ("P2P_AUD_BUFS", "每客户端音频块数", spec["p2p"]["audio_bufs"], "块", "发送 + 接收，通常 2", "0"),
        ("AUDIO_KBPS", "音频码率", spec["audio"]["bitrate"], "kbps", "MIKE_STREAM_AUDIO_BITRATE", "#,##0"),
        ("AUDIO_CH", "音频通道数", spec["audio"]["channels"], "路", "音频 ring 路数", "0"),
        ("DRAM_MB", "总 DRAM", spec["dram_mb"], "MB", "硬件", "0"),
    ]:
        add_param(r, key, name, val, unit, note, fmt)
        r += 1

    # ---- 二/三、媒体侧输入 ----
    fs_rows, enc_rows = [], []
    if measured:
        r += 1
        section(ws, r, "二、媒体内存块（SigmaStar：mma_heap_name0 实测，单位字节）", 10); r += 1
        header(ws, r, ["分组", "名称", "", "", "字节", "", "", "备注", "", ""]); r += 1
        ws.merge_cells(start_row=r - 1, start_column=2, end_row=r - 1, end_column=4)
        ws.merge_cells(start_row=r - 1, start_column=5, end_row=r - 1, end_column=7)
        ws.merge_cells(start_row=r - 1, start_column=8, end_row=r - 1, end_column=10)
        for cc in (3, 4, 6, 7, 9, 10):
            ws.cell(row=r - 1, column=cc).border = BORDER
        for i, b in enumerate(mblocks):
            rr = r
            put(ws, rr, 1, b.get("group", ""), fill=YELLOW, align="center")
            ws.merge_cells(start_row=rr, start_column=2, end_row=rr, end_column=4)
            put(ws, rr, 2, b.get("name", f"block{i}"), bold=True)
            ws.merge_cells(start_row=rr, start_column=5, end_row=rr, end_column=7)
            put(ws, rr, 5, b.get("bytes", 0), fmt="#,##0", fill=YELLOW, align="center")
            ws.merge_cells(start_row=rr, start_column=8, end_row=rr, end_column=10)
            put(ws, rr, 8, b.get("note", ""), size=8)
            for cc in (3, 4, 6, 7, 9, 10):
                ws.cell(row=rr, column=cc).border = BORDER
            ws.row_dimensions[rr].height = 22
            r += 1
        media_cap_row = r
        add_param(r, "MEDIA_CAP", "媒体堆容量 cap", media.get("cap_bytes", 0), "B",
                  "bootargs mma_heap=...,sz=；Linux 可见 = DRAM − cap", "#,##0")
        r += 1
        r += 1
        section(ws, r, "三、编码通道", 10); r += 1
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
        put(ws, r, 1, "SigmaStar 路径不按分辨率推 VENC pool：编码器/帧源/ISP 内存已由上面的实测块覆盖（改规格请重采 mma_heap_name0 并重跑脚本）。", size=9)
        ws.row_dimensions[r].height = 26
        r += 2
    else:
        r += 1
        section(ws, r, "二、帧源通道 FrameSource / VB（sensor 分组）", 10); r += 1
        header(ws, r, ["启用", "名称", "Sensor", "宽 W", "高 H", "nrVBs", "IVDC直通", "备注"]); r += 1
        ws.merge_cells(start_row=r - 1, start_column=8, end_row=r - 1, end_column=10)
        for cc in (9, 10):
            ws.cell(row=r - 1, column=cc).border = BORDER
        fs_rows = list(range(r, r + len(spec["frame_sources"])))
        for i, rr in enumerate(fs_rows):
            x = spec["frame_sources"][i]
            put(ws, rr, 1, _cn(x.get("enabled")), fill=YELLOW, align="center")
            put(ws, rr, 2, x.get("name", ""), bold=True)
            put(ws, rr, 3, int(x.get("sensor", 0)), fmt="0", fill=YELLOW, align="center")
            put(ws, rr, 4, x.get("w", 0), fmt="#,##0", fill=YELLOW, align="center")
            put(ws, rr, 5, x.get("h", 0), fmt="#,##0", fill=YELLOW, align="center")
            put(ws, rr, 6, x.get("nrvbs", 0), fmt="0", fill=YELLOW, align="center")
            put(ws, rr, 7, _cn(x.get("ivdc")), fill=YELLOW, align="center")
            ws.merge_cells(start_row=rr, start_column=8, end_row=rr, end_column=10)
            put(ws, rr, 8, "IVDC：VB = ivdc_mem_line × W × 1.5" if x.get("ivdc") else "VB = nrVBs × W × H × 1.5")
            for cc in (9, 10):
                ws.cell(row=rr, column=cc).border = BORDER
            ws.row_dimensions[rr].height = 22
        r = fs_rows[-1] + 2

        # ---- 三、编码通道 ----
        section(ws, r, "三、编码通道（VENC 码率与 SDK ring 码率分开填）", 10); r += 1
        header(ws, r, ["启用", "名称", "Sensor", "类型", "宽 W", "高 H", "VENC kbps", "ring kbps", "流缓存 ring", "备注"]); r += 1
        enc_rows = list(range(r, r + len(spec["encoders"])))
        for i, rr in enumerate(enc_rows):
            x = spec["encoders"][i]
            put(ws, rr, 1, _cn(x.get("enabled")), fill=YELLOW, align="center")
            put(ws, rr, 2, x.get("name", ""), bold=True)
            put(ws, rr, 3, int(x.get("sensor", 0)), fmt="0", fill=YELLOW, align="center")
            put(ws, rr, 4, x.get("type", "H265"), fill=YELLOW, align="center")
            put(ws, rr, 5, x.get("w", 0), fmt="#,##0", fill=YELLOW, align="center")
            put(ws, rr, 6, x.get("h", 0), fmt="#,##0", fill=YELLOW, align="center")
            put(ws, rr, 7, x.get("bitrate", 0), fmt="#,##0", fill=YELLOW, align="center")
            put(ws, rr, 8, x.get("ring_bitrate", x.get("bitrate", 0)), fmt="#,##0", fill=YELLOW, align="center")
            put(ws, rr, 9, _cn(x.get("ring")), fill=YELLOW, align="center")
            default = "JPEG 按需建拆则稳态不计" if str(x.get("type", "")).upper() == "JPEG" else ""
            put(ws, rr, 10, x.get("note", default))
            ws.row_dimensions[rr].height = 22
        r = enc_rows[-1] + 2

    # ---- 四、网络与蓝牙 ----
    add_section(r, "四、网络与蓝牙"); r += 2
    for key, name, val, unit, note, fmt in [
        ("NET_TYPE", "网络类型", nt_display, "", "WiFi / 4G / WiFi+4G / 无（网络在对端 RTOS）", None),
        ("NET_WIFI_KB", "WiFi supplicant", net.get("wifi_supplicant_kb", 0), "KB", "wpa_supplicant + udhcpc；4K 样机 实测 2,076KB", "#,##0"),
        ("NET_LTE_KB", "4G supplicant", net.get("lte_supplicant_kb", 0), "KB", "4G 机型选填，默认 512", "#,##0"),
        ("NET_LTE_MOD_KB", "4G 模块预留", net.get("lte_module_kb", 0), "KB", "驱动/固件预留", "#,##0"),
        ("BT_ENABLED", "启用蓝牙", _cn(bt.get("enabled")), "", "AIC8800 机型", None),
        ("BT_USER_KB", "蓝牙用户态", bt.get("userspace_kb", 868), "KB", "miio_bt；内核模块在 modules 基线", "#,##0"),
    ]:
        add_param(r, key, name, val, unit, note, fmt)
        r += 1

    # ---- 五、MTP / 海外语控 ----
    r += 1
    add_section(r, "五、MTP / 海外语控（可选）"); r += 2
    for key, name, val, unit, note, fmt in [
        ("MTP_ENABLED", "启用 MTP", _cn(mtp.get("enabled")), "", "是/否；4K-4G 选是", None),
        ("MTP_SESSIONS", "MTP 会话数", mtp.get("sessions", 3), "个", "默认 max(3, P2P clients+1)", "0"),
        ("MTP_FIXED_KB", "MTP 固定开销", mtp.get("fixed_kb", 128), "KB", "SDK 池/管理器", "#,##0"),
        ("MTP_MEASURED", "MTP 实测覆盖", mtp.get("measured_bytes", 0), "B", ">0 时优先于公式", "#,##0"),
        ("WEBRTC_ENABLED", "启用海外语控", _cn(web.get("enabled")), "", "WebRTC / Alexa / Google 统称", None),
        ("WEBRTC_SESSIONS", "语控会话数", web.get("sessions", 1), "个", "HWYK 独占 1 路", "0"),
        ("WEBRTC_FIXED_KB", "语控固定开销", web.get("fixed_kb", 128), "KB", "SDK/信令/初始化", "#,##0"),
        ("WEBRTC_PER_SESSION_KB", "每会话开销", web.get("per_session_kb", 1024), "KB", "ICE/DTLS/SCTP/RTP/jitter，估算", "#,##0"),
        ("WEBRTC_MEASURED", "语控实测覆盖", web.get("measured_bytes", 0), "B", ">0 时优先于公式", "#,##0"),
    ]:
        add_param(r, key, name, val, unit, note, fmt)
        r += 1

    # ---- 六、NPU 与算法 ----
    r += 1
    add_section(r, "六、NPU 与算法"); r += 2
    for key, name, val, unit, note, fmt in [
        ("NPU_POOL_KB", "NPU 池", na.get("npu_pool_kb", 0), "KB", "init.sh cma_pool_size（4K-WiFi 4MB / 4K-4G 3.5MB）", "#,##0"),
        ("ALGO_MODEL_KB", "算法模型", na.get("algo_model_kb", 0), "KB", "查 references/algo-models.md 登记表", "#,##0"),
        ("NPU_IN_CMA", "含在 CMA", _cn(na.get("inside_cma", True)), "", "是：算法与 CMA 不双算；否：单独加", None),
    ]:
        add_param(r, key, name, val, unit, note, fmt)
        r += 1

    # ---- 七、固定项基线 ----
    r += 1
    add_section(r, "七、固定项基线（历史实测；单位字节）"); r += 2
    parts = fx.get("parts", {})

    def add_fixed(key, name, note):
        nonlocal r
        plist = parts.get(key, []) if key in ("kernel", "modules") else []
        if not plist:
            add_param(r, "FIX_" + key.upper(), name, fx.get(key, 0), "B", note, "#,##0")
            r += 1
            return
        first = r
        for i, part in enumerate(plist):
            add_param(r, f"FIX_{key.upper()}_P{i}", "· " + part["name"], part["bytes"], "B",
                      part.get("note", "构建产物段大小（不含 init/exit）"), "#,##0")
            ws.row_dimensions[r].outlineLevel = 1
            put(ws, r, 1, "· " + part["name"], bold=False)
            r += 1
        inp["FIX_" + key.upper()] = r
        put(ws, r, 1, name + " 合计", bold=True)
        put(ws, r, 2, f"=SUM(B{first}:B{r - 1})", fmt="#,##0", fill=GREY, align="center")
        put(ws, r, 3, "B", align="center")
        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=10)
        put(ws, r, 4, note)
        for cc in range(5, 11):
            ws.cell(row=r, column=cc).border = BORDER
        ws.row_dimensions[r].height = 22
        r += 1

    for key, name, note in [
        ("kernel", "kernel", "内核代码+数据（text/rodata/data/bss + 页表/栈/余量）"),
        ("modules", "modules", "已加载 .ko 段大小合计（不含 init/exit）"),
        ("cma", "CMA / TNPU", "CONFIG_CMA_SIZE_MBYTES"),
        ("slab", "slab", "仅 SUnreclaim 经验值；可回收部分不计"),
        ("app_base", "c_mi_ipc 基础 anon", "不含 ring/P2P 的堆与栈"),
        ("miio_client", "miio_client", "独立 IoT 客户端 RSS"),
        ("miio_ot", "miio_ot", "如有独立进程，按实测 RSS"),
        ("other_procs", "其他进程", "init/log/adbd/storage"),
        ("file_cache", "file cache", "squashfs 代码页，可回收"),
        ("zram", "zram 实际占用", "zsmalloc 压缩后"),
    ]:
        add_fixed(key, name, note)

    # ---- 八、新增线程 ----
    r += 1
    add_section(r, "八、新增线程（可选）"); r += 2
    for key, name, val, unit, note, fmt in [
        ("EXTRA_THREADS", "新增线程数", sl.get("extra_threads", 0), "个", "基准线程栈已含在基础 anon，不要重复加", "0"),
        ("THREAD_STACK_KB", "每线程栈", sl.get("thread_stack_kb", 64), "KB", "常见配置 64KB", "#,##0"),
    ]:
        add_param(r, key, name, val, unit, note, fmt)
        r += 1

    # ---- 九、标定系数与常量（高级） ----
    r += 1
    add_section(r, "九、标定系数与常量（高级，一般不动）"); r += 2
    for key, name, val, unit, note, fmt in [
        ("IVDC_LINE", "ivdc_mem_line", spec["ivdc_mem_line"], "行", "init.sh insmod tx-isp 参数", "0"),
        ("K_ISP", "k_isp", spec["coefficients"]["k_isp"], "×单帧", "MDNS 系数；每个 sensor 各乘一次", "0.0000"),
        ("POOL_A", "pool_a", spec["coefficients"].get("pool_a", 0), "×单帧", "码率无关 pool 系数；0 = 回退 k_recon/k_stream", "0.000000"),
        ("POOL_B", "pool_b", spec["coefficients"].get("pool_b", 0), "B", "pool 固定项", "#,##0"),
        ("K_JPEG", "k_jpeg", spec["coefficients"]["k_jpeg"], "×单帧", "JPEG 码流缓冲系数", "0.0000"),
        ("JPEG_OVERHEAD", "JPEG 固定开销", spec["coefficients"]["jpeg_overhead"], "B", "实测字节 − k_jpeg×F", "#,##0"),
        ("K_RECON", "k_recon", spec["coefficients"]["k_recon"], "×单帧", "旧式回退系数（pool_a=0 时用）", "0.0000"),
        ("K_STREAM", "k_stream", spec["coefficients"]["k_stream"], "×maxI", "旧式回退系数（pool_a=0 时用）", "0.0000"),
        ("MAXI_FACTOR", "maxI 系数", spec["coefficients"]["maxi_factor"], "×码率", "maxIPictureSize = maxBitRate × 5/2", "0.0000"),
        ("H264_FACTOR", "h264_factor", spec["coefficients"].get("h264_factor", 1.22), "×H265模型", "H264 无 RFC，按 H265 模型乘此系数", "0.0000"),
        ("SDK_RESERVED", "SDK 预留块", spec["sdk_reserved"], "B", "2 × 512,000 reuse=FREE", "#,##0"),
        ("MTP_WND_RCV", "KCP wnd_rcv", mtp.get("wnd_rcv", 128), "段", "SDK 常量 MTP_IKCP_WND_RCV", "0"),
        ("MTP_WND_SND", "KCP wnd_snd", mtp.get("wnd_snd", 32), "段", "SDK 常量 MTP_IKCP_WND_SND", "0"),
        ("MTP_MTU", "KCP MTU", mtp.get("mtu_bytes", 920), "B", "SDK 常量 MTP_IKCP_MTU_DEF", "0"),
        ("MTP_OVERHEAD_KB", "MTP 每会话开销", mtp.get("overhead_kb_per_session", 64), "KB", "会话/MBuffer/加密等，估算", "#,##0"),
        ("BYTES_PER_MB", "1 MB", MIB, "B", "单位换算", "#,##0"),
    ]:
        add_param(r, key, name, val, unit, note, fmt)
        r += 1

    # ---- 校验与定义名 ----
    dv_net = DataValidation(type="list", formula1='"WiFi,4G,WiFi+4G,无（网络栈在对端 MCU/RTOS）"', allow_blank=False)
    ws.add_data_validation(dv_net); dv_net.add(f"B{inp['NET_TYPE']}")
    dv_yn = DataValidation(type="list", formula1='"是,否"', allow_blank=False)
    ws.add_data_validation(dv_yn)
    for k in ("BT_ENABLED", "MTP_ENABLED", "WEBRTC_ENABLED", "NPU_IN_CMA"):
        dv_yn.add(f"B{inp[k]}")
    dv = DataValidation(type="list", formula1='"是,否"', allow_blank=False)
    ws.add_data_validation(dv)
    for rows_ in (fs_rows, enc_rows):
        if rows_:
            dv.add(f"A{rows_[0]}:A{rows_[-1]}")
    if fs_rows:
        dv.add(f"G{fs_rows[0]}:G{fs_rows[-1]}")
    if enc_rows:
        dv.add(f"I{enc_rows[0]}:I{enc_rows[-1]}")
        dv2 = DataValidation(type="list", formula1='"H265,H264,JPEG"', allow_blank=False)
        ws.add_data_validation(dv2)
        dv2.add(f"D{enc_rows[0]}:D{enc_rows[-1]}")

    for key, row in inp.items():
        wb.defined_names.add(DefinedName(key, attr_text=f"'输入详细'!$B${row}"))

    # ==================== 详细表 ====================
    ws = wb.create_sheet("详细表")
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDEF", [16, 30, 54, 15, 10, 32]):
        ws.column_dimensions[col].width = w
    title(ws, "详细表（按五大块展开）", "F")
    ws.merge_cells("A2:F2")
    put(ws, 2, 1, "A 列标块（1 RMEM … 5 其他）；Excel 大纲可折叠：1 级=块内项/应用，2 级=kernel 段、各 .ko、c_mi_ipc 开销；灰色格为公式。", size=9)

    def _h(*pairs):
        n = 1
        for text, width in pairs:
            if not text:
                continue
            w = sum(2 if ord(ch) > 127 else 1 for ch in str(text))
            n = max(n, -(-w // width))
        return max(22, n * 14 + 8)

    def line(row, name, basis, expr, note="", fill=GREY, level=1):
        put(ws, row, 2, name, bold=True)
        put(ws, row, 3, basis)
        put(ws, row, 4, expr, fmt="#,##0", fill=fill)
        put(ws, row, 5, f"=D{row}/BYTES_PER_MB", fmt="0.00", fill=fill)
        put(ws, row, 6, note)
        ws.row_dimensions[row].height = _h((name, 28), (basis, 52), (note, 30))
        ws.row_dimensions[row].outlineLevel = level

    def total_line(row, label, expr, basis="SUM", note="", level=0):
        put(ws, row, 2, label, bold=True)
        put(ws, row, 3, basis)
        put(ws, row, 4, expr, fmt="#,##0", fill=GREEN, bold=True)
        put(ws, row, 5, f"=D{row}/BYTES_PER_MB", fmt="0.00", fill=GREEN, bold=True)
        put(ws, row, 6, note)
        ws.row_dimensions[row].height = 22
        ws.row_dimensions[row].outlineLevel = level

    def block_label(first, last, text):
        # 不纵向合并：合并区跨折叠行时 Excel 显示不可靠；块名同时写在首行与小计行，折叠后仍可见
        for rr in range(first, last + 1):
            c = ws.cell(row=rr, column=1)
            c.border = BORDER
            c.fill = PatternFill("solid", fgColor=LBLUE)
        for rr in (first,):
            c = ws.cell(row=rr, column=1, value=text)
            c.font = Font(name=FONT, size=10, bold=True)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    r = 4
    header(ws, r, ["块", "项目", "计算依据", "字节", "MB", "备注"]); r += 1

    # ---- 块 1：RMEM ----
    b = r
    rmem_first = r
    if measured:
        for blk in mblocks:
            if blk.get("group") == "算法":
                continue
            line(r, blk.get("name", "块"), f"实测 · {blk.get('group', '')}（mma_heap_name0）",
                 float(blk.get("bytes", 0)), blk.get("note", ""))
            r += 1
        if spec.get("sdk_reserved"):
            line(r, "SDK 预留块", "spec.sdk_reserved", float(spec["sdk_reserved"]), "媒体堆外的小块预留")
            r += 1
        rmem_sub = r
        total_line(r, "RMEM 小计", f"=SUM(D{rmem_first}:D{r - 1})", "媒体堆 used − 算法块", "块 1 合计")
        r += 1
        cap = result["rmem"]["cap"]
        line(r, "媒体堆容量 cap", "bootargs mma_heap=...,sz=（carve-out 上限）", cap,
             f"used {result['rmem']['usage'] * 100:.1f}%；Linux 可见 = DRAM − cap", fill=GREEN, level=0)
        r += 1
        block_label(b, r - 1, "1. RMEM")
        r += 1
    else:
        for s in result["rmem"]["mdns_by_sensor"]:
            terms = ",".join(
                f"IF(AND('输入详细'!$A${x}=\"是\",'输入详细'!$C${x}={s}),"
                f"'输入详细'!$D${x}*'输入详细'!$E${x}*1.5,0)"
                for x in fs_rows
            )
            line(r, f"ISP MDNS · Sensor {s}", "k_isp × 该 sensor 的最大帧字节", f"=K_ISP*MAX({terms})", "每 sensor 一份，不是全机一份")
            r += 1
        for i, x in enumerate(fs_rows):
            name = spec["frame_sources"][i].get("name", f"FS{i}")
            line(r, f"{name} VB", "IVDC：ivdc_mem_line×W×1.5；否则 nrVBs×W×H×1.5",
                 f"=IF('输入详细'!$A${x}=\"否\",0,IF('输入详细'!$G${x}=\"是\","
                 f"IVDC_LINE*'输入详细'!$D${x}*1.5,"
                 f"'输入详细'!$F${x}*'输入详细'!$D${x}*'输入详细'!$E${x}*1.5))",
                 f"Sensor {spec['frame_sources'][i].get('sensor', 0)}")
            r += 1
        for i, x in enumerate(enc_rows):
            line(r, f"{spec['encoders'][i].get('name', f'enc{i}')} VENC pool",
                 "JPEG：k_jpeg×F+overhead；否则 pool_a×F+pool_b（与码率无关）",
                 f"=IF('输入详细'!$A${x}=\"否\",0,IF('输入详细'!$D${x}=\"JPEG\","
                 f"K_JPEG*'输入详细'!$E${x}*'输入详细'!$F${x}*1.5+JPEG_OVERHEAD,"
                 f"IF(POOL_A>0,"
                 f"(POOL_A*'输入详细'!$E${x}*'输入详细'!$F${x}*1.5+POOL_B)*"
                 f"IF('输入详细'!$D${x}=\"H264\",H264_FACTOR,1),"
                 f"(K_RECON*'输入详细'!$E${x}*'输入详细'!$F${x}*1.5+"
                 f"K_STREAM*('输入详细'!$G${x}*MAXI_FACTOR*1000/8))*"
                 f"IF('输入详细'!$D${x}=\"H264\",H264_FACTOR,1))))",
                 f"Sensor {spec['encoders'][i].get('sensor', 0)}；pool_a=0 时回退 k_recon/k_stream")
            r += 1
        line(r, "SDK 预留块", "2 × 512,000 B（reuse=FREE）", "=SDK_RESERVED", "continuous_alloc 小块")
        r += 1
        rmem_sub = r
        total_line(r, "RMEM 小计", f"=SUM(D{rmem_first}:D{r - 1})", f"SUM(D{rmem_first}:D{r - 1})", "块 1 合计")
        r += 1
        line(r, "建议 rmem carve-out", "按需求向上取整到 1MB",
             f"=CEILING(D{rmem_sub}/BYTES_PER_MB,1)*BYTES_PER_MB", "rmem 由管线计算，不做输入", fill=GREEN, level=0)
        r += 1
        block_label(b, r - 1, "1. RMEM")
        r += 1

    # ---- 块 2：Linux 系统 ----
    os_block = r
    b = r
    cma_note = ("NPU+算法已超 CMA 总量，CMA 全额计入算法块"
                if na.get("inside_cma", True)
                and (float(na.get("npu_pool_kb", 0)) + float(na.get("algo_model_kb", 0))) * 1024.0 > float(fx.get("cma", 0))
                else "避免与算法块双算")

    def sub_line(row, name, basis, expr, note=""):
        put(ws, row, 2, "     · " + name)
        put(ws, row, 3, basis)
        put(ws, row, 4, expr, fmt="#,##0")
        put(ws, row, 5, f"=D{row}/BYTES_PER_MB", fmt="0.00")
        put(ws, row, 6, note)
        ws.row_dimensions[row].height = _h((name, 26), (basis, 52), (note, 30))
        ws.row_dimensions[row].outlineLevel = 2

    os_top = []
    for key, label, basis, expr, note in [
        ("kernel", "kernel", "内核代码+数据：各段之和 + 页表 + 栈 + 余量", "=FIX_KERNEL", "展开项见上，可折叠"),
        ("modules", "modules", "已加载 .ko 段大小之和", "=FIX_MODULES", "展开项见上，可折叠"),
    ]:
        for i, part in enumerate(fx.get("parts", {}).get(key, [])):
            sub_line(r, part["name"], part.get("basis", "构建产物段大小（不含 init/exit）"),
                     f"=FIX_{key.upper()}_P{i}", part.get("note", ""))
            r += 1
        os_top.append(r)
        line(r, label, basis, expr, note)
        r += 1
    for name, basis, expr, note in [
        ("CMA（非算法）", "总 CMA − NPU/算法；算法不含 CMA 时取全量",
         "=IF(NPU_IN_CMA=\"是\",MAX(0,FIX_CMA-(NPU_POOL_KB+ALGO_MODEL_KB)*1024),FIX_CMA)", cma_note),
        ("slab", "仅 SUnreclaim 经验值", "=FIX_SLAB", "SReclaimable 不计"),
        ("file cache", "squashfs 代码页，可回收", "=FIX_FILE_CACHE", "硬占用会扣掉它"),
        ("zram", "zsmalloc 压缩后实际占用", "=FIX_ZRAM", "按压力上界计入"),
    ]:
        os_top.append(r)
        line(r, name, basis, expr, note)
        r += 1
    os_sub = r
    total_line(r, "Linux 系统小计", "=SUM(" + ",".join(f"D{x}" for x in os_top) + ")",
               "kernel + modules + CMA + slab + cache + zram", "块 2 合计")
    r += 1
    block_label(b, r - 1, "2. Linux 系统")
    r += 1

    # ---- 块 3：Linux 应用 ----
    app_block = r
    b = r
    app_first = r
    for i, x in enumerate(enc_rows):
        sub_line(r, f"{spec['encoders'][i].get('name', f'enc{i}')} 流缓存 ring",
                 "ring 码率 × 128 × 缓存秒数（启用且勾 ring、非 JPEG）",
                 f"=IF(AND('输入详细'!$A${x}=\"是\",'输入详细'!$I${x}=\"是\","
                 f"'输入详细'!$D${x}<>\"JPEG\"),'输入详细'!$H${x}*128*BUF_SEC,0)",
                 "SDK 堆，开机一次性 calloc")
        r += 1
    sub_line(r, "音频流缓存 ring", "音频码率 × 128 × 缓存秒数 × 通道数", "=AUDIO_KBPS*128*BUF_SEC*AUDIO_CH", "OPUS 25fps")
    r += 1
    sub_line(r, "P2P 收发缓冲", "客户端 ×（视频块×块数 + 音频块×块数×2）",
             "=P2P_CLIENTS*(P2P_VID_KB*1024*P2P_VID_BUFS+P2P_AUD_KB*1024*P2P_AUD_BUFS*2)", "块数为估计值，需实测")
    r += 1
    sub_line(r, "MTP SDK（可选）", "固定 + sessions × ((wnd_rcv+wnd_snd)×MTU + 每会话开销)",
             "=IF(MTP_ENABLED=\"否\",0,IF(MTP_MEASURED>0,MTP_MEASURED,"
             "MTP_FIXED_KB*1024+MTP_SESSIONS*((MTP_WND_RCV+MTP_WND_SND)*MTP_MTU+MTP_OVERHEAD_KB*1024)))",
             "KCP 窗口估算；有实测填实测覆盖")
    r += 1
    sub_line(r, "海外语控 SDK（可选）", "固定 + 会话数 × 每会话开销",
             "=IF(WEBRTC_ENABLED=\"否\",0,IF(WEBRTC_MEASURED>0,WEBRTC_MEASURED,"
             "WEBRTC_FIXED_KB*1024+WEBRTC_SESSIONS*WEBRTC_PER_SESSION_KB*1024))",
             "统称 WebRTC / Alexa / Google")
    r += 1
    sub_line(r, "新增线程栈", "新增线程数 × 每线程栈（物理触底）", "=EXTRA_THREADS*THREAD_STACK_KB*1024",
             "基准线程已含在基础 anon，不重复加")
    r += 1
    sub_line(r, "基础 anon", "不含 ring/P2P 的堆与栈", "=FIX_APP_BASE", "固定项基线")
    r += 1
    mike_sub = r
    app_name = "mi_ipc" if measured else "c_mi_ipc"
    total_line(r, f"{app_name} 合计", f"=SUM(D{app_first}:D{r - 1})", f"SUM(D{app_first}:D{r - 1})",
               "应用开销见上（ring / 音频 / P2P / MTP / 语控 / 线程 / 基础 anon）", level=1)
    r += 1
    miio1 = r
    line(r, "miio_client", "独立 IoT 客户端 RSS", "=FIX_MIIO_CLIENT", "固定项基线")
    r += 1
    miio2 = r
    line(r, "miio_ot", "如有独立进程，按实测 RSS", "=FIX_MIIO_OT", "固定项基线")
    r += 1
    app_sub = r
    total_line(r, "Linux 应用小计", f"=D{mike_sub}+D{miio1}+D{miio2}", "c_mi_ipc + miio_client + miio_ot", "块 3 合计")
    r += 1
    block_label(b, r - 1, "3. Linux 应用")
    r += 1

    # ---- 块 4：算法 ----
    b = r
    algo_first = r
    if measured:
        for blk in mblocks:
            if blk.get("group") != "算法":
                continue
            line(r, blk.get("name", "算法块"), "实测 · MMA 堆内（已从 RMEM 划出，不双算）",
                 float(blk.get("bytes", 0)), blk.get("note", ""))
            r += 1
    else:
        line(r, "NPU 池", "TNPU/CMA 池（init.sh cma_pool_size）", "=NPU_POOL_KB*1024", "不可随手缩：长测会被动态缓冲吃干")
        r += 1
        line(r, "算法模型", "查 references/algo-models.md 登记表", "=ALGO_MODEL_KB*1024", "flash 模型文件大小不是工作内存")
        r += 1
    algo_sub = r
    total_line(r, "算法小计", f"=SUM(D{algo_first}:D{r - 1})", f"SUM(D{algo_first}:D{r - 1})", "块 4 合计")
    r += 1
    block_label(b, r - 1, "4. 算法")
    r += 1

    # ---- 块 5：其他 ----
    b = r
    other_first = r
    line(r, "other_procs", "init / log / adbd / storage 等", "=FIX_OTHER_PROCS", "固定项基线")
    r += 1
    line(r, "网络（WiFi/4G）", "按网络类型取 wpa_supplicant / LTE supplicant",
         "=IF(OR(NET_TYPE=\"WiFi\",NET_TYPE=\"WiFi+4G\"),NET_WIFI_KB*1024,0)"
         "+IF(OR(NET_TYPE=\"4G\",NET_TYPE=\"WiFi+4G\"),(NET_LTE_KB+NET_LTE_MOD_KB)*1024,0)",
         "WiFi 机型未填按 0 计，可能漏计")
    r += 1
    line(r, "蓝牙用户态", "miio_bt；内核模块在 Linux 系统块", "=IF(BT_ENABLED=\"是\",BT_USER_KB*1024,0)", "4K 样机 实测 868KB")
    r += 1
    other_sub = r
    total_line(r, "其他小计", f"=SUM(D{other_first}:D{r - 1})", f"SUM(D{other_first}:D{r - 1})", "块 5 合计")
    r += 1
    block_label(b, r - 1, "5. 其他")
    r += 1

    ws.row_breaks.append(Break(id=os_block - 1))
    ws.row_breaks.append(Break(id=app_block - 1))
    grand = r
    ws.cell(row=grand, column=1).border = BORDER
    put(ws, grand, 2, "五大块合计", bold=True)
    put(ws, grand, 3, "1+2+3+4+5")
    put(ws, grand, 4, f"=D{rmem_sub}+D{os_sub}+D{app_sub}+D{algo_sub}+D{other_sub}",
        fmt="#,##0", fill=GREEN, bold=True)
    put(ws, grand, 5, f"=D{grand}/BYTES_PER_MB", fmt="0.00", fill=GREEN, bold=True)
    put(ws, grand, 6, "应等于『汇总』五项合计")
    ws.row_dimensions[grand].height = 22
    r += 2
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    put(ws, r, 1, "口径：rmem 先从 DRAM 扣掉，Linux 可见 = DRAM − rmem；算法含 CMA 时从 Linux 系统 CMA 中扣除，避免双算。", size=9)
    ws.row_dimensions[r].height = 26

    # ==================== 汇总 ====================
    ws = wb.create_sheet("汇总")
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCD", [32, 18, 12, 56]):
        ws.column_dimensions[col].width = w
    title(ws, f"{spec['project']} 内存汇总（简化）", "D")
    ws.merge_cells("A2:D2")
    put(ws, 2, 1, "每块一行，逐项明细见『详细表』；判定看硬余量。", size=9)
    section(ws, 3, "一、五大块汇总与最终预留", 4)
    header(ws, 4, ["项目", "字节", "MB", "说明"])
    rows = [
        ("DRAM 总量", "=DRAM_MB*BYTES_PER_MB", "硬件"),
        ("1. RMEM", f"='详细表'!D{rmem_sub}",
         "MMA 堆 used（含编码/帧源/ISP，未含算法块）" if measured else "媒体 carve-out（含 JPEG / SDK 预留）"),
        ("2. Linux 系统", f"='详细表'!D{os_sub}", "kernel / modules / CMA(非算法) / slab / cache / zram"),
        ("3. Linux 应用", f"='详细表'!D{app_sub}", "c_mi_ipc 合计 + miio_client / miio_ot"),
        ("4. 算法", f"='详细表'!D{algo_sub}", "NPU 池 + 算法模型"),
        ("5. 其他", f"='详细表'!D{other_sub}", "other_procs + 网络 + 蓝牙"),
        ("五项合计", "=SUM(B6:B10)", "1+2+3+4+5"),
        ("最终预留（账面）", "=B5-B11", "DRAM − 五项合计"),
        ("硬占用", "=B11-FIX_FILE_CACHE", "五项合计 − file cache（可回收）"),
        ("硬余量", "=B5-B13", "DRAM − 硬占用；判定看这一行"),
    ]
    r = 5
    for name, expr, note in rows:
        put(ws, r, 1, name, bold=True)
        put(ws, r, 2, expr, fmt="#,##0", fill=GREY)
        put(ws, r, 3, f"=B{r}/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, r, 4, note)
        ws.row_dimensions[r].height = 22
        r += 1
    verdict_row = r + 1
    put(ws, verdict_row, 1, "判定", bold=True)
    put(ws, verdict_row, 2, "=IF(B14>=0,\"✅ 预算可行\",\"⚠️ 预算超支\")", fill=GREEN)
    ws.merge_cells(start_row=verdict_row, start_column=2, end_row=verdict_row, end_column=4)

    section(ws, verdict_row + 2, "二、判定口径", 4)
    for i, s in enumerate([
        "账面占用 = 五项合计；硬占用 = 五项合计 − file_cache；硬余量 = DRAM − 硬占用，OK/超看硬余量。",
        "rmem 与 Linux 是两本账：rmem 先从 DRAM 扣掉再算 Linux 可见，不能拿 Linux 余量补 rmem。",
        "默认最大场景（常驻组件全在线 + 全流 + P2P 满客户端 + 缓存取最大），稳态快照只作参考。",
    ]):
        rr = verdict_row + 3 + i
        ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=4)
        put(ws, rr, 1, s)
        ws.row_dimensions[rr].height = 24

    # ==================== 速查表 ====================
    ws = wb.create_sheet("速查表")
    ws.sheet_view.showGridLines = False
    widths = [24, 10, 10, 12, 8, 8, 11, 11, 11, 12, 12, 14, 14, 14]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    title(ws, "附录 · 速查表", "N")
    ws.merge_cells("A2:N2")
    put(ws, 2, 1, "主表：分辨率 × 码率横比；附表：实测标定点与采集命令。", size=9)
    section(ws, 3, "一、分辨率 × 码率速查（不含固定项，单视频通道 + 单帧源）", 14)
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
        put(ws, row, 8, f"=IF(POOL_A>0,POOL_A*B{row}*C{row}*1.5+POOL_B,"
                        f"K_RECON*B{row}*C{row}*1.5+K_STREAM*(D{row}*MAXI_FACTOR*1000/8))/BYTES_PER_MB",
            fmt="0.00", fill=GREY)
        put(ws, row, 9, f"=IF(F{row}=\"是\",IVDC_LINE*B{row}*1.5,2*B{row}*C{row}*1.5)/BYTES_PER_MB",
            fmt="0.00", fill=GREY)
        put(ws, row, 10, f"=K_ISP*B{row}*C{row}*1.5/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, row, 11, f"=D{row}*128*BUF_SEC/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, row, 12, f"=(IF(POOL_A>0,POOL_A*B{row}*C{row}*1.5+POOL_B,"
                         f"K_RECON*B{row}*C{row}*1.5+K_STREAM*(D{row}*MAXI_FACTOR*1000/8))+"
                         f"IF(F{row}=\"是\",IVDC_LINE*B{row}*1.5,2*B{row}*C{row}*1.5)+"
                         f"K_ISP*B{row}*C{row}*1.5+SDK_RESERVED)/BYTES_PER_MB", fmt="0.00", fill=GREEN)
        put(ws, row, 13, f"=(D{row}*128*BUF_SEC+P2P_CLIENTS*(P2P_VID_KB*1024*P2P_VID_BUFS+"
                         f"P2P_AUD_KB*1024*P2P_AUD_BUFS*2))/BYTES_PER_MB", fmt="0.00", fill=GREY)
        put(ws, row, 14, f"=L{row}+M{row}", fmt="0.00", fill=GREEN)
        ws.row_dimensions[row].height = 22
    ws.merge_cells("A18:N19")
    if measured:
        put(ws, 18, 1, "注：本表是君正 T32Pro 的 rmem 参考式（按分辨率/码率推导 pool），"
                       "SigmaStar 的媒体内存由 mma_heap_name0 实测块决定，不用本表外推；"
                       "本项目实际占用以『详细表』块 1 为准。", size=9, fill=RED)
    else:
        put(ws, 18, 1, "注：默认系数来自 4K-4G 2026-11 实测；4K-WiFi 2026-06 的 28MB 场景为历史参考。"
                       "其他项目请按附表重新拟合。双摄的 MDNS 要按 sensor 各算一次。", size=9, fill=RED)
    ws.row_dimensions[18].height = 30

    pts = [
        ("4K-4G ISP MDNS @4K", 9433088, "continuous_mem_info", "k_isp × max(F)"),
        ("4K-4G FS0 IVDC VB", 1152000, "continuous_mem_info", "ivdc_mem_line × W × 1.5"),
        ("4K-4G FS1 VB (nrVBs=2)", 1198080, "continuous_mem_info", "nrVBs × W × H × 1.5"),
        ("4K-4G VENC 主 4K/1500", 10579968, "continuous_mem_info", "pool_a × F + pool_b"),
        ("4K-4G VENC 次 480P/350", 1115136, "continuous_mem_info", "pool_a × F + pool_b"),
        ("4K-4G JPEG pool 832×480", 609536, "continuous_mem_info", "k_jpeg × F + overhead"),
        ("4K-4G SDK small blocks", 113152, "continuous_mem_info", "SDK 预留块"),
        ("4K-WiFi ISP MDNS @4K", 10436608, "rmem_optimization_analysis.md:16", "k_isp × max(F)（2026-06 系数）"),
        ("4K-WiFi FS0 IVDC VB", 1152000, "rmem_optimization_analysis.md:21", "ivdc_mem_line × W × 1.5"),
        ("4K-WiFi FS1 VB (nrVBs=2)", 1198080, "rmem_optimization_analysis.md:22", "nrVBs × W × H × 1.5"),
        ("4K-WiFi VENC pool 4K H265", 14312448, "rmem_optimization_analysis.md:19", "旧式 k_recon×F + k_stream×I"),
        ("4K-WiFi VENC pool 832×480", 789248, "rmem_optimization_analysis.md:20", "旧式 k_recon×F + k_stream×I"),
        ("4K-WiFi JPEG pool 832×480", 607360, "20261016-jpeg-snap-ondemand.md", "k_jpeg × F + overhead"),
        ("4K-WiFi SDK ring 主 2000kbps×6s", 1536000, "c-mi-ipc-memory-profile.md §八", "ring kbps × 128 × 秒"),
        ("4K-WiFi SDK ring 音频 100kbps×6s", 76800, "c-mi-ipc-memory-profile.md §八", "音频 kbps × 128 × 秒"),
        ("4K-WiFi SDK 预留块 2×512KB", 1024000, "rmem_optimization_analysis.md:17-18", "2 × 512,000 B"),
    ]
    ws.row_breaks.append(Break(id=20))
    section(ws, 21, "二、实测标定点（标定依据；本项目的模型预测见『详细表』）", 14)
    header(ws, 22, ["数据点", "实测字节", "", "来源", "", "", "对应公式", "", "", "", "", "", "", ""])
    ws.merge_cells("B22:C22"); ws.merge_cells("D22:F22"); ws.merge_cells("G22:N22")
    for i, (name, meas, src, term) in enumerate(pts):
        row = 23 + i
        put(ws, row, 1, name, bold=True)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        put(ws, row, 2, meas, fmt="#,##0", fill=YELLOW)
        ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=6)
        put(ws, row, 4, src, size=8)
        ws.merge_cells(start_row=row, start_column=7, end_row=row, end_column=14)
        put(ws, row, 7, term, size=8)
        ws.row_dimensions[row].height = _h((name, 22), (src, 34))

    emp_start = 23 + len(pts) + 2
    ws.row_breaks.append(Break(id=emp_start - 1))
    section(ws, emp_start, "三、经验值 / 历史实测基线（说明页『经验值』项的取值）", 14)
    header(ws, emp_start + 1, ["项", "", "值", "", "来源 / 口径", "", "", "", "", "", "", "", "", ""])
    ws.merge_cells(start_row=emp_start + 1, start_column=1, end_row=emp_start + 1, end_column=2)
    ws.merge_cells(start_row=emp_start + 1, start_column=3, end_row=emp_start + 1, end_column=4)
    ws.merge_cells(start_row=emp_start + 1, start_column=5, end_row=emp_start + 1, end_column=14)
    for cc in range(6, 15):
        ws.cell(row=emp_start + 1, column=cc).border = BORDER
    for cc in (2, 4):
        ws.cell(row=emp_start + 1, column=cc).border = BORDER
    emps = [
        ("kernel（4K-WiFi 基线）", "6,501,171 B", "历史实测基线；vmlinux 段复核 5.81 MiB"),
        ("modules（4K-WiFi）", "1,782,579 B", "/proc/modules 汇总；用户态 wpa / BT 不在内"),
        ("CMA 总量", "4,194,304 / 6,291,456 B", "4K-WiFi 4MB / 4K-4G 6MB；CONFIG_CMA_SIZE_MBYTES / init.sh"),
        ("slab（SUnreclaim）", "4,071,424 B", "4K 样机 实测 3,976KB；SReclaimable 不计"),
        ("c_mi_ipc 基础 anon", "2,097,152 B", "4K-WiFi smaps 残差确认 1.99 MiB"),
        ("other_procs", "3,565,158 B", "init / log / adbd / storage 实测"),
        ("file cache", "8,283,750 B", "/proc/meminfo 实测；可回收，硬占用中扣除"),
        ("zram", "5,242,880 B", "zsmalloc 压缩后实测；按压力上界计入"),
        ("WiFi supplicant", "2,076 KB", "4K 样机：wpa 1,608 + udhcpc 468（最大场景）"),
        ("4G supplicant", "512 KB", "默认经验值"),
        ("蓝牙 miio_bt", "868 KB", "4K 样机 实测 840–868KB"),
        ("P2P 单客户端", "1,028 KB 视频 + 128 KB 音频", "4K 样机 实测匿名映射；块数为经验值"),
        ("NPU 池", "3,584 / 5,120 KB", "4K 系列 3.5MB；双摄-1440p 系列 5MB（init.sh cma_pool_size）"),
        ("算法模型（常电人/车）", "1,229 KB", "4K-WiFi 实测；双摄-1440p 系列已含池内，勿重复计"),
        ("MTP 默认（3 会话）", "769,280 B", "KCP 窗口公式估算，未 A/B 隔离"),
        ("海外语控 SDK", "1,024 KB / 会话", "KVS 源码常量推导，未实机重标"),
    ]
    for i, (name, val, src) in enumerate(emps):
        row = emp_start + 2 + i
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        put(ws, row, 1, name, bold=True)
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
        put(ws, row, 3, val, align="center")
        ws.merge_cells(start_row=row, start_column=5, end_row=row, end_column=14)
        put(ws, row, 5, src, size=8)
        ws.row_dimensions[row].height = _h((name, 22), (val, 22), (src, 70))

    cmd_start = emp_start + len(emps) + 3
    section(ws, cmd_start, "四、实机采集命令（新构建 / 新项目重标时用）", 14)
    cmds = [
        "① 确认 rmem：cat /proc/cmdline",
        "② 媒体拓扑：impdbg --fs_info / --enc_info（enc_info 第二个 GROUP 在 25 行之后）",
        "③ rmem 明细：cat /tmp/continuous_mem_info（SDK continuous_alloc dump）",
        "④ Linux：cat /proc/meminfo ; cat /proc/buddyinfo",
        "⑤ app：P=$(ps | awk '/[c]_mi_ipc/{print $1; exit}'); awk '/^Rss:/{R+=$2}/^Anonymous:/{A+=$2}/^Swap:/{W+=$2} END{printf \"RSS=%d anon=%d file=%d swap=%d kB\\n\",R,A,R-A,W}' /proc/$P/smaps",
        "⑥ 写 SD 卡再取，不要写 /tmp；串口不要 cat 大文件。",
    ]
    for i, s in enumerate(cmds):
        row = cmd_start + 1 + i
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=14)
        put(ws, row, 1, s, size=9)
        for cc in range(2, 15):
            ws.cell(row=row, column=cc).border = BORDER
        ws.row_dimensions[row].height = 24

    for name in wb.sheetnames:
        s = wb[name]
        s.page_setup.orientation = "landscape"
        s.page_setup.fitToWidth = 1
        s.page_setup.fitToHeight = 0
        s.sheet_properties.pageSetUpPr.fitToPage = True
        s.print_options.horizontalCentered = True
        s.page_margins.left = s.page_margins.right = 0.3
        s.page_margins.top = s.page_margins.bottom = 0.4
    wb["详细表"].freeze_panes = "A5"
    wb["详细表"].print_title_rows = "4:4"
    wb["汇总"].freeze_panes = "A5"
    wb["速查表"].freeze_panes = "A5"

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="membudget_")
    raw = os.path.join(tmpdir, os.path.basename(out_path))
    wb.save(raw)
    return raw, tmpdir, {
        "rmem": f"D{rmem_sub}",
        "linux_system": f"D{os_sub}",
        "linux_apps": f"D{app_sub}",
        "algorithm": f"D{algo_sub}",
        "other": f"D{other_sub}",
        "total": f"D{grand}",
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
    tp = result["system"]["top"]
    checks = {
        "rmem": (layout["rmem"], result["rmem"]["total"]),
        "linux_system": (layout["linux_system"], tp["linux_system"]),
        "linux_apps": (layout["linux_apps"], tp["linux_apps"]),
        "algorithm": (layout["algorithm"], tp["algorithm"]),
        "other": (layout["other"], tp["other"]),
        "total": (layout["total"], tp["total"]),
    }
    for name, (ref, want) in checks.items():
        got = wb["详细表"][ref].value
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
    if rmem.get("measured"):
        groups = OrderedDict()
        for blk in rmem.get("blocks", []):
            k = blk.get("group") or "其他"
            groups[k] = groups.get(k, 0.0) + float(blk.get("bytes", 0))
        detail = " | ".join(f"{k} {m(v):.2f}" for k, v in groups.items())
        algo_in_media = sum(v for k, v in groups.items() if k == "算法")
        rmem_line = (f"rmem : 媒体堆 used {m(rmem['total'] + algo_in_media):.2f} MB / cap "
                     f"{m(rmem['cap']):.2f} MB（{rmem['usage'] * 100:.1f}%）| {detail} "
                     f"| 计入 RMEM {m(rmem['total']):.2f} MB")
    else:
        rmem_line = (f"rmem : MDNS {mdns} | FS VB {m(rmem['fs_vb']):.2f} | "
                     f"VENC {m(sum(x['bytes'] for x in rmem['venc'])):.2f} | SDK {m(rmem['sdk_reserved']):.2f} "
                     f"= {m(rmem['total']):.2f} MB")
    lines = [
        f"项目: {r['project']} ({r['platform']})    rmem 需求: {m(rmem['need']):.2f} MB",
        rmem_line,
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
            f"系统 : DRAM {m(sy['dram']):.0f} | rmem {m(sy.get('media_cap') or sy['rmem_need']):.2f}"
            f" | Linux {m(sy['linux_total']):.2f}/{m(sy['linux_visible']):.2f}"
            f" | NPU+算法 {m(sy['npu_algo']):.2f}{'(含CMA)' if sy['inside_cma'] else ''}"
        )
        lines.append(f"预留 : 账面占用 {m(sy['system_used']):.2f} MB，账面预留 {m(sy['final_reserve']):.2f} MB"
                     f" ; 硬占用 {m(sy.get('hard_total', sy['system_used'])):.2f} MB，硬余量 {m(sy.get('hard_reserve', sy['final_reserve'])):.2f} MB")
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
        # 4K-4G 实测精度验收：rmem / MIKE SDK / miio_client 偏差 ≤1MB
        hm_path = os.path.join(HERE, "..", "templates", "spec-4k-4g-mtp.json")
        if os.path.exists(hm_path):
            with open(hm_path, encoding="utf-8") as fh:
                hm = merge(DEFAULTS, json.load(fh))
            hr = compute(hm)
            assert abs(hr["rmem"]["total"] - 24200960) < MIB, "4K-4G rmem 偏差 >1MB"
            mike_anon = hr["linux"]["dynamic"] + float(hm["fixed"].get("app_base", 0))
            assert abs(mike_anon / 1024.0 - 6652) < 1024, "4K-4G MIKE SDK 偏差 >1MB"
            assert abs(hr["system"]["apps"]["miio_total"] - 1708032) < MIB, "miio_client 偏差 >1MB"

        # 双摄-1440p-WiFi 实测：五项合计/最终预留偏差 ≤1MB
        h6_path = os.path.join(HERE, "..", "templates", "spec-dual-1440p-wifi.json")
        if os.path.exists(h6_path):
            with open(h6_path, encoding="utf-8") as fh:
                h6 = merge(DEFAULTS, json.load(fh))
            r6 = compute(h6)["system"]["top"]
            assert abs(r6["total"] - (117077760 + h6["fixed"].get("slab", 0))) < MIB, "双摄-1440p-WiFi 五项合计偏差 >1MB"
            assert abs(r6["final_reserve"] - (17139968 - h6["fixed"].get("slab", 0))) < MIB, "双摄-1440p-WiFi 最终预留偏差 >1MB"

        # SigmaStar：媒体按实测块计，算法块从 RMEM 划出，Linux 可见按 cap
        ss_path = os.path.join(HERE, "..", "templates", "spec-sigmastar-256mb.json")
        if os.path.exists(ss_path):
            with open(ss_path, encoding="utf-8") as fh:
                ss = merge(DEFAULTS, json.load(fh))
            sr = compute(ss)
            assert abs(sr["rmem"]["total"] - 115317824) < 1, "SigmaStar RMEM 块合计异常"
            assert abs(sr["system"]["top"]["algorithm"] - 5874048) < 1, "SigmaStar 算法块未从媒体堆划出"
            assert abs(sr["linux"]["visible"] - (256 * MIB - 141557760)) < 1, "SigmaStar Linux 可见未按 cap 计算"
            assert sr["system"]["top"]["rmem"] + sr["system"]["top"]["algorithm"] <= sr["rmem"]["cap"], "媒体块超 cap"

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
