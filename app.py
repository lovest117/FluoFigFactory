"""
FluoFigFactory - Flask Backend
显微荧光图像排版与分析工具
Channel Scope Studio 平替 (开源方案)

支持的格式(P1): TIFF / TIF (多页或单页)
"""
import os
import sys
import io
import re
import uuid
import math
import base64
import logging
import traceback
import threading
import webbrowser
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import tifffile
from flask import Flask, request, jsonify, send_file, render_template
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
import math

# ======== 兼容 PyInstaller frozen 模式 ========
# 当通过 PyInstaller 打包后 sys._MEIPASS 指向临时解压目录
# 此时 templates/ 与 static/ 在该目录下 (由 app.spec 的 datas 注入)
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    TEMP_DIR = os.path.join(os.environ.get('TEMP', os.path.expanduser('~')), 'FluoFigFactory')
    os.makedirs(TEMP_DIR, exist_ok=True)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    TEMP_DIR = BASE_DIR

# ======== 日志 ========
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('microscope')

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=os.path.join(BASE_DIR, 'static'))
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB

# 上传/临时文件目录: frozen 模式下放用户 TEMP, 源码模式放项目 uploads/
UPLOAD_FOLDER = os.path.join(TEMP_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 当前会话数据: session_id -> dict
session_data = {}

# ======== 通道识别规则 ========
# 按文件名关键词识别通道 (从 ChannelScope Studio 学来的)
#
# 免疫荧光主用 4 个通道槽位 (按激光波长顺序): 405 / 488 / 594 / 640
# 明场 (BF/DIC/Phase) 与 CFP/YFP 暂不启用, 仅保留文件名关键词以免误识别为未知
MAX_CHANNELS = 4
IF_CHANNEL_ORDER = ['dapi', 'gfp', 'rfp', 'cy5']   # 默认槽位顺序 (必须 4 项)

CHANNEL_KEYWORDS = {
    # 蓝色 - DAPI / Hoechst / 405
    'dapi': {'name': 'DAPI', 'color': '#0000FF', 'rgb': (0, 0, 255), 'laser': '405',
             'alias': ['dapi', 'hoechst', '405', 'blue']},
    # 绿色 - GFP / FITC / 488 / ayn488 (α-syn 488)
    'gfp':  {'name': 'GFP',  'color': '#00FF00', 'rgb': (0, 255, 0), 'laser': '488',
             'alias': ['gfp', 'fitc', 'alexa488', '488', 'green', 'ayn488', 'syn488']},
    # 红色 - RFP / mCherry / 561 / 594 / hucd594
    'rfp':  {'name': 'RFP',  'color': '#FF0000', 'rgb': (255, 0, 0), 'laser': '594',
             'alias': ['rfp', 'mcherry', 'texas', '561', '594', 'red', 'cy3', 'hucd594', 'alexa594']},
    # 远红 - Cy5 / 640
    'cy5':  {'name': 'Cy5',  'color': '#FF00FF', 'rgb': (255, 0, 255), 'laser': '640',
             'alias': ['cy5', 'alexa647', '640', 'far-red', 'magenta']},
    # ---- 以下仅作文件名兼容识别, 不占用默认通道槽位 ----
    # 明场
    'bf':   {'name': 'BF',   'color': '#FFFFFF', 'rgb': (255, 255, 255), 'laser': None,
             'alias': ['bf', 'bright', 'dic', 'phase', 'ph']},
    # 青色
    'cyan': {'name': 'CFP',  'color': '#00FFFF', 'rgb': (0, 255, 255), 'laser': '458',
             'alias': ['cfp', 'cyan']},
    # 黄色
    'yel':  {'name': 'YFP',  'color': '#FFFF00', 'rgb': (255, 255, 0), 'laser': '514',
             'alias': ['yfp', 'yellow']},
}

# RGB 伪彩导出: 平面索引 → (名称, 颜色) —— R 平面必须配红, 不能用默认槽位顺序
# 蓝平面 (索引2) 默认名 DAPI: 免疫荧光里蓝通道即核染, 与「405nm 一律 DAPI」约定一致;
# 该名同时用于界面通道名与导出面板标签, 用户可在界面改名。
PLANE_NAMES = ['R', 'G', 'DAPI']
PLANE_COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]

# Olympus FV3000 UPLXAPO 10× 全幅视野 (权威值, 取自 10×/2048 面板 X Dimension = 1272.792 μm)
FOV_10X_NATIVE_UM = 1272.792


def fv3000_pixel_size_um(magnification, image_width, zoom_size=1.0):
    """按 FV3000 标定公式换算 1 px = ? μm。

    μm/px = (FOV_10X_NATIVE_UM × 10 / mag) / image_width / zoom
    zoom=1 默认; >1 缩小视野 (如误设 zoom=2 → FOV 减半)。
    返回 None 表示参数无效 (调用方应保留原 pixel_size_um)。
    """
    try:
        m = float(magnification)
        w = float(image_width)
        z = float(zoom_size) if zoom_size else 1.0
        if m <= 0 or w <= 0 or z <= 0:
            return None
        return (FOV_10X_NATIVE_UM * 10.0 / m) / w / z
    except (TypeError, ValueError):
        return None

# FV3000 通道编号 → 默认 (name, color, rgb)
FV3000_CHANNEL_MAP = {
    1: ('DAPI', '#0000FF', (0, 0, 255)),     # C001 = 蓝
    2: ('GFP',  '#00FF00', (0, 255, 0)),     # C002 = 绿
    3: ('RFP',  '#FF0000', (255, 0, 0)),     # C003 = 红
    4: ('Cy5',  '#FF00FF', (255, 0, 255)),   # C004 = 紫
}

# FV3000 通道槽位编号 → 激光波长 (nm): C001=405蓝 / C002=488绿 / C003=594红 / C004=640远红
FV3000_SLOT_WAVELENGTH = {1: 405, 2: 488, 3: 594, 4: 640}

# 波长 (nm) → 默认通道名/颜色 (从文件名解析波长数字时使用, 如 `asyn488` → 488nm)
WAVELENGTH_CHANNEL_MAP = {
    405: ('DAPI',  '#0000FF', (0, 0, 255)),
    488: ('GFP',   '#00FF00', (0, 255, 0)),
    561: ('RFP',   '#FF0000', (255, 0, 0)),
    # 594 一律红色: 与 FV3000 C003 槽位及 CHANNEL_KEYWORDS['rfp'] 保持一致 (用户约定, 勿改回橙色)
    594: ('TRITC', '#FF0000', (255, 0, 0)),
    640: ('Cy5',   '#FF00FF', (255, 0, 255)),
}


def _detect_fv3000_channel(filename):
    """检测 FV3000 文件名中的通道编号 _C001T001 / _C001 → 返回 (ch_num, name, color, rgb) 或 None"""
    m = re.search(r'_C(\d{3})T?\d*', filename, re.IGNORECASE)
    if m:
        ch_num = int(m.group(1))
        if ch_num in FV3000_CHANNEL_MAP:
            name, color, rgb = FV3000_CHANNEL_MAP[ch_num]
            return (ch_num, name, color, rgb)
        return (ch_num, f'Ch{ch_num}', '#FFFFFF', (255, 255, 255))
    return None


def _rgb_live_planes(data):
    """从单页 RGB 伪彩图里取出「有信号」的平面。

    FV3000 把一次采集的 N 个通道导出为**单页 RGB 伪彩 TIFF**:
    启用的通道写进对应平面并着伪彩, 未启用的通道平面几乎全 0 (仅残留极少量噪点)。
    因此「N 通道 TIFF」= 该文件里有 N 个平面有信号, 而不是 N 个文件。

    返回: (live_arrays, live_indices)
      - live_arrays  : 有信号的平面数组列表 (可能 1~3 个)
      - live_indices : 对应 R=0 / G=1 / B=2, 供上层按物理平面配色
    全部平面都为空 (整图全黑) 时退回全部平面, 避免把文件判成 0 通道。
    """
    if data.ndim != 3 or data.shape[2] < 3:
        return None, None
    planes = [data[:, :, i] for i in range(3)]
    stats = [channel_activity(p) for p in planes]
    live_idx = [i for i, s in enumerate(stats) if not s['is_dead']]
    if not live_idx:
        log.warning('  RGB 三个平面均无信号 (整图全黑), 保留原始 3 平面')
        live_idx = [0, 1, 2]
    log.info('  RGB 平面活跃度: ' + ', '.join(
        '%s=%s(非零%.2f%%, 唯一值%d)' % (
            PLANE_NAMES[i], '有信号' if not s['is_dead'] else '空',
            s['nonzero_ratio'] * 100.0, s['unique_count'])
        for i, s in enumerate(stats)))
    return [planes[i].copy() for i in live_idx], live_idx


def _rgb_to_single_channel(data):
    """兼容旧接口: 检测 RGB 数据是否实际只包含 1 个有信号的通道。

    返回: (单通道数组, RGB 位置 0=R/1=G/2=B) 或 (None, None)
    """
    live, idx = _rgb_live_planes(data)
    if live is not None and len(live) == 1:
        return live[0], idx[0]
    return None, None


def guess_channel_from_filename(filename):
    """根据文件名猜测通道类型, 优先 FV3000 模式"""
    fn_lower = filename.lower()
    base = os.path.splitext(fn_lower)[0]

    # 1) FV3000: _C001T001 / _C001
    fv = _detect_fv3000_channel(filename)
    if fv:
        return ('fv3000', fv)

    # 2) Merge / Composite / FV3000 的 _T001 (无 C00X)
    if '_t001' in base and '_c0' not in base:
        return 'merge', None
    if any(kw in base for kw in ['merge', 'composite', 'merged', 'overlay']):
        return 'merge', None

    # 3) 关键词匹配
    for key, info in CHANNEL_KEYWORDS.items():
        for alias in info['alias']:
            if re.search(r'(^|[_\-\s\.\(\)])' + re.escape(alias) + r'($|[_\-\s\.\(\)])', base):
                return key, info
    return None, None


def parse_filename_metadata(filename):
    """从文件名解析倍数 + 通道(名+波长) 提示信息, 供 UI 预填。

    设计目标: 匹配 FV3000 用户常见的命名风格, 如
      `sham4-6ileum2.2-20X-asyn488chat594.tif.frames.tiff`
        → magnification=20, channels=[(asyn,488),(chat,594)]

    解析规则:
      - magnification: `(\\d+)X` / `(\\d+)x` (大小写不敏感), 必须是 1~3 位整数且 ≤99
        (避免把 `sham4-6ileum2.2` 里的孤立 1 位数字误判为倍数)
      - channel: `([a-zA-Z]+)(\\d{3})` → (name, wavelength)
        - 3 位连续数字才视为波长 (405/488/561/594/640 等)
        - 自动忽略样本号 `sham4` / `ileum2` (数字非 3 位, 或后面没接字母)
      - wavelength 不在 WAVELENGTH_CHANNEL_MAP 表里时, 该通道仅返回名字, 不强配色

    返回 dict: {'magnification': int|None, 'channels': [...], 'raw_matches': [...]}
    """
    if not filename:
        return {'magnification': None, 'channels': [], 'raw_matches': []}
    base = os.path.splitext(os.path.basename(filename))[0]
    # `.frames.tiff` 这种多扩展名再剥一次
    for ext in ('.frames', '.frame', '.max', '.mean'):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]

    # 1) 倍数: 必须有 X/x 后缀, 避免误判 sham4 / ileum2
    magnification = None
    mag_m = re.search(r'(\d{1,3})[xX]', base)
    if mag_m:
        mag_val = int(mag_m.group(1))
        if 1 <= mag_val <= 99:
            magnification = mag_val

    # 2) 通道识别
    #    优先级: FV3000 槽位 _C00X (决定波长/颜色, 最可靠) > 文件名 字母+3位波长
    #    关键校正: FV3000 文件名 `xxx_C001T001` 中 T001 是「扫描序号」而非通道,
    #    其 3 位数字(1)会被误当波长 → UI 显示为 "T (1nm)"; 必须忽略, 蓝色槽位固定 DAPI。
    channels = []
    raw_matches = []
    seen = set()

    # 2a) FV3000 槽位 (_C001 / _C002 ...): 决定波长与名称/颜色
    fv = _detect_fv3000_channel(filename)
    if fv:
        ch_num, fv_name, fv_color, fv_rgb = fv
        wavelength = FV3000_SLOT_WAVELENGTH.get(ch_num)
        if wavelength:
            channels.append({
                'name': fv_name, 'wavelength': wavelength,
                'color': fv_color, 'rgb': fv_rgb, 'default_name': fv_name,
            })
            raw_matches.append((fv_name, wavelength))
            seen.add(('__fv__', wavelength))

    # 2b) 通用 字母+3位数字 波长识别 (asyn488 / chat594 / 405 等)
    for m in re.finditer(r'([A-Za-z]+)(\d{3})', base):
        name = m.group(1)
        wavelength = int(m.group(2))
        # 跳过 FV3000 内部编号 (C001 / T001 等扫描槽位码):
        #   单字母 c/t + 低槽位号(≤9); 真实波长 405/488/594/640 均 >9, 不会被误杀
        if len(name) == 1 and name.lower() in ('c', 't') and wavelength <= 9:
            continue
        key = (name.lower(), wavelength)
        if key in seen:
            continue
        seen.add(key)
        raw_matches.append((name, wavelength))
        info = WAVELENGTH_CHANNEL_MAP.get(wavelength)
        if info:
            default_name, color, rgb = info
            if wavelength == 405:
                # 用户规则 (2026-09-16): 405/蓝色通道一律默认 DAPI, 不采纳文件名字母
                # (如 `t405` 曾被误识别为通道 "T"; FV3000 T001 扫描序号亦忽略)
                name = default_name
            channels.append({
                'name': name, 'wavelength': wavelength,
                'color': color, 'rgb': rgb, 'default_name': default_name,
            })
        else:
            # 波长未知, 仅给名字
            channels.append({
                'name': name, 'wavelength': wavelength,
                'color': None, 'rgb': None, 'default_name': None,
            })

    return {
        'magnification': magnification,
        'channels': channels,
        'raw_matches': raw_matches,
    }


def default_channel_config(idx):
    """根据索引返回默认通道配置

    槽位顺序固定为免疫荧光 4 通道 (405→488→594→640), 超过 4 个时循环取值,
    避免出现第 5 通道变成白色无名通道。明场/相差不参与默认槽位。
    """
    if IF_CHANNEL_ORDER:
        info = CHANNEL_KEYWORDS[IF_CHANNEL_ORDER[idx % len(IF_CHANNEL_ORDER)]]
        cycle = idx // len(IF_CHANNEL_ORDER)
        name = info['name'] if cycle == 0 else f"{info['name']}{cycle + 1}"
        return {'name': name, 'color': info['color'], 'rgb': info['rgb'],
                'visible': True, 'brightness': 1.0, 'contrast': 1.0,
                'black': 0, 'white': 255, 'gamma': 1.0}
    return {'name': f'通道 {idx+1}', 'color': '#FFFFFF', 'rgb': (255, 255, 255),
            'visible': True, 'brightness': 1.0, 'contrast': 1.0,
            'black': 0, 'white': 255, 'gamma': 1.0}


# ======== 空通道(未启用通道)检测 ========
# FV3000 把「N 通道」导出为单页 RGB 伪彩 TIFF 时, 未启用的通道平面几乎全 0,
# 但会残留极少量 0/255 噪点。实测标定 (2048×2048 uint8):
#     活通道: 非零像素占比 38%~100%, 抽样唯一值数 116~256
#     空通道: 非零像素占比 ≤0.09%,   抽样唯一值数 1~2
# ⚠️ 不能用 dynamic range (ptp) / 方差 / MAD 判定: 空通道的 ptp 可达 255,
#    会把空通道误判为活通道 (已实测踩坑, 勿改回)。
# 判据用「与」而非「或」, 宁可漏杀也不误杀真实弱信号通道。
DEAD_NONZERO_RATIO = 0.005      # 非零像素占比 < 0.5%
DEAD_UNIQUE_MAX = 8             # 且抽样唯一值数 ≤ 8  → 判为空通道
ACTIVITY_SAMPLE_TARGET = 250000  # 唯一值统计的抽样点数


def channel_activity(data):
    """统计单个通道的活跃度, 用于识别未启用/空通道。

    返回 dict: nonzero_ratio / unique_count / min / max / is_dead
    """
    d = np.asarray(data)
    if d.size == 0:
        return {'nonzero_ratio': 0.0, 'unique_count': 0, 'min': 0, 'max': 0, 'is_dead': True}
    ratio = float(np.count_nonzero(d)) / float(d.size)
    # 唯一值数在抽样网格上统计: 4M 像素全量 np.unique 太慢, 抽样 25 万点已足够区分
    step = max(1, int(math.sqrt(d.size / float(ACTIVITY_SAMPLE_TARGET))))
    sample = d[::step, ::step] if d.ndim >= 2 else d[::step]
    uniq = int(np.unique(sample).size)
    return {
        'nonzero_ratio': ratio,
        'unique_count': uniq,
        'min': int(d.min()),
        'max': int(d.max()),
        'is_dead': bool(ratio < DEAD_NONZERO_RATIO and uniq <= DEAD_UNIQUE_MAX),
    }


def is_dead_channel(data):
    """该通道是否为未启用/空通道 (无有效色阶波动)"""
    return channel_activity(data)['is_dead']


# ======== 图像处理核心 ========

def normalize_channel(data, black=0, white=255, gamma=1.0, brightness=1.0, contrast=1.0):
    """将通道数据归一化到 0-255，应用色阶(black/white/gamma)与亮度对比度。

    色阶作用于已线性归一到 0-255 的显示空间:
      - black/white: 把 [black, white] 映射为 [0, 255] (低于 black 变黑, 高于 white 变白)
      - gamma: 中间调 (gamma>1 变亮, <1 变暗)
    """
    d = data.astype(np.float64)
    dmin, dmax = d.min(), d.max()
    if dmax - dmin < 1e-9:
        return np.full(d.shape, 128, dtype=np.uint8)
    # 1) 线性归一到 0-255 显示空间
    d = (d - dmin) / (dmax - dmin) * 255.0
    # 2) 色阶: 把 [black, white] 映射为 [0, 255]
    black = float(black); white = float(white)
    if white <= black:
        white = black + 1.0
    d = (d - black) / (white - black) * 255.0
    d = np.clip(d, 0.0, 255.0)
    # 3) Gamma 中间调 (gamma>1 提亮, <1 压暗)
    gamma = float(gamma)
    if gamma != 1.0 and gamma > 0:
        d = 255.0 * np.power(d / 255.0, 1.0 / gamma)
    # 4) 亮度/对比度 (兼容保留, 默认 1.0 无操作)
    d = d * brightness
    d = (d - 128.0) * contrast + 128.0
    return np.clip(d, 0, 255).astype(np.uint8)


def _read_tiff_page_arrays(tif, filepath):
    """尽力用多种后端读取 TIFF 页, 返回 (pages, errors)。

    策略优先级:
      1. tifffile series (最稳健, 自动展开 ImageJ/OME/Z-stack 为多维数组)
      2. 逐页 page.asarray()    (兼容无 series 的场景)
      3. PIL fallback           (内置 libtiff, 不依赖 imagecodecs, 覆盖多数 LZW/无压缩 TIFF)
    任一策略成功即返回; 全部失败则返回累积的错误信息, 便于暴露真实解码原因。
    """
    errors = []

    # 策略1: 逐页 asarray —— 多页独立通道 TIFF 最可靠 (如 FV3000 多页 16-bit /
    #        ImageJ 多页), 每个 page 即一个 2D 通道, 由调用方负责按维度拆通道。
    #        注意: 多页 TIFF 在 tifffile 中会被识别为「多个独立 series」, 若直接
    #        取 series[0] 会只得到第一页, 故逐页遍历才是正确做法。
    pages = []
    for idx, page in enumerate(tif.pages):
        try:
            comp = getattr(page, 'compression', None)
            p = page.asarray()
            if p is not None and getattr(p, 'size', 0) > 0:
                pages.append(p)
            else:
                errors.append(f'page[{idx}] 返回空数组 (compression={comp})')
        except Exception as e:
            errors.append(f'page[{idx}] 解码失败 compression={comp}: {type(e).__name__}: {e}')
    if pages:
        return pages, []

    # 策略2: series —— 逐页为空时, 处理单 series 多维 (ImageJ/OME/Z-stack 等
    #        被聚合为单个多维数组的情形)
    try:
        for s in tif.series:
            try:
                arr = s.asarray()
                if arr is not None and getattr(arr, 'size', 0) > 0:
                    return [arr], []
            except Exception as e:
                errors.append(f'series.asarray: {type(e).__name__}: {e}')
    except Exception as e:
        errors.append(f'series 枚举失败: {type(e).__name__}: {e}')

    # 策略3: PIL fallback (内置 libtiff, 无需 imagecodecs 即可解 LZW/无压缩)
    try:
        from PIL import Image
        im = Image.open(filepath)
        pils = []
        for i in range(int(getattr(im, 'n_frames', 1))):
            try:
                im.seek(i)
                pils.append(np.asarray(im))
            except Exception as e:
                errors.append(f'PIL 帧[{i}] 失败: {type(e).__name__}: {e}')
        if pils:
            return pils, []
    except Exception as e:
        errors.append(f'PIL 打开失败: {type(e).__name__}: {e}')

    return [], errors


def _parse_fv3000_metadata(text):
    """从 FV3000 ImageDescription 提取关键采集参数。
    - Magnification: 物镜倍数 (如 10X → 10.0)
    - Zoom Size: 数字 zoom (默认 1.0; >1 缩小 FOV)
    - Objective Lens: 物镜型号 (如 UPLXAPO10X)
    - pixel_size_um_from_metadata: 优先使用的 μm/px (来自 X Dimension 第 3 列)
    返回 dict, 缺字段为 None
    """
    if not text:
        return {}
    import re
    out = {}
    m = re.search(r'Magnification\s*=\s*(\d+(?:\.\d+)?)\s*X?', text)
    if m:
        out['magnification'] = float(m.group(1))
    m = re.search(r'Zoom Size\s*=\s*([\d.]+)', text)
    if m:
        out['zoom_size'] = float(m.group(1))
    m = re.search(r'Objective Lens\s*=\s*(\S+)', text)
    if m:
        out['objective_lens'] = m.group(1)
    # X Dimension= N, A - B [um], C [u...   C 即 μm/px
    m = re.search(r'X Dimension\s*=\s*\d+\s*,\s*[\d.\-]+\s*-\s*[\d.\-]+\s*\[um\]\s*,\s*([\d.]+)\s*\[u', text)
    if m:
        out['pixel_size_um_from_metadata'] = float(m.group(1))
    return out


def read_tiff_channels(filepath, target_filename=''):
    """
    读取 TIFF 文件，返回通道列表和元数据
    支持: 单页灰度、多页灰度(C,H,W)、RGB(H,W,3)、RGBA、Z-stack
    """
    with tifffile.TiffFile(filepath) as tif:
        # 提取像素尺寸
        pixel_size_um = None
        fv3000_meta = {}
        for page in tif.pages:
            tags = page.tags
            try:
                # FV3000 ImageDescription 优先 (含 Magnification / Zoom Size / 真实 μm/px)
                if 'ImageDescription' in tags:
                    desc_text = tags['ImageDescription'].value
                    if desc_text and isinstance(desc_text, str):
                        fv3000_meta = _parse_fv3000_metadata(desc_text)
                        if 'pixel_size_um_from_metadata' in fv3000_meta:
                            pixel_size_um = fv3000_meta['pixel_size_um_from_metadata']
                # fallback 到 TIFF ResolutionUnit (FV3000 这个值经常错误, 仅作最后兜底)
                if pixel_size_um is None and 'ResolutionUnit' in tags and 'XResolution' in tags:
                    res_unit = tags['ResolutionUnit'].value
                    x_res = tags['XResolution'].value
                    if isinstance(x_res, tuple):
                        x_res = x_res[0] / max(x_res[1], 1)
                    if res_unit == 3:  # CENTIMETER
                        pixel_size_um = 10000.0 / x_res if x_res > 0 else None
                    elif res_unit == 2:  # INCH
                        pixel_size_um = 25400.0 / x_res if x_res > 0 else None
            except Exception:
                continue
            if pixel_size_um or fv3000_meta:
                break

        # 分层读取所有页 (tifffile series -> 逐页 asarray -> PIL fallback);
        # 不再静默吞掉解码异常, 失败时将真实原因纳入报错, 便于定位压缩/编码问题
        pages, read_errors = _read_tiff_page_arrays(tif, filepath)
        if not pages:
            detail = read_errors[0] if read_errors else '未知原因 (无可用页面)'
            raise ValueError(f'TIFF 文件无有效页面 (无法解码): {detail}')

        data = pages[0] if len(pages) == 1 else np.stack(pages, axis=0)
        channels = []
        color_type = 'gray'   # 'gray' | 'rgb' | 'rgba' — 原始页面是否为彩色平面 (用于校正通道配色)
        rgb_planes = None     # RGB 伪彩来源: 每个通道对应的物理平面索引 (0=R/1=G/2=B)
        rgb_empty_planes = []  # RGB 里被判定无信号的平面 [(plane_idx, array)], 可恢复

        if data.ndim == 2:
            # 单页灰度
            channels = [data]
            h, w = data.shape
        elif data.ndim == 3:
            # 可能是 (C, H, W) 多页 / (H, W, C) RGB / (Z, H, W) Z-stack
            if data.shape[0] <= 8 and data.shape[0] < min(data.shape[1], data.shape[2]):
                # (C, H, W) - 通道在第一维
                for i in range(data.shape[0]):
                    channels.append(data[i])
                h, w = data.shape[1], data.shape[2]
            elif data.shape[2] <= 4:
                # (H, W, C) - RGB/RGBA
                # FV3000「N 通道」伪彩导出: 未启用的通道平面几乎全 0 → 只保留有信号的平面
                # (RGBA 的 A 平面不参与: alpha 通常为全 255, 会被误当成一个通道)
                live, live_idx = _rgb_live_planes(data)
                if live is None:
                    live = [data[:, :, i] for i in range(data.shape[2])]
                    live_idx = list(range(min(3, data.shape[2])))
                channels = live
                rgb_planes = live_idx
                # 只有 1 个平面有信号 → 实际是单通道图, 不能按 merge 配色
                if len(channels) > 1:
                    color_type = 'rgb'
                # 被判定无信号的平面: 不进通道列表, 但登记为「可恢复」, 便于用户一键找回
                rgb_empty_planes = [(i, data[:, :, i].copy())
                                    for i in range(3) if i not in live_idx]
                h, w = data.shape[0], data.shape[1]
            else:
                # 当作 (Z, H, W)，取中位数投影
                proj = np.median(data, axis=0).astype(data.dtype)
                channels = [proj]
                h, w = proj.shape
        elif data.ndim == 4:
            # (Z, C, H, W) 或 (C, Z, H, W) 或 (T, H, W, C)
            # 取第一个非空切片
            if data.shape[-1] <= 4 and data.shape[-1] < data.shape[-2]:
                # (T/Z, H, W, C)
                arr = data[0]
                color_type = 'rgba' if arr.shape[2] == 4 else 'rgb'
                for i in range(arr.shape[2]):
                    channels.append(arr[:, :, i])
                h, w = arr.shape[0], arr.shape[1]
            elif data.shape[1] <= 4 and data.shape[1] < data.shape[2]:
                # (Z, C, H, W)
                for i in range(data.shape[1]):
                    channels.append(data[0, i])
                h, w = data.shape[2], data.shape[3]
            else:
                # 取中间帧
                mid = data.shape[0] // 2
                arr = data[mid]
                for i in range(arr.shape[0]):
                    channels.append(arr[i])
                h, w = arr.shape[1], arr.shape[2]
        else:
            raise ValueError(f'不支持的 TIFF 维度: {data.ndim}D, shape={data.shape}')

        if not channels:
            raise ValueError('无法从 TIFF 提取通道')

        return channels, {
            'width': int(w), 'height': int(h),
            'num_channels': len(channels),
            'pixel_size_um': pixel_size_um,
            'color_type': color_type,
            'rgb_planes': rgb_planes,
            'rgb_empty_planes': rgb_empty_planes,
            'source_file': os.path.basename(filepath),
            'magnification': fv3000_meta.get('magnification'),
            'zoom_size': fv3000_meta.get('zoom_size', 1.0),
            'objective_lens': fv3000_meta.get('objective_lens'),
            'pixel_size_source': 'fv3000_metadata' if fv3000_meta else 'tiff_resolution',
        }


def split_rgb_to_channels(rgb_array, filename=''):
    """把 RGB 图拆为 3 个独立通道 (R/G/B)"""
    if rgb_array.ndim == 2:
        return [rgb_array]
    if rgb_array.ndim == 3:
        if rgb_array.shape[2] == 4:  # RGBA
            return [rgb_array[:, :, 0], rgb_array[:, :, 1], rgb_array[:, :, 2]]
        if rgb_array.shape[2] >= 3:
            return [rgb_array[:, :, 0], rgb_array[:, :, 1], rgb_array[:, :, 2]]
    return [rgb_array]


def create_merge_image(channel_data_list, channel_configs, height, width, bg=(0, 0, 0)):
    """根据通道配置创建合并 RGB 图像 (应用每个通道的色阶 black/white/gamma + 亮度/对比度)"""
    merged = np.zeros((height, width, 3), dtype=np.float64)
    for ch_idx, (data, config) in enumerate(zip(channel_data_list, channel_configs)):
        if not config.get('visible', True):
            continue
        normalized = normalize_channel(
            data,
            brightness=config.get('brightness', 1.0),
            contrast=config.get('contrast', 1.0),
            black=config.get('black', 0),
            white=config.get('white', 255),
            gamma=config.get('gamma', 1.0),
        ).astype(np.float64)
        rgb = config.get('rgb', (255, 255, 255))
        for c in range(3):
            merged[:, :, c] += normalized * (rgb[c] / 255.0)
    merged = np.clip(merged, 0, 255).astype(np.uint8)
    return Image.fromarray(merged, mode='RGB')


def pt_to_px(pt_val, dpi=72):
    """pt → px 换算 (1pt = 1/72 inch)。dpi=72 时 1pt=1px (预览); dpi=300/600 时按比例放大 (导出)"""
    try:
        return max(1, int(round(float(pt_val) * float(dpi) / 72.0)))
    except (TypeError, ValueError):
        return 1


def compute_scale_bar_font_pt(bar_length_um, pixel_size_um, ratio=0.3):
    """按 bar 长度自动算字号 (pt), 与 Illustrator "字号与 bar 长度相当" 的习惯一致。
    比例 0.3 → 50μm bar @ 0.31 μm/px ≈ 161px 长的 bar 对应 ~48pt 字号 (72 DPI 下约 48px)。
    返回 None 表示无法计算 (无像素尺寸或参数无效)。"""
    if not pixel_size_um or pixel_size_um <= 0 or not bar_length_um or bar_length_um <= 0:
        return None
    bar_px_at_72dpi = bar_length_um / pixel_size_um   # 72 DPI 下 1pt = 1px
    return max(6.0, round(bar_px_at_72dpi * ratio, 1))


def add_scale_bar(img, pixel_size_um, bar_length_um, position='bottom-right',
                  bar_color='white', thickness_pt=1.0, font_size_pt=None,
                  dpi=72):
    """在图像上添加 Scale bar。

    v1.4.0 起改用 pt 为单位 (Illustrator 习惯, 默认 1pt 厚度), 渲染时按 dpi 换算:
      - 前端预览 (native resolution, dpi=72): 1pt = 1px
      - 导出 PNG/TIFF @ 300 DPI: 1pt ≈ 4.17px (线条视觉更粗, 符合打印预期)
      - 导出 PDF/SVG: 字号/线宽已在矢量层独立指定, 这里不参与
    font_size_pt=None → 自动按 bar 长度算 (compute_scale_bar_font_pt)
    """
    img = img.copy()
    draw = ImageDraw.Draw(img)
    w, h = img.size

    # pt → px
    thickness_px = pt_to_px(thickness_pt, dpi)
    if font_size_pt is None:
        font_size_pt = compute_scale_bar_font_pt(bar_length_um, pixel_size_um) or 12.0
    font_size_px = pt_to_px(font_size_pt, dpi)

    if pixel_size_um and pixel_size_um > 0:
        bar_px = max(int(bar_length_um / pixel_size_um), thickness_px + 2)
    else:
        bar_px = int(bar_length_um)

    try:
        font = ImageFont.truetype("arial.ttf", font_size_px)
    except Exception:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", font_size_px)
        except Exception:
            font = ImageFont.load_default()

    label = f"{bar_length_um} μm"
    margin = max(15, font_size_px + 5)
    if position == 'bottom-right':
        x_start = w - bar_px - margin
        y_start = h - margin - thickness_px - font_size_px - 5
    elif position == 'bottom-left':
        x_start = margin
        y_start = h - margin - thickness_px - font_size_px - 5
    elif position == 'top-right':
        x_start = w - bar_px - margin
        y_start = margin
    elif position == 'top-left':
        x_start = margin
        y_start = margin
    else:
        x_start = w - bar_px - margin
        y_start = h - margin - thickness_px - font_size_px - 5

    bar_y = y_start + font_size_px + 5
    draw.rectangle([x_start, bar_y, x_start + bar_px, bar_y + thickness_px], fill=bar_color)
    draw.text((x_start, y_start), label, fill=bar_color, font=font)
    return img


def extract_line_profile(channel_data, x1, y1, x2, y2, num_points=None):
    """沿两点之间的线提取像素值 (双线性插值)"""
    h, w = channel_data.shape
    dx = x2 - x1
    dy = y2 - y1
    length = math.sqrt(dx*dx + dy*dy)
    if length == 0:
        return {'positions': [0], 'values': [float(channel_data[int(y1), int(x1)])]}

    if num_points is None:
        num_points = int(length)

    positions, values = [], []
    for i in range(num_points + 1):
        t = i / max(num_points, 1)
        x = x1 + t * dx
        y = y1 + t * dy
        x0, y0 = int(x), int(y)
        x1i, y1i = min(x0 + 1, w - 1), min(y0 + 1, h - 1)
        fx, fy = x - x0, y - y0
        if 0 <= x0 < w and 0 <= y0 < h:
            val = (channel_data[y0, x0] * (1-fx) * (1-fy) +
                   channel_data[y0, x1i] * fx * (1-fy) +
                   channel_data[y1i, x0] * (1-fx) * fy +
                   channel_data[y1i, x1i] * fx * fy)
            positions.append(i)
            values.append(float(val))

    return {'positions': positions, 'values': values}


def calculate_pearson(ch1_data, ch2_data, roi=None):
    """计算两通道之间的 Pearson 相关系数
    纯 numpy + math 实现, 避免打包 scipy (其懒加载链会触发 numpy.testing → pydoc, PyInstaller 难处理)
    p 值通过 Student-t 双尾检验 + 不完全 Beta 函数计算
    """
    if roi:
        # 前端传来的是画布坐标系(可能为浮点), 必须转 int 并夹到图像边界
        H, W = ch1_data.shape[:2]
        x = int(round(float(roi['x'])))
        y = int(round(float(roi['y'])))
        w = int(round(float(roi['width'])))
        h = int(round(float(roi['height'])))
        x = max(0, min(x, W - 1))
        y = max(0, min(y, H - 1))
        w = max(1, min(w, W - x))
        h = max(1, min(h, H - y))
        m1 = ch1_data[y:y+h, x:x+w].flatten()
        m2 = ch2_data[y:y+h, x:x+w].flatten()
    else:
        m1 = ch1_data.flatten()
        m2 = ch2_data.flatten()

    nonzero = (m1 > 0) | (m2 > 0)
    m1 = m1[nonzero].astype(np.float64)
    m2 = m2[nonzero].astype(np.float64)
    n = len(m1)
    if n < 2:
        return {'pearson_r': 0.0, 'p_value': 1.0, 'n_pixels': int(n)}

    # Pearson r = sum((x-xm)(y-ym)) / sqrt(sum((x-xm)^2) * sum((y-ym)^2))
    xm = m1.mean()
    ym = m2.mean()
    dx = m1 - xm
    dy = m2 - ym
    num = float((dx * dy).sum())
    den = float(np.sqrt((dx * dx).sum() * (dy * dy).sum()))
    if den == 0.0:
        return {'pearson_r': 0.0, 'p_value': 1.0, 'n_pixels': int(n)}
    r = num / den
    r = max(-1.0, min(1.0, r))  # 防数值误差

    # 双尾 p 值 (Student-t 检验, df = n-2)
    if n < 3 or abs(r) >= 1.0:
        p = 0.0
    else:
        t = abs(r) * math.sqrt((n - 2) / (1.0 - r * r))
        df = n - 2
        # survival function 1 - CDF(t, df) = 0.5 * I_{df/(df+t^2)}(df/2, 0.5)
        xb = df / (df + t * t)
        p = _betainc_regularized(df / 2.0, 0.5, xb)

    return {'pearson_r': float(r), 'p_value': float(p), 'n_pixels': int(n)}


def _betainc_regularized(a, b, x, max_iter=200, eps=1e-12):
    """正则化不完全 Beta 函数 I_x(a, b) — 用连分式展开"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - lbeta) / a
    return front * _betacf(a, b, x, max_iter=max_iter, eps=eps)


def _betacf(a, b, x, max_iter=200, eps=1e-12):
    """不完全 Beta 连分式 (Numerical Recipes 6.4)"""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300: d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300: c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300: d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300: c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


# ======== Base64 helpers ========
def array_to_base64(arr):
    img = Image.fromarray(arr.astype(np.uint8), mode='L')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def image_to_base64(img):
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('utf-8')


# ======== 全局错误处理: 永远返回 JSON ========
@app.errorhandler(400)
def bad_request(e):
    return jsonify({'error': f'请求格式错误: {str(e)}'}), 400


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': f'接口不存在: {request.path}'}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({'error': f'方法不允许: {request.method} {request.path}'}), 405


@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': '文件过大 (限制 500MB)'}), 413


@app.errorhandler(500)
def server_error(e):
    log.error(f'500 error: {e}\n{traceback.format_exc()}')
    return jsonify({'error': f'服务器内部错误: {str(e)}'}), 500


@app.errorhandler(Exception)
def all_exceptions(e):
    # 任何未捕获的异常都返回 JSON
    log.error(f'Uncaught exception: {e}\n{traceback.format_exc()}')
    return jsonify({'error': f'异常: {type(e).__name__}: {str(e)}'}), 500


# ======== 路由 ========
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'version': '1.4.5', 'formats': ['tiff', 'tif']})


@app.route('/api/set_pixel_size', methods=['POST'])
def set_pixel_size_api():
    """更新当前会话的 pixel_size_um (μm/px)。
    用户来源: 手动输入 / 物镜倍数自动换算 / 保存的默认值。
    同步刷新后端 sd['metadata']['pixel_size_um'], 后续 add_scale_bar / export 等接口优先采用。
    """
    data = request.json or {}
    session_id = data.get('session_id')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在'}), 404
    val = data.get('pixel_size_um')
    try:
        val_f = float(val)
    except (TypeError, ValueError):
        return jsonify({'error': 'pixel_size_um 必须是正数'}), 400
    if val_f <= 0 or val_f > 1000:
        return jsonify({'error': 'pixel_size_um 需在 (0, 1000] μm/px 范围内'}), 400
    sd = session_data[session_id]
    sd['metadata']['pixel_size_um'] = val_f
    sd['metadata']['pixel_size_source'] = data.get('source', 'manual')
    return jsonify({'ok': True, 'pixel_size_um': val_f, 'source': sd['metadata']['pixel_size_source']})


def _process_uploaded_files(file_list, filenames, channel_count=None):
    """
    通用上传处理: 接收 (fileobj, filename) 列表
    - 智能识别: 多页 TIFF 保留多通道; RGB TIFF 拆 3 通道
    - 通道命名: 优先用文件名关键词猜测
    - channel_count: 用户预先选择的通道数 (先选通道 → 导入文件夹 → 自动识别)
    - 返回会话数据
    """
    if not file_list:
        raise ValueError('未提供文件')

    all_channels = []  # (data, suggested_name, suggested_color_info)
    pixel_size_um = None
    first_dims = None
    first_meta = None   # 首个文件解析出的 FV3000 元数据 (magnification/zoom/objective), 用于自动预填
    filename_meta = None  # 首个文件的文件名解析结果 (倍数 + 通道名+波长), 用于前端预填
    used_names = set()
    skipped_files = []          # 文件夹导入时跳过的非 TIFF 文件
    merge_candidates = []       # 识别为 merge 的文件 (channels, meta, filename), 不计入通道
    rgb_empty_pending = []      # 伪彩 RGB 中无信号的平面 [(plane_idx, array)], 登记为可恢复

    for fileobj, filename in zip(file_list, filenames):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ['.tif', '.tiff']:
            # 文件夹导入可能含非 TIFF (如 Results.csv), 跳过而非报错
            skipped_files.append(filename)
            continue

        # 保存
        session_id_tmp = str(uuid.uuid4())
        filepath = os.path.join(UPLOAD_FOLDER, f'{session_id_tmp}{ext}')
        fileobj.save(filepath)

        try:
            # 读取 (filename 统一用 basename, 兼容完整路径上传)
            base_filename = os.path.basename(filename) or filename
            channels, meta = read_tiff_channels(filepath, base_filename)
            log.info(f'{base_filename}: {meta["num_channels"]} channels, {meta["width"]}x{meta["height"]}')
            # 记录首个成功解析文件的 FV3000 元数据 (magnification/zoom/objective), 供自动预填
            if first_meta is None:
                first_meta = meta
            # 文件名解析 (首个文件, 倍数 + 通道名+波长) — 前端可一键应用
            if filename_meta is None:
                filename_meta = parse_filename_metadata(base_filename)
                if filename_meta['magnification'] or filename_meta['channels']:
                    log.info(f'  → 文件名解析: mag={filename_meta["magnification"]}, '
                             f'channels={[(c["name"], c["wavelength"]) for c in filename_meta["channels"]]}')
            # 伪彩 RGB 里无信号的平面 (未启用的通道) 记录下来, 供后续「一键恢复」
            rgb_empty_pending.extend(meta.get('rgb_empty_planes') or [])

            # 检查尺寸一致
            if first_dims is None:
                first_dims = (meta['width'], meta['height'])
            elif first_dims != (meta['width'], meta['height']):
                raise ValueError(f'文件 "{base_filename}" 尺寸 {meta["width"]}x{meta["height"]} 与第一张 {first_dims[0]}x{first_dims[1]} 不一致')

            # 自动识别通道
            kw_type, kw_info = guess_channel_from_filename(base_filename)

            # 识别为 merge (FV3000 _T001 / 文件名含 merge) → 不计入通道, 仅记录用于 merge 视图
            if kw_type == 'merge':
                if pixel_size_um is None:
                    pixel_size_um = meta['pixel_size_um']
                merge_candidates.append((channels, meta, base_filename))
                log.info(f'  → 识别为 MERGE (不作为通道): {base_filename}')
                continue

            # 单通道图优先提供像素尺寸 (FV3000 merge 的 pixel_size 不可靠)
            if pixel_size_um is None:
                pixel_size_um = meta['pixel_size_um']

            # 检测 RGB merge: 文件名含 merge/rgb/composite 或文件本身是 RGB/RGBA 彩色平面
            # (关键校正: 文件名无关键词但实际是彩色合成 TIFF 时, 也必须按 R/G/B 平面配色, 否则通道颜色错乱)
            is_rgb_merge = kw_type == 'merge' or any(
                kw in base_filename.lower()
                for kw in ['merge', 'rgb', 'composite', 'merged', 'overlay', 'color']
            ) or meta.get('color_type') in ('rgb', 'rgba')

            if kw_type == 'fv3000' and kw_info:
                # FV3000 单通道: _C001T001 等, 已经在 read_tiff_channels 拆为单通道
                # 注意: C 编号只是通道槽位, 不代表固定波长/颜色 -> detected=False (低置信)
                ch_num, ch_name, ch_color, ch_rgb = kw_info
                name = ch_name
                color = ch_color
                rgb = ch_rgb
                if name in used_names:
                    name = f'{name} (C{ch_num:03d})'
                used_names.add(name)
                all_channels.append({
                    'data': channels[0], 'name': name, 'color': color, 'rgb': rgb,
                    '_order': ch_num, 'detected': False
                })
                log.info(f'  → FV3000 C{ch_num:03d} → {name} ({color}) [低置信, 待确认]')
            elif meta.get('rgb_planes') and len(meta['rgb_planes']) == len(channels):
                # 伪彩 RGB 导出: 按物理平面配色 (R→红/G→绿/B→蓝), 且只保留有信号的平面
                # 例: 只启用 2 个通道的 TIFF → 只有 2 个平面有信号 → 得到 2 个通道
                for k, pidx in enumerate(meta['rgb_planes']):
                    name = PLANE_NAMES[pidx]
                    rgb = PLANE_COLORS[pidx]
                    color = '#%02X%02X%02X' % rgb
                    if name in used_names:
                        name = f'{name} ({base_filename})'
                    used_names.add(name)
                    all_channels.append({
                        'data': channels[k], 'name': name, 'color': color, 'rgb': rgb,
                        '_order': len(all_channels), 'detected': True
                    })
                log.info(f'  → 伪彩 RGB 拆出 {len(channels)} 个有信号平面 {meta["rgb_planes"]}')
            elif is_rgb_merge and meta['num_channels'] == 3:
                # RGB merge: 拆为 R / G / B 通道 (R→红/G→绿/B→蓝 物理对应, 高置信)
                for i, ch in enumerate(channels):
                    name = PLANE_NAMES[i] if i < 3 else f'Ch{i+1}'
                    rgb = PLANE_COLORS[i] if i < 3 else (255, 255, 255)
                    color = '#%02X%02X%02X' % rgb
                    if name in used_names:
                        name = f'{name} (Merge)'
                    used_names.add(name)
                    all_channels.append({
                        'data': ch, 'name': name, 'color': color, 'rgb': rgb,
                        '_order': len(all_channels), 'detected': True
                    })
            else:
                for i, ch in enumerate(channels):
                    if kw_info and meta['num_channels'] == 1:
                        # 单通道图使用文件名识别 (波长/染料关键词, 高置信)
                        name = kw_info['name']
                        color = kw_info['color']
                        rgb = kw_info['rgb']
                        detected = True
                    else:
                        # 多通道图或未识别，按索引 (颜色不可靠, 低置信)
                        cfg = default_channel_config(len(all_channels))
                        name = cfg['name']
                        color = cfg['color']
                        rgb = cfg['rgb']
                        detected = False

                    if name in used_names:
                        name = f'{name} ({base_filename})'
                    used_names.add(name)
                    all_channels.append({
                        'data': ch, 'name': name, 'color': color, 'rgb': rgb,
                        '_order': len(all_channels), 'detected': detected
                    })
        finally:
            # 清理临时文件 (后续按需重新生成预览)
            try:
                os.remove(filepath)
            except Exception:
                pass

    # 兜底: 若没有任何单通道但识别到 merge, 拆 merge 为 R/G/B 作为通道
    # 关键: 颜色必须与 R/G/B 平面严格对应 (R→红 / G→绿 / B→蓝), 不能用 default_channel_config
    # (default 顺序是 DAPI蓝/GFP绿/RFP红, 会把红平面涂蓝、蓝平面涂红)
    if not all_channels and merge_candidates:
        channels, meta, filename = merge_candidates[0]
        for i, ch in enumerate(channels[:3]):
            name = PLANE_NAMES[i] if i < 3 else f'Ch{i+1}'
            rgb = PLANE_COLORS[i] if i < 3 else (255, 255, 255)
            color = '#%02X%02X%02X' % rgb
            all_channels.append({
                'data': ch, 'name': name, 'color': color, 'rgb': rgb,
                '_order': i, 'detected': True
            })
        log.info(f'  兜底: 从 merge 文件 {filename} 拆分出 {len(all_channels)} 通道 (R→红/G→绿/B→蓝)')

    # 按自然顺序排序 (FV3000: C001<C002<...; 其他: 出现顺序)
    all_channels.sort(key=lambda c: c.get('_order', 1e9))

    # ======== 空通道(未启用通道)过滤 ========
    empty_channels = []
    # (a) 伪彩 RGB 里无信号的平面: read_tiff_channels 已判定, 直接登记为可恢复
    for pidx, arr in rgb_empty_pending:
        empty_channels.append({
            'data': arr, 'name': PLANE_NAMES[pidx],
            'color': '#%02X%02X%02X' % PLANE_COLORS[pidx], 'rgb': PLANE_COLORS[pidx],
            '_order': 1e8 + pidx, '_empty': True,
            '_empty_reason': f'RGB {PLANE_NAMES[pidx]} 平面无信号 (该通道未启用)'
        })
    # (b) 再兜一层, 覆盖「多页 TIFF 含空白页」「文件夹导入混入全黑文件」两种情况。
    #     至少保留 1 个通道, 避免把整组图判成 0 通道。
    live_channels = []
    for ch in all_channels:
        act = channel_activity(ch['data'])
        ch['activity'] = act
        if act['is_dead'] and live_channels:
            ch['_empty_reason'] = (
                f"非零像素 {act['nonzero_ratio'] * 100:.2f}% / 唯一值 {act['unique_count']}")
            empty_channels.append(ch)
        else:
            live_channels.append(ch)
    if empty_channels:
        empty_channels.sort(key=lambda c: c.get('_order', 1e9))
        log.info(f'  空通道过滤: 忽略 {len(empty_channels)} 个无信号通道 '
                 f'({", ".join(c["name"] for c in empty_channels)})')
    all_channels = live_channels

    # 按用户预先选择的通道数截断 (先选通道数 → 导入 → 自动识别对应图片)
    if channel_count and channel_count > 0 and len(all_channels) > channel_count:
        dropped = len(all_channels) - channel_count
        all_channels = all_channels[:channel_count]
        log.info(f'  按通道数 {channel_count} 截断, 丢弃 {dropped} 个多余通道')

    if not all_channels:
        msg = '未提取到任何通道'
        if skipped_files:
            msg += f' (已跳过 {len(skipped_files)} 个非 TIFF 文件)'
        raise ValueError(msg)

    # 构建会话
    sid = str(uuid.uuid4())
    h, w = first_dims[1], first_dims[0]

    # 解析到 FV3000 物镜倍数时, 用权威标定公式覆盖 TIFF ResolutionUnit 经常给出的错误值 (如 138.9 μm/px)
    if first_meta and first_meta.get('magnification'):
        fov_px = fv3000_pixel_size_um(first_meta['magnification'], w, first_meta.get('zoom_size', 1.0))
        if fov_px:
            pixel_size_um = fov_px
            log.info(f'  → 按 FV3000 标定公式覆盖像素尺寸: {first_meta["magnification"]}× '
                     f'(zoom {first_meta.get("zoom_size", 1.0)}) → {pixel_size_um:.4f} μm/px')

    configs = []
    for ch in all_channels:
        configs.append({
            'name': ch['name'],
            'color': ch['color'],
            'rgb': ch['rgb'],
            'visible': True,
            'brightness': 1.0,
            'contrast': 1.0,
            'black': 0,
            'white': 255,
            'gamma': 1.0,
            'detected': ch.get('detected', True)
        })
    channel_data = [ch['data'] for ch in all_channels]

    # 生成预览 + 直方图 (色阶图形, 基态归一化到 0-255 的分布)
    previews = []
    histograms = []
    for data in channel_data:
        normalized = normalize_channel(data)
        previews.append(array_to_base64(normalized))
        hist, _ = np.histogram(normalized.astype(np.float64), bins=256, range=(0, 256))
        histograms.append(hist.astype(int).tolist())

    merged_img = create_merge_image(channel_data, configs, h, w)
    merged_b64 = image_to_base64(merged_img)

    session_data[sid] = {
        'channels': channel_data,
        'configs': configs,
        'histograms': histograms,
        'active_count': len(channel_data),
        # 被判定为「无信号」而自动忽略的通道, 保留数据供用户一键恢复 (自动识别兜底)
        'dropped': [
            {'data': c['data'], 'name': c['name'], 'color': c['color'], 'rgb': c['rgb'],
             'reason': c.get('_empty_reason', '')}
            for c in empty_channels
        ],
        'metadata': {
            'width': w, 'height': h,
            'num_channels': len(channel_data),
            'pixel_size_um': pixel_size_um,
            'magnification': first_meta.get('magnification') if first_meta else None,
            'zoom_size': first_meta.get('zoom_size', 1.0) if first_meta else 1.0,
            'objective_lens': first_meta.get('objective_lens') if first_meta else None,
            'pixel_size_source': first_meta.get('pixel_size_source') if first_meta else None,
            # 文件名解析的预填建议 (首个文件); 前端展示并可一键应用
            'filename_meta': filename_meta or {'magnification': None, 'channels': [], 'raw_matches': []},
        }
    }
    log.info(f'Session {sid} created: {len(channel_data)} channels')

    return {
        'session_id': sid,
        'metadata': session_data[sid]['metadata'],
        'configs': configs,
        'channel_previews': previews,
        'histograms': histograms,
        'merged_preview': merged_b64,
        'expected_channels': channel_count,
        'detected_channels': len(all_channels),
        'needs_color_check': any(not c.get('detected', True) for c in all_channels),
        'skipped_files': skipped_files,
        'merge_detected': bool(merge_candidates),
        # 自动忽略的空通道 (未启用/无信号) 数量与名称, 供前端提示 + 一键恢复
        'dropped_empty': len(empty_channels),
        'dropped_details': [
            {'name': c['name'], 'reason': c.get('_empty_reason', '')} for c in empty_channels
        ],
    }


@app.route('/api/upload', methods=['POST'])
def upload_file():
    """单文件上传 (兼容旧接口)"""
    if 'file' not in request.files:
        return jsonify({'error': '未选择文件'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '未选择文件'}), 400
    try:
        result = _process_uploaded_files([file], [file.filename])
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        log.error(f'Upload failed: {e}\n{traceback.format_exc()}')
        return jsonify({'error': f'图像处理失败: {str(e)}'}), 500


@app.route('/api/upload_multi', methods=['POST'])
def upload_multi():
    """多文件/文件夹上传 - 支持 channel_count (先选通道数 → 导入文件夹 → 自动识别)"""
    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': '未选择文件，请至少选择 1 张 TIFF'}), 400

    filenames = [f.filename for f in files]
    channel_count_raw = request.form.get('channel_count')
    channel_count = None
    if channel_count_raw:
        try:
            channel_count = int(channel_count_raw)
        except ValueError:
            channel_count = None
    try:
        result = _process_uploaded_files(files, filenames, channel_count=channel_count)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        log.error(f'Multi upload failed: {e}\n{traceback.format_exc()}')
        return jsonify({'error': f'图像处理失败: {str(e)}'}), 500


@app.route('/api/update_channel', methods=['POST'])
def update_channel():
    data = request.json or {}
    session_id = data.get('session_id')
    ch_idx = data.get('channel_index')
    config = data.get('config')

    if session_id not in session_data:
        return jsonify({'error': '会话不存在或已过期，请重新导入图像'}), 404

    sd = session_data[session_id]
    if ch_idx is None or ch_idx < 0 or ch_idx >= len(sd['configs']):
        return jsonify({'error': '通道索引无效'}), 400

    for key, value in (config or {}).items():
        if key == 'color' and isinstance(value, str):
            hex_color = value.lstrip('#')
            try:
                rgb = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
                sd['configs'][ch_idx]['rgb'] = rgb
                sd['configs'][ch_idx]['color'] = value
            except ValueError:
                return jsonify({'error': f'无效颜色值: {value}'}), 400
        else:
            sd['configs'][ch_idx][key] = value

    h, w = sd['metadata']['height'], sd['metadata']['width']
    chs_ac, cfgs_ac = _active_slice(sd)
    merged_img = create_merge_image(chs_ac, cfgs_ac, h, w)
    merged_b64 = image_to_base64(merged_img)

    ch_data = sd['channels'][ch_idx]
    cfg = sd['configs'][ch_idx]
    normalized = normalize_channel(
        ch_data,
        black=cfg.get('black', 0),
        white=cfg.get('white', 255),
        gamma=cfg.get('gamma', 1.0),
        brightness=cfg.get('brightness', 1.0),
        contrast=cfg.get('contrast', 1.0)
    )
    # 统一返回灰度 (原始强度), 着色由前端按 config 颜色实时完成:
    # 1) 保证与上传接口语义一致 (此前上传返回灰度、本接口返回彩色, 导致导入后是灰图、动滑块才变彩)
    # 2) 支持前端灰度/彩色零延迟切换, 无需重新请求
    # 3) 灰度三通道相同, 前端本地色阶预览的 gamma 反变换不会再产生色偏
    ch_preview = array_to_base64(normalized)

    return jsonify({'merged_preview': merged_b64, 'channel_preview': ch_preview})


@app.route('/api/set_channel_count', methods=['POST'])
def set_channel_count():
    """切换活跃通道数 (1..N): 左侧通道列表/预览/导出仅使用前 N 个通道, 不丢失底层数据, 可随时调回"""
    data = request.json or {}
    session_id = data.get('session_id')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在或已过期，请重新导入图像'}), 404
    sd = session_data[session_id]
    try:
        n = int(data.get('count', 0))
    except (TypeError, ValueError):
        n = 0
    full = len(sd['channels'])
    if n < 1 or n > full:
        return jsonify({'error': f'通道数需在 1-{full} 之间'}), 400
    sd['active_count'] = n
    h, w = sd['metadata']['height'], sd['metadata']['width']
    chs, cfgs = sd['channels'][:n], sd['configs'][:n]
    merged_b64 = image_to_base64(create_merge_image(chs, cfgs, h, w))
    previews = []
    for ch, cfg in zip(chs, cfgs):
        normalized = normalize_channel(
            ch,
            black=cfg.get('black', 0),
            white=cfg.get('white', 255),
            gamma=cfg.get('gamma', 1.0),
            brightness=cfg.get('brightness', 1.0),
            contrast=cfg.get('contrast', 1.0)
        )
        # 灰度预览, 着色交前端 (与上传/update_channel 接口语义保持一致)
        previews.append(array_to_base64(normalized))
    return jsonify({
        'ok': True, 'count': n, 'active_count': n,
        'merged_preview': merged_b64,
        'channel_previews': previews,
        'histograms': sd['histograms'][:n],
        'configs': cfgs,
    })


@app.route('/api/restore_empty_channels', methods=['POST'])
def restore_empty_channels():
    """把导入时被自动判定为「无信号」而忽略的通道放回通道列表。

    自动识别的兜底: 万一空通道判据误杀 (例如信号极弱/极稀疏的真实通道),
    用户可一键恢复, 之后正常参与显示与导出。
    """
    data = request.json or {}
    session_id = data.get('session_id')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在或已过期，请重新导入图像'}), 404
    sd = session_data[session_id]
    dropped = sd.get('dropped') or []
    if not dropped:
        return jsonify({'error': '本次导入没有可恢复的空通道'}), 400

    h, w = sd['metadata']['height'], sd['metadata']['width']
    for item in dropped:
        sd['channels'].append(item['data'])
        sd['configs'].append({
            'name': item['name'], 'color': item['color'], 'rgb': item['rgb'],
            'visible': True, 'brightness': 1.0, 'contrast': 1.0,
            'black': 0, 'white': 255, 'gamma': 1.0, 'detected': False
        })
    sd['dropped'] = []
    sd['active_count'] = len(sd['channels'])
    sd['metadata']['num_channels'] = len(sd['channels'])

    previews, histograms = [], []
    for ch, cfg in zip(sd['channels'], sd['configs']):
        normalized = normalize_channel(
            ch, black=cfg.get('black', 0), white=cfg.get('white', 255),
            gamma=cfg.get('gamma', 1.0), brightness=cfg.get('brightness', 1.0),
            contrast=cfg.get('contrast', 1.0))
        previews.append(array_to_base64(normalized))
        hist, _ = np.histogram(normalized.astype(np.float64), bins=256, range=(0, 256))
        histograms.append(hist.astype(int).tolist())
    sd['histograms'] = histograms

    merged_b64 = image_to_base64(create_merge_image(sd['channels'], sd['configs'], h, w))
    return jsonify({
        'ok': True,
        'active_count': sd['active_count'],
        'configs': sd['configs'],
        'channel_previews': previews,
        'histograms': histograms,
        'merged_preview': merged_b64,
        'restored': len(dropped),
    })


@app.route('/api/reorder_channels', methods=['POST'])
def reorder_channels():
    """按 order 数组重排通道顺序 (order 为旧索引的新排列)"""
    data = request.json or {}
    session_id = data.get('session_id')
    order = data.get('order')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在或已过期，请重新导入图像'}), 404
    sd = session_data[session_id]
    n = len(sd['channels'])
    if not isinstance(order, list) or len(order) != n:
        return jsonify({'error': f'order 长度应为 {n}'}), 400
    try:
        order = [int(x) for x in order]
        if sorted(order) != list(range(n)):
            return jsonify({'error': 'order 必须包含 0..n-1 的完整排列'}), 400
        sd['channels'] = [sd['channels'][i] for i in order]
        sd['configs'] = [sd['configs'][i] for i in order]
        sd['histograms'] = [sd['histograms'][i] for i in order]
    except (ValueError, IndexError, TypeError):
        return jsonify({'error': 'order 包含无效索引'}), 400
    chs_ac, cfgs_ac = _active_slice(sd)
    merged_img = create_merge_image(chs_ac, cfgs_ac,
                                    sd['metadata']['height'], sd['metadata']['width'])
    return jsonify({'success': True, 'merged_preview': image_to_base64(merged_img),
                    'histograms': sd['histograms'][:int(sd.get('active_count') or len(sd['channels']))]})


@app.route('/api/add_scale_bar', methods=['POST'])
def add_scale_bar_api():
    data = request.json or {}
    session_id = data.get('session_id')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在'}), 404

    sd = session_data[session_id]
    h, w = sd['metadata']['height'], sd['metadata']['width']

    chs_ac, cfgs_ac = _active_slice(sd)
    merged_img = create_merge_image(chs_ac, cfgs_ac, h, w)
    # 优先采用前端当前值 (用户所见即所得), 回写后端保证两侧一致, 杜绝预览/导出像素尺寸漂移
    pixel_size_um = data.get('pixel_size_um') or sd['metadata'].get('pixel_size_um')
    if pixel_size_um is not None:
        try:
            sd['metadata']['pixel_size_um'] = float(pixel_size_um)
            pixel_size_um = sd['metadata']['pixel_size_um']
        except (TypeError, ValueError):
            pass
    # v1.4.0: 优先用 pt 单位 (Illustrator 习惯); 老字段 (font_size/thickness) 作 fallback 兼容
    thickness_pt = float(data.get('thickness_pt', data.get('thickness', 1.0)))
    font_size_auto = bool(data.get('font_size_auto', True))
    font_size_pt = (None if font_size_auto
                    else float(data.get('font_size_pt', data.get('font_size', 12))))
    merged_img = add_scale_bar(
        merged_img, pixel_size_um,
        data.get('bar_length_um', 50),
        data.get('position', 'bottom-right'),
        data.get('bar_color', 'white'),
        thickness_pt=thickness_pt,
        font_size_pt=font_size_pt,
        dpi=72,
    )
    # 保存当前 scale bar 配置, 供后续 export_image 复用 (避免每次重传)
    sd['scale_bar_config'] = {
        'length_um': data.get('bar_length_um', 50),
        'position': data.get('position', 'bottom-right'),
        'color': data.get('bar_color', 'white'),
        'thickness_pt': thickness_pt,
        'font_size_pt': font_size_pt,
        'font_size_auto': font_size_auto,
    }
    return jsonify({'preview': image_to_base64(merged_img)})


@app.route('/api/line_profile', methods=['POST'])
def line_profile_api():
    data = request.json or {}
    session_id = data.get('session_id')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在'}), 404

    sd = session_data[session_id]
    x1, y1 = data.get('x1'), data.get('y1')
    x2, y2 = data.get('x2'), data.get('y2')
    channel_indices = data.get('channels', [])

    profiles = {}
    for idx in channel_indices:
        if 0 <= idx < len(sd['channels']):
            ch_data = sd['channels'][idx].astype(np.float64)
            profile = extract_line_profile(ch_data, x1, y1, x2, y2)
            profiles[sd['configs'][idx]['name']] = profile
    return jsonify({'profiles': profiles})


@app.route('/api/pearson', methods=['POST'])
def pearson_api():
    data = request.json or {}
    session_id = data.get('session_id')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在'}), 404

    sd = session_data[session_id]
    ch1_idx = data.get('channel1')
    ch2_idx = data.get('channel2')
    roi = data.get('roi')

    if ch1_idx is None or ch2_idx is None:
        return jsonify({'error': '需要指定两个通道'}), 400
    if ch1_idx < 0 or ch1_idx >= len(sd['channels']) or ch2_idx < 0 or ch2_idx >= len(sd['channels']):
        return jsonify({'error': '通道索引无效'}), 400

    result = calculate_pearson(
        sd['channels'][ch1_idx].astype(np.float64),
        sd['channels'][ch2_idx].astype(np.float64),
        roi
    )
    result['channel1_name'] = sd['configs'][ch1_idx]['name']
    result['channel2_name'] = sd['configs'][ch2_idx]['name']
    return jsonify(result)


@app.route('/api/roi_preview', methods=['POST'])
def roi_preview_api():
    data = request.json or {}
    session_id = data.get('session_id')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在'}), 404

    sd = session_data[session_id]
    roi = data.get('roi') or {}
    x, y, w, h = int(roi.get('x', 0)), int(roi.get('y', 0)), int(roi.get('width', 0)), int(roi.get('height', 0))
    if w <= 0 or h <= 0:
        return jsonify({'error': 'ROI 无效'}), 400

    merged_img = create_merge_image(
        sd['channels'], sd['configs'],
        sd['metadata']['height'], sd['metadata']['width']
    )
    x2, y2 = min(x + w, merged_img.width), min(y + h, merged_img.height)
    roi_img = merged_img.crop((x, y, x2, y2))
    return jsonify({'preview': image_to_base64(roi_img)})


# ======== 导出 - 支持 6 面板布局 (B/G/R/Merge/Inset/Profile) ========

def _make_color_channel_image(data, rgb, brightness=1.0, contrast=1.0, black=0, white=255, gamma=1.0):
    """生成单通道的彩色图"""
    normalized = normalize_channel(data, black=black, white=white, gamma=gamma,
                                   brightness=brightness, contrast=contrast)
    colored = np.zeros((normalized.shape[0], normalized.shape[1], 3), dtype=np.uint8)
    for c in range(3):
        colored[:, :, c] = (normalized * (rgb[c] / 255.0)).astype(np.uint8)
    return Image.fromarray(colored, mode='RGB')


def _make_inset_panel(merged_img, channel_images, roi, inset_height_px, scale_bar_config, pixel_size_um):
    """生成 Inset 面板: 多通道的 ROI 放大图垂直堆叠"""
    if not roi or roi.get('width', 0) <= 0 or roi.get('height', 0) <= 0:
        return None
    x, y, w, h = int(roi['x']), int(roi['y']), int(roi['width']), int(roi['height'])
    if w <= 0 or h <= 0:
        return None

    # 裁剪每个通道的 ROI
    crops = []
    for img in channel_images:
        x2, y2 = min(x + w, img.width), min(y + h, img.height)
        crop = img.crop((x, y, x2, y2))
        crops.append(crop)

    # 追加 merge 的 ROI 放大图 (堆叠到最下方)
    if merged_img is not None:
        mx2, my2 = min(x + w, merged_img.width), min(y + h, merged_img.height)
        crops.append(merged_img.crop((x, y, mx2, my2)))

    if not crops:
        return None

    # 堆叠 (垂直)
    n = len(crops)
    # 统一宽度
    target_w = min(c.width for c in crops)
    target_h = int(target_w * h / w) if w > 0 else crops[0].height
    crops = [c.resize((target_w, target_h), Image.LANCZOS) for c in crops]

    gap = 2
    total_h = n * target_h + (n - 1) * gap
    panel = Image.new('RGB', (target_w, total_h), (255, 255, 255))
    for i, c in enumerate(crops):
        panel.paste(c, (0, i * (target_h + gap)))
    return panel


def _apply_inset_overlay(img, roi, inset_config, panel_type='channel'):
    """
    在图像右上角叠加 Inset 放大视图 (类似 ChannelScope Studio)
    inset_config: {
        'enabled': bool,
        'width': int,       # ROI 宽度 (px)
        'height': int,      # ROI 高度 (px)
        'zoom': float,      # 显示放大倍数
        'position': 'top-right' | 'top-left' | 'bottom-right' | 'bottom-left'
    }
    """
    if not inset_config or not inset_config.get('enabled'):
        return img
    if not roi or roi.get('width', 0) <= 0 or roi.get('height', 0) <= 0:
        return img

    x, y, w, h = int(roi['x']), int(roi['y']), int(roi['width']), int(roi['height'])
    if w <= 0 or h <= 0:
        return img

    # 确保 ROI 在图像内
    img_w, img_h = img.size
    x2 = min(x + w, img_w)
    y2 = min(y + h, img_h)
    x = max(0, x)
    y = max(0, y)
    w = x2 - x
    h = y2 - y
    if w <= 0 or h <= 0:
        return img

    zoom = float(inset_config.get('zoom', 2.0))
    if zoom <= 0.1:
        zoom = 1.0

    # 按 zoom 缩放裁剪区域
    inset_w = int(w * zoom)
    inset_h = int(h * zoom)
    if inset_w < 10 or inset_h < 10:
        return img

    crop = img.crop((x, y, x + w, y + h)).resize((inset_w, inset_h), Image.LANCZOS)

    # 放置位置: 右上角默认, 带 margin
    margin = max(8, int(min(img_w, img_h) * 0.015))
    position = inset_config.get('position', 'top-right')
    if position == 'top-right':
        px, py = img_w - inset_w - margin, margin
    elif position == 'top-left':
        px, py = margin, margin
    elif position == 'bottom-right':
        px, py = img_w - inset_w - margin, img_h - inset_h - margin
    else:  # bottom-left
        px, py = margin, img_h - inset_h - margin

    # 确保不超出图像边界
    px = max(0, min(px, img_w - inset_w))
    py = max(0, min(py, img_h - inset_h))

    out = img.copy()
    # 单层白色 1px 描边 (替代原先的外白内黑双层框, 与 SVG/PDF 1pt 对齐)
    overlay = Image.new('RGBA', (inset_w, inset_h), (255, 255, 255, 0))
    overlay.paste(crop, (0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, 0, inset_w - 1, inset_h - 1], outline=(255, 255, 255), width=1)
    out.paste(overlay, (px, py), overlay)
    return out


def _make_profile_plot(profiles, channel_configs, channel_names, pixel_size_um,
                       target_w, target_h, line_points=None):
    """生成 Profile Plot 面板: 强度曲线图"""
    if not profiles:
        # 空提示
        panel = Image.new('RGB', (target_w, target_h), (255, 255, 255))
        d = ImageDraw.Draw(panel)
        try:
            font = ImageFont.truetype("arial.ttf", 12)
        except Exception:
            font = ImageFont.load_default()
        d.text((target_w // 4, target_h // 2 - 8), 'Draw a line', fill=(120, 120, 120), font=font)
        return panel

    panel = Image.new('RGB', (target_w, target_h), (255, 255, 255))
    d = ImageDraw.Draw(panel)
    try:
        font = ImageFont.truetype("arial.ttf", 10)
        font_lbl = ImageFont.truetype("arial.ttf", 9)
    except Exception:
        font = ImageFont.load_default()
        font_lbl = font

    margin = {'top': 15, 'right': 10, 'bottom': 22, 'left': 32}
    plot_w = target_w - margin['left'] - margin['right']
    plot_h = target_h - margin['top'] - margin['bottom']

    # 全局最大
    global_max = 0
    for p in profiles.values():
        global_max = max(global_max, max(p['values']) if p['values'] else 0)
    if global_max <= 0:
        global_max = 1

    # 边框
    d.rectangle([margin['left'], margin['top'], margin['left'] + plot_w, margin['top'] + plot_h],
                outline=(180, 180, 180), width=1)

    # 网格
    for i in range(1, 4):
        y = margin['top'] + plot_h * i / 4
        d.line([(margin['left'], y), (margin['left'] + plot_w, y)], fill=(230, 230, 230), width=1)
        d.text((2, y - 5), f'{int(global_max * (4-i) / 4)}', fill=(120, 120, 120), font=font_lbl)

    # X轴
    for i in range(5):
        x = margin['left'] + plot_w * i / 4
        d.line([(x, margin['top'] + plot_h), (x, margin['top'] + plot_h + 3)], fill=(180, 180, 180), width=1)

    # 找最大 positions
    max_pos = 0
    for p in profiles.values():
        if p['positions']:
            max_pos = max(max_pos, p['positions'][-1])
    if max_pos <= 0:
        max_pos = 1

    # 单位: 像素/μm
    if pixel_size_um and pixel_size_um > 0:
        x_label = f'Distance (μm)'
    else:
        x_label = 'Distance (px)'

    # 绘制曲线 - 按通道配置取色
    for idx, (name, profile) in enumerate(profiles.items()):
        # 找对应通道配置取色
        color = (200, 50, 50)
        for i, cfg in enumerate(channel_configs):
            if cfg['name'] == name:
                rgb = cfg.get('rgb', (255, 100, 100))
                color = rgb
                break
        positions = profile['positions']
        values = profile['values']
        if not positions:
            continue
        pts = []
        for pos, val in zip(positions, values):
            x = margin['left'] + (pos / max_pos) * plot_w
            y = margin['top'] + plot_h * (1 - val / global_max)
            pts.append((x, y))
        if len(pts) >= 2:
            d.line(pts, fill=color, width=1)

    # Y轴标签
    d.text((4, 2), 'Intensity', fill=(80, 80, 80), font=font_lbl)
    # X轴标签
    d.text((target_w - 70, target_h - 12), x_label, fill=(80, 80, 80), font=font_lbl)

    # 图例 (右上角)
    legend_x = margin['left'] + plot_w - 80
    legend_y = margin['top'] + 2
    for idx, name in enumerate(profiles.keys()):
        color = (200, 50, 50)
        for i, cfg in enumerate(channel_configs):
            if cfg['name'] == name:
                color = cfg.get('rgb', (200, 50, 50))
                break
        d.rectangle([legend_x, legend_y + idx * 11, legend_x + 8, legend_y + idx * 11 + 8], fill=color)
        short_name = name[:8]
        d.text((legend_x + 10, legend_y + idx * 11 - 1), short_name, fill=(60, 60, 60), font=font_lbl)

    return panel


def _draw_dashed_rect(draw, box, color, width=2, dash=12, gap=8):
    """在 box=(x0,y0,x1,y1) 四条边上绘制虚线矩形"""
    x0, y0, x1, y1 = box
    edges = [
        [(x0, y0), (x1, y0)],
        [(x1, y0), (x1, y1)],
        [(x1, y1), (x0, y1)],
        [(x0, y1), (x0, y0)],
    ]
    for (sx, sy), (ex, ey) in edges:
        length = math.hypot(ex - sx, ey - sy)
        if length == 0:
            continue
        dx, dy = (ex - sx) / length, (ey - sy) / length
        dist = 0.0
        while dist < length:
            d2 = min(dist + dash, length)
            draw.line([(sx + dx * dist, sy + dy * dist), (sx + dx * d2, sy + dy * d2)],
                      fill=color, width=width)
            dist += dash + gap


def _draw_roi_boundary(img, roi):
    """在导出图上绘制 ROI 选框 (标记选区位置, 类似 ChannelScope Studio)

    框线颜色由 roi['color'] 决定 (缺省白色 255,255,255); 默认 1px 实线 (与 SVG/PDF 的 1pt 一致)。
    """
    if not roi or roi.get('width', 0) <= 0 or roi.get('height', 0) <= 0:
        return img
    x, y, w, h = int(roi['x']), int(roi['y']), int(roi['width']), int(roi['height'])
    img_w, img_h = img.size
    x2 = min(x + w, img_w)
    y2 = min(y + h, img_h)
    x = max(0, x)
    y = max(0, y)
    w = x2 - x
    h = y2 - y
    if w <= 0 or h <= 0:
        return img
    out = img.copy()
    draw = ImageDraw.Draw(out)
    # 框线颜色可自定义 (roi['color'], 支持 '#rrggbb' 或 [r,g,b]), 未指定则回退白色 (与 SVG 默认一致)
    line_color = _as_rgb(roi.get('color')) if roi.get('color') else (255, 255, 255)
    # 1px 实线 (与 PDF 1pt 对齐); PIL 不直接支持 dasharray, 栅格导出保持实线, 虚线仅矢量 (SVG) 输出
    draw.rectangle([x, y, x + w - 1, y + h - 1], outline=line_color, width=1)
    return out


def _apply_overlays(img, scale_bar_config, inset_config, roi_config, pixel_size_um, panel_type='channel'):
    """给图像应用 ROI 虚线框、Scale Bar 和 Inset Overlay"""
    is_main = panel_type in ('channel', 'merge') or (isinstance(panel_type, str) and panel_type.startswith('ch'))
    if roi_config and is_main:
        img = _draw_roi_boundary(img, roi_config)
    if scale_bar_config:
        # v1.4.0: 厚度按 pt, 字号按 pt (或自动); 兼容旧 px 字段
        thickness_pt = scale_bar_config.get('thickness_pt')
        if thickness_pt is None:
            thickness_pt = float(scale_bar_config.get('thickness', 3))   # 旧 px 字段当 pt 用 (≈ 1:1)
        if scale_bar_config.get('font_size_auto', True):
            font_size_pt = None   # 走 add_scale_bar 自动
        elif scale_bar_config.get('font_size_pt') is not None:
            font_size_pt = float(scale_bar_config['font_size_pt'])
        else:
            font_size_pt = float(scale_bar_config.get('font_size', 14))   # 旧 px 当 pt
        img = add_scale_bar(
            img, pixel_size_um,
            scale_bar_config.get('length_um', 50),
            scale_bar_config.get('position', 'bottom-right'),
            scale_bar_config.get('color', 'white'),
            thickness_pt=thickness_pt,
            font_size_pt=font_size_pt,
            dpi=72,
        )
    if inset_config and inset_config.get('enabled') and roi_config:
        img = _apply_inset_overlay(img, roi_config, inset_config, panel_type)
    return img


def _as_rgb(color):
    """归一化颜色为 (r,g,b) int 元组; 兼容 list / tuple / hex 字符串 / None。
    PIL 的 draw.text(fill=...) 只接受 tuple, JSON 反序列化可能产生 list, 故统一归一化。"""
    if color is None:
        return (0, 0, 0)
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        try:
            return (int(color[0]), int(color[1]), int(color[2]))
        except (ValueError, TypeError):
            return (0, 0, 0)
    if isinstance(color, str):
        h = color.lstrip('#')
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        if len(h) == 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                pass
    return (0, 0, 0)


def _to_roman(n):
    """整数转罗马数字 (1..3999)"""
    n = max(1, int(n))
    table = [(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'), (100, 'C'),
             (90, 'XC'), (50, 'L'), (40, 'XL'), (10, 'X'), (9, 'IX'), (5, 'V'),
             (4, 'IV'), (1, 'I')]
    res = ''
    for v, s in table:
        while n >= v:
            res += s
            n -= v
    return res


def _format_label(idx, key, panel_meta, mode):
    """返回 (text, (r,g,b)) 用于顶部斜杠标签; 支持多种文献常用格式"""
    meta = panel_meta.get(key, {})
    if mode == 'name':
        return (meta.get('label', '') or key, _as_rgb(meta.get('color', (0, 0, 0))))
    if mode == 'none':
        return ('', (0, 0, 0))
    if mode == 'letter':
        return (chr(ord('A') + idx), (0, 0, 0))
    if mode == 'letter_paren':
        return ('(' + chr(ord('A') + idx) + ')', (0, 0, 0))
    if mode == 'lower':
        return (chr(ord('a') + idx), (0, 0, 0))
    if mode == 'number':
        return (str(idx + 1), (0, 0, 0))
    if mode == 'roman':
        return (_to_roman(idx + 1), (0, 0, 0))
    # 兜底
    return (chr(ord('A') + idx), (0, 0, 0))


def _short_channel_code(label, rgb):
    """由通道主色生成短码 (文献常用 G/R/B 表示法): B/G/R/C/M/Y;
    白/灰/黑等无主导色时取名称首字母。"""
    r, g, b = _as_rgb(rgb)
    mx = max(r, g, b)
    if mx <= 60:
        return (label or 'C')[0].upper()
    if b > r and b > g:
        return 'B'
    if g > r and g > b:
        return 'G'
    if r > g and r > b:
        return 'R'
    if abs(g - b) < 40 and g > r + 40:
        return 'C'
    if abs(r - b) < 40 and r > g + 40:
        return 'M'
    if abs(r - g) < 40 and r > b + 40:
        return 'Y'
    return (label or 'C')[0].upper()


def _active_slice(sd):
    """按会话活跃通道数返回 (channels, configs) 切片 (底层数据不截断, 可随时调回)"""
    n = int(sd.get('active_count') or len(sd['channels']))
    return sd['channels'][:n], sd['configs'][:n]


def _load_arial_font(size):
    """优先加载 Windows 自带 Arial 字体 (导出标签统一 Arial), 失败回退默认字体"""
    for path in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/Arial.ttf",
                 "C:/Windows/Fonts/arialbd.ttf", "arial.ttf", "Arial"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# Scale bar / 标签在 PDF 导出中统一使用 Arial (与 SVG 矢量层 / 栅格层一致, 符合 Illustrator 习惯);
# reportlab 内置无 Arial, 需从系统字体注册; 注册失败则回退 Helvetica。
try:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    _SCALE_BAR_PDF_FONT = 'Helvetica'
    for _ap in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/Arial.ttf",
                "C:/Windows/Fonts/arialbd.ttf", "arial.ttf", "Arial"):
        try:
            pdfmetrics.registerFont(TTFont('ArialWB', _ap))
            _SCALE_BAR_PDF_FONT = 'ArialWB'
            break
        except Exception:
            continue
except Exception:
    _SCALE_BAR_PDF_FONT = 'Helvetica'


def _draw_label(draw, text, x, y, font, color=(0, 0, 0)):
    """在 (x, y) 位置绘制粗体标签 (上标效果)"""
    draw.text((x, y), text, fill=_as_rgb(color), font=font)


def _encode_lossless(img):
    """面板图像无损编码为 PNG

    此前用 JPEG quality=95, 会对荧光细节与 Scale Bar 边缘引入有损压缩伪影,
    发论文不可接受。PNG 为无损格式, 逐像素保留原始像素值。
    """
    buf = io.BytesIO()
    rgb = img.convert('RGB') if img.mode != 'RGB' else img
    rgb.save(buf, format='PNG')
    buf.seek(0)
    return buf


def _build_pdf(panels, panel_w, panel_h, label_h, gap, cols, panel_meta, scale_bar_config, dpi, pearson_data=None, line_points=None, margin=10, font_size=None,
               layout='grid', top_header_lines=None, header_align='center'):
    """使用 reportlab 构建可编辑的 PDF 排版图"""
    # 顶部 header 高度
    header_h = 0
    if top_header_lines:
        fs = font_size if font_size else max(12, int(panel_w * 0.035))
        header_h = max(int(fs * 1.7 / dpi * 72), int(panel_w * 0.04 / dpi * 72)) + 4

    # ===== merge_left 特殊路径 =====
    if layout == 'merge_left':
        return _build_pdf_merge_left(panels, panel_w, panel_h, gap, margin, header_h,
                                      font_size, top_header_lines, scale_bar_config, dpi,
                                      pearson_data, header_align=header_align)

    n = len(panels)
    rows = math.ceil(n / cols)
    total_w_pt = (cols * panel_w + (cols - 1) * gap + 2 * margin) / dpi * 72
    total_h_pt = (rows * (panel_h + label_h) + (rows - 1) * gap + 2 * margin) / dpi * 72 + header_h
    if pearson_data:
        total_h_pt += (label_h + 10) / dpi * 72

    buf = io.BytesIO()
    cvs = pdf_canvas.Canvas(buf, pagesize=(total_w_pt, total_h_pt))


    # 顶部 header
    if top_header_lines:
        _draw_top_header_pdf(cvs, top_header_lines, total_w_pt, header_h, margin, font_size, panel_w, dpi, total_h_pt, align=header_align, image_top=header_h + margin + label_h + gap)

    for i, (key, img) in enumerate(panels):
        r, col = i // cols, i % cols
        x = (margin + col * (panel_w + gap)) / dpi * 72
        y = total_h_pt - header_h - (margin + (r + 1) * (panel_h + label_h + gap)) / dpi * 72

        # 图片 (无损 PNG 嵌入, 逐像素保留原始分辨率; 文字/边框仍为矢量可编辑)
        cvs.drawImage(ImageReader(_encode_lossless(img)), x, y,
                      width=panel_w / dpi * 72, height=panel_h / dpi * 72, preserveAspectRatio=True)

        # 标签 (矢量文字, 可在 Illustrator 中编辑)
        meta = panel_meta.get(key, {})
        label = meta.get('label', '')
        color = meta.get('color', (0, 0, 0))
        if label:
            cvs.setFillColorRGB(color[0] / 255.0, color[1] / 255.0, color[2] / 255.0)
            label_font = (font_size / dpi * 72) if font_size else panel_w / dpi * 72 * 0.08
            cvs.setFont('Helvetica', max(8, label_font))
            cvs.drawString(x, y + panel_h / dpi * 72 + 2, label)

    # 底部 Pearson r
    if pearson_data:
        r_val = pearson_data.get('pearson_r', 0)
        n_val = pearson_data.get('n_pixels', 0)
        cvs.setFillColorRGB(0, 0, 0)
        pearson_font = (font_size / dpi * 72 * 0.75) if font_size else panel_w / dpi * 72 * 0.06
        cvs.setFont('Helvetica', max(7, pearson_font))
        cvs.drawString(total_w_pt / 2 - 70, 8, f'Pearson r = {r_val:.4f}, N = {n_val}')

    cvs.save()
    buf.seek(0)
    return buf


def _draw_top_header_pdf(cvs, lines, total_w_pt, header_h, margin, font_size, panel_w, dpi, total_h_pt, align='center', image_top=None):
    """reportlab: 顶部一行多色文本 (原点左下角, y 向上, 因此画在画布顶部); align: center/left;
    image_top: 首行图片顶部 y (px, 从画布顶部起), 传入后标签 baseline 与图片保持 1px"""
    if not lines:
        return
    label_font = (font_size / dpi * 72) if font_size else panel_w / dpi * 72 * 0.08
    label_font = max(8, label_font)
    cvs.setFont('Helvetica', label_font)
    sep_text = ' / '
    sep_w = cvs.stringWidth(sep_text, 'Helvetica', label_font)
    seg_widths = []
    total_w = 0
    for text, _ in lines:
        w = cvs.stringWidth(text, 'Helvetica', label_font)
        seg_widths.append(w)
        total_w += w + sep_w
    total_w -= sep_w
    if align == 'left':
        x = margin / dpi * 72
    else:
        x = max(margin / dpi * 72, (total_w_pt - total_w) / 2)
    # reportlab 原点左下, y 向上 -> header 画在顶部; baseline 距图片顶部 1px
    if image_top:
        y = total_h_pt - (image_top - 1) / dpi * 72
    else:
        y = total_h_pt - header_h + max(3, label_font * 0.35)
    for i, (text, color) in enumerate(lines):
        cvs.setFillColorRGB(color[0] / 255.0, color[1] / 255.0, color[2] / 255.0)
        cvs.drawString(x, y, text)
        x += seg_widths[i]
        if i < len(lines) - 1:
            cvs.setFillColorRGB(120 / 255.0, 120 / 255.0, 120 / 255.0)
            cvs.drawString(x, y, sep_text)
            x += sep_w


def _build_pdf_merge_left(panels, panel_w, panel_h, gap, margin, header_h,
                           font_size, top_header_lines, scale_bar_config, dpi, pearson_data,
                           header_align='center'):
    """PDF: Merge 大图左 + 其余面板(通道/ROI/Inset/Profile)竖排右 (各按自身比例)"""
    merge_data, right_panels = _split_merge_left(panels, panel_w, margin)
    if merge_data is None:
        # 回退: 没有可放左大格的内容
        return None
    merge_img, merge_w, merge_h, right_total_h, small_w, right_heights, gap_eff = merge_data

    total_w_pt = (2 * margin + merge_w + gap_eff + small_w) / dpi * 72
    total_h_pt = (2 * margin + right_total_h) / dpi * 72 + header_h

    buf = io.BytesIO()
    cvs = pdf_canvas.Canvas(buf, pagesize=(total_w_pt, total_h_pt))

    if top_header_lines:
        _draw_top_header_pdf(cvs, top_header_lines, total_w_pt, header_h, margin, font_size, panel_w, dpi, total_h_pt, align=header_align, image_top=margin + header_h)

    def _embed_pdf(img):
        return ImageReader(_encode_lossless(img))

    # Merge 左大格
    cvs.drawImage(_embed_pdf(merge_img),
                  margin / dpi * 72,
                  total_h_pt - header_h - (margin + merge_h) / dpi * 72,
                  width=merge_w / dpi * 72, height=merge_h / dpi * 72,
                  preserveAspectRatio=True)
    # 右列 (通道 + ROI/Inset/Profile 等, 各自比例)
    cursor_px = margin
    for i, (k, img) in enumerate(right_panels):
        sh = right_heights[i]
        y_pt = total_h_pt - header_h - (cursor_px + sh) / dpi * 72
        x_pt = (margin + merge_w + gap_eff) / dpi * 72
        cvs.drawImage(_embed_pdf(img), x_pt, y_pt,
                      width=small_w / dpi * 72, height=sh / dpi * 72,
                      preserveAspectRatio=True)
        cursor_px += sh + gap_eff
    cvs.save()
    buf.seek(0)
    return buf


def _split_merge_left(panels, panel_w, margin):
    """拆出 merge_left 的 (左大格, 右列);
    返回 (merge_data, right_panels) 或 (None, None)"""
    if not panels:
        return None, None
    merge_idx = None
    for i, (k, _) in enumerate(panels):
        if k in ('merge', 'merged'):
            merge_idx = i
            break
    right_panels = [(k, img) for k, img in panels if k not in ('merge', 'merged')]
    if merge_idx is None:
        # 没有 merge -> 把右列第一个当左大格
        if not right_panels:
            return None, None
        merge_key, merge_img = right_panels.pop(0)
    else:
        merge_key, merge_img = panels[merge_idx]
    N = len(right_panels)
    small_w = panel_w
    gap_eff = max(4, int(small_w * 0.012))
    right_heights = []
    for k, img in right_panels:
        r = img.height / img.width if img.width > 0 else 1
        right_heights.append(max(1, int(small_w * r)))
    right_total_h = sum(right_heights) + max(0, N - 1) * gap_eff if N > 0 else small_w
    # 左大格保持自身比例, 高 = 右列总高
    merge_ratio = merge_img.height / merge_img.width if merge_img.width > 0 else 1
    merge_h = right_total_h
    merge_w = int(merge_h / merge_ratio) if merge_ratio > 0 else small_w
    return (merge_img, merge_w, merge_h, right_total_h, small_w, right_heights, gap_eff), right_panels


def _build_svg(panels, panel_w, panel_h, label_h, gap, cols, panel_meta, scale_bar_config, dpi, pearson_data=None, margin=10, font_size=None,
               layout='grid', top_header_lines=None, header_align='center'):
    """构建 SVG 排版图 (图像为 base64 嵌入, 文字/边框为矢量)"""
    header_h = 0
    if top_header_lines:
        fs = font_size if font_size else max(12, int(panel_w * 0.035))
        header_h = max(int(fs * 1.7), int(panel_w * 0.04)) + 6

    # ===== merge_left 特殊路径 =====
    if layout == 'merge_left':
        return _build_svg_merge_left(panels, panel_w, panel_h, gap, margin, header_h,
                                      font_size, top_header_lines, pearson_data,
                                      header_align=header_align)

    n = len(panels)
    rows = math.ceil(n / cols)
    total_w = cols * panel_w + (cols - 1) * gap + 2 * margin
    total_h = rows * (panel_h + label_h) + (rows - 1) * gap + 2 * margin + header_h
    if pearson_data:
        total_h += label_h + 10

    svg = ET.Element('svg', {
        'xmlns': 'http://www.w3.org/2000/svg',
        'xmlns:xlink': 'http://www.w3.org/1999/xlink',
        'width': str(total_w),
        'height': str(total_h),
        'viewBox': f'0 0 {total_w} {total_h}'
    })

    # 背景
    bg = ET.SubElement(svg, 'rect', {
        'x': '0', 'y': '0', 'width': str(total_w), 'height': str(total_h), 'fill': 'white'
    })

    # 顶部 header
    if top_header_lines:
        _draw_top_header_svg(svg, top_header_lines, total_w, header_h, margin, font_size, panel_w, align=header_align, image_top=margin + header_h + label_h)

    for i, (key, img) in enumerate(panels):
        r, c = i // cols, i % cols
        x = margin + c * (panel_w + gap)
        y = margin + header_h + r * (panel_h + label_h + gap)

        # 嵌入图像为无损 PNG base64 (逐像素保真, 仍可被 Illustrator 编辑)
        b64 = base64.b64encode(_encode_lossless(img).getvalue()).decode('ascii')
        data_uri = f'data:image/png;base64,{b64}'
        img_el = ET.SubElement(svg, 'image', {
            'x': str(x), 'y': str(y + label_h),
            'width': str(panel_w), 'height': str(panel_h),
            'xlink:href': data_uri,
            'href': data_uri
        })

        # 标签
        meta = panel_meta.get(key, {})
        label = meta.get('label', '')
        color = meta.get('color', (0, 0, 0))
        if label:
            label_font = int(font_size) if font_size else max(12, int(panel_w * 0.035))
            text = ET.SubElement(svg, 'text', {
                'x': str(x), 'y': str(y + label_h - 3),
                'fill': f'rgb({color[0]},{color[1]},{color[2]})',
                'font-family': 'Arial, Helvetica, sans-serif',
                'font-size': str(label_font)
            })
            text.text = label

    # Pearson
    if pearson_data:
        r_val = pearson_data.get('pearson_r', 0)
        n_val = pearson_data.get('n_pixels', 0)
        pearson_font = int(font_size * 0.85) if font_size else max(11, int(panel_w * 0.03))
        text = ET.SubElement(svg, 'text', {
            'x': str(total_w // 2 - 100), 'y': str(total_h - 8),
            'fill': 'black', 'font-family': 'Arial, Helvetica, sans-serif',
            'font-size': str(pearson_font)
        })
        text.text = f'Pearson r = {r_val:.4f}, N = {n_val}'

    xml_str = ET.tostring(svg, encoding='utf-8')
    pretty = minidom.parseString(xml_str).toprettyxml(indent='  ')
    buf = io.BytesIO(pretty.encode('utf-8'))
    buf.seek(0)
    return buf


def _draw_top_header_svg(svg_root, lines, total_w, header_h, margin, font_size, panel_w, align='center', image_top=None):
    """SVG: 顶部一行多色文本 (斜杠分隔); align: center/left;
    image_top: 首行图片顶部 y (px), 传入后标签 baseline 与图片保持 1px (SVG y 为 baseline)"""
    if not lines:
        return
    label_font = int(font_size) if font_size else max(12, int(panel_w * 0.035))
    sep_text = ' / '
    # 估算宽度: Arial 中 char 宽 ~0.55*font_size (粗略)
    sep_w = int(len(sep_text) * label_font * 0.55)
    seg_widths = [int(len(text) * label_font * 0.55) for text, _ in lines]
    total_text_w = sum(seg_widths) + sep_w * (len(lines) - 1)
    if align == 'left':
        x = margin
    else:
        x = max(margin, (total_w - total_text_w) // 2)
    if image_top:
        y = image_top - 1
    else:
        y = margin + max(2, header_h * 0.4)
    for i, (text, color) in enumerate(lines):
        t = ET.SubElement(svg_root, 'text', {
            'x': str(x), 'y': str(y),
            'fill': f'rgb({color[0]},{color[1]},{color[2]})',
            'font-family': 'Arial, Helvetica, sans-serif',
            'font-size': str(label_font)
        })
        t.text = text
        x += seg_widths[i]
        if i < len(lines) - 1:
            t = ET.SubElement(svg_root, 'text', {
                'x': str(x), 'y': str(y),
                'fill': 'rgb(120,120,120)',
                'font-family': 'Arial, Helvetica, sans-serif',
                'font-size': str(label_font)
            })
            t.text = sep_text
            x += sep_w


def _build_svg_merge_left(panels, panel_w, panel_h, gap, margin, header_h,
                           font_size, top_header_lines, pearson_data, header_align='center'):
    """SVG: Merge 大图左 + 其余面板竖排右 (各按自身比例)"""
    merge_data, right_panels = _split_merge_left(panels, panel_w, margin)
    if merge_data is None:
        return None
    merge_img, merge_w, merge_h, right_total_h, small_w, right_heights, gap_eff = merge_data
    total_w = 2 * margin + merge_w + gap_eff + small_w
    total_h = 2 * margin + right_total_h + header_h

    svg = ET.Element('svg', {
        'xmlns': 'http://www.w3.org/2000/svg',
        'xmlns:xlink': 'http://www.w3.org/1999/xlink',
        'width': str(total_w),
        'height': str(total_h),
        'viewBox': f'0 0 {total_w} {total_h}'
    })
    ET.SubElement(svg, 'rect', {'x': '0', 'y': '0', 'width': str(total_w), 'height': str(total_h), 'fill': 'white'})
    if top_header_lines:
        _draw_top_header_svg(svg, top_header_lines, total_w, header_h, margin, font_size, panel_w, align=header_align, image_top=margin + header_h)

    def _embed(img):
        b64 = base64.b64encode(_encode_lossless(img).getvalue()).decode('ascii')
        return f'data:image/png;base64,{b64}'

    # Merge 左大格
    ET.SubElement(svg, 'image', {
        'x': str(margin), 'y': str(margin + header_h),
        'width': str(merge_w), 'height': str(merge_h),
        'xlink:href': _embed(merge_img), 'href': _embed(merge_img)
    })
    # 右列
    cursor = margin + header_h
    for i, (_k, img) in enumerate(right_panels):
        sh = right_heights[i]
        x = margin + merge_w + gap_eff
        y = cursor
        ET.SubElement(svg, 'image', {
            'x': str(x), 'y': str(y),
            'width': str(small_w), 'height': str(sh),
            'xlink:href': _embed(img), 'href': _embed(img)
        })
        cursor += sh + gap_eff

    xml_str = ET.tostring(svg, encoding='utf-8')
    pretty = minidom.parseString(xml_str).toprettyxml(indent='  ')
    buf = io.BytesIO(pretty.encode('utf-8'))
    buf.seek(0)
    return buf


def _svg_scale_bar(W, H, scale_bar_config, pixel_size_um, disp_scale=1.0):
    """计算 Scale Bar 的 SVG 几何 (复用 add_scale_bar 的坐标逻辑), 供结构化矢量导出。

    v1.4.0 起按 pt 单位算: 厚度默认 1pt (Illustrator), 字号默认自动 (与 bar 长度成比例);
    返回的 fs/thickness 已是 px (矢量层 font-size/stroke-width 直接吃 px)。
    disp_scale: 用户单位/显示pt 比例 (SVG 默认 100pt 显示时 >1), 仅缩放厚度/字号/边距等
    "显示属性", bar 的物理像素长度 bar_px 不缩放, 保证比例尺物理含义不变。
    """
    length_um = scale_bar_config.get('length_um', 50)
    # v1.4.0: 优先 pt; 兼容旧 px 字段; disp_scale 让 100pt 默认显示下仍呈 1pt 视觉
    if 'thickness_pt' in scale_bar_config:
        thickness_px = pt_to_px(scale_bar_config['thickness_pt'], dpi=72) * disp_scale
    else:
        thickness_px = int(scale_bar_config.get('thickness', 3)) * disp_scale
    # font_size
    if scale_bar_config.get('font_size_auto', True):
        fs_pt = compute_scale_bar_font_pt(length_um, pixel_size_um) or 12.0
    elif 'font_size_pt' in scale_bar_config and scale_bar_config['font_size_pt'] is not None:
        fs_pt = float(scale_bar_config['font_size_pt'])
    else:
        # 旧 px 字段直接当 px 用
        fs_pt = float(scale_bar_config.get('font_size', 14))
    fs_px = pt_to_px(fs_pt, dpi=72) * disp_scale

    color = scale_bar_config.get('color', 'white')
    position = scale_bar_config.get('position', 'bottom-right')
    if pixel_size_um and pixel_size_um > 0:
        bar_px = max(int(length_um / pixel_size_um), thickness_px + 2)
    else:
        bar_px = int(length_um)
    margin = max(15, fs_px + 5)
    if position == 'bottom-right':
        x_start = W - bar_px - margin
        y_start = H - margin - thickness_px - fs_px - 5
    elif position == 'bottom-left':
        x_start = margin
        y_start = H - margin - thickness_px - fs_px - 5
    elif position == 'top-right':
        x_start = W - bar_px - margin
        y_start = margin
    elif position == 'top-left':
        x_start = margin
        y_start = margin
    else:
        x_start = W - bar_px - margin
        y_start = H - margin - thickness_px - fs_px - 5
    bar_y = y_start + fs_px + 5
    return {
        'x': x_start, 'y': bar_y, 'w': bar_px, 'th': thickness_px,
        'color': color, 'fs': fs_px, 'text_y': y_start + fs_px, 'label': f'{length_um} μm'
    }


def _svg_inset(base_img, roi_config, inset_config):
    """计算 Inset 放大图的 SVG 几何 (复用 _apply_inset_overlay 的坐标逻辑), 返回裁切图 base64"""
    x, y, w, h = int(roi_config['x']), int(roi_config['y']), int(roi_config['width']), int(roi_config['height'])
    img_w, img_h = base_img.size
    x2, y2 = min(x + w, img_w), min(y + h, img_h)
    x, y, w, h = max(0, x), max(0, y), x2 - x, y2 - y
    if w <= 0 or h <= 0:
        return None
    zoom = float(inset_config.get('zoom', 2.0))
    if zoom <= 0.1:
        zoom = 1.0
    inset_w = int(w * zoom)
    inset_h = int(h * zoom)
    if inset_w < 10 or inset_h < 10:
        return None
    crop = base_img.crop((x, y, x + w, y + h)).resize((inset_w, inset_h), Image.LANCZOS)
    margin = max(8, int(min(img_w, img_h) * 0.015))
    position = inset_config.get('position', 'top-right')
    if position == 'top-right':
        px, py = img_w - inset_w - margin, margin
    elif position == 'top-left':
        px, py = margin, margin
    elif position == 'bottom-right':
        px, py = img_w - inset_w - margin, img_h - inset_h - margin
    else:
        px, py = margin, img_h - inset_h - margin
    px = max(0, min(px, img_w - inset_w))
    py = max(0, min(py, img_h - inset_h))
    b64 = base64.b64encode(_encode_lossless(crop).getvalue()).decode('ascii')
    uri = f'data:image/png;base64,{b64}'
    border_w = max(2, int(min(inset_w, inset_h) * 0.02))
    return {'x': px, 'y': py, 'w': inset_w, 'h': inset_h, 'uri': uri, 'bw': border_w, 'crop': crop}


def _svg_display_dims(W, H, display_pt):
    """计算 SVG 根 <svg> 的显示尺寸 (pt) 与 用户单位/显示pt 比例 sc。

    display_pt<=0 或 None -> 原生尺寸 (宽=W px, sc=1, AI 打开为 W pt, 与旧行为一致);
    否则将较长边映射为 display_pt pt, 保持长宽比; 底图 PNG 始终原生分辨率无损嵌入,
    仅"显示画布"缩小到 100pt 方便 Illustrator 打开, 不丢失任何像素。
    """
    if not display_pt or display_pt <= 0:
        return float(W), float(H), 1.0
    if W >= H:
        dw = float(display_pt)
        dh = float(display_pt) * H / W
    else:
        dh = float(display_pt)
        dw = float(display_pt) * W / H
    sc = (W / dw) if dw else 1.0   # == H/dh, 用户单位/显示pt
    return dw, dh, sc


def _svg_layout_display_dims(total_w, total_h, panel_w, display_pt):
    """排版 (多图) SVG 的画布尺寸: display_pt 指「每张单图(面板)」的宽度 pt, 不是整幅画布。

    旧实现把整幅画布的长边映射到 display_pt, 于是一行 N 张图时每张单图被压成
    display_pt/N 级别 (用户 2026-09-17 实测: 4 张 2048px 面板 → 每张仅 24.8846pt)。
    现在按面板宽度定标: k = display_pt / panel_w → 画布 = (total_w*k, total_h*k),
    单图宽度恰为 display_pt, 与单图导出的「100pt」语义一致。

    返回 (dw, dh, sc); sc 仍是 用户单位/显示pt = total_w/dw, 覆盖层按它缩放 → 呈现值不变。
    display_pt<=0 或 panel_w 缺失时退回 _svg_display_dims (含 <=0 的原生尺寸分支)。
    """
    try:
        pw = float(panel_w)
    except (TypeError, ValueError):
        pw = 0.0
    if display_pt and display_pt > 0 and pw > 0:
        k = float(display_pt) / pw
        dw, dh = total_w * k, total_h * k
        sc = (total_w / dw) if dw else 1.0
        return dw, dh, sc
    return _svg_display_dims(total_w, total_h, display_pt)


# ---- 面板标签字号: 统一以「一张单图(通道图/merge)的宽度」为基准 ----
# 单图 100pt 时标签 = 7.5pt (清晰合适); 文字过长则自动缩小, 保证不超出一张单图宽。
LABEL_FONT_RATIO = 0.075    # 标签字号 = 单图宽度 × 7.5%
LABEL_LINE_RATIO = 1.45     # 标签条高 = 字号 × 1.45
LABEL_FONT_MIN = 2.0        # 极端超长标签的兜底下限 (user units)
_TXT_MEASURE_CACHE = {}


def _measure_text_units(text, font_units):
    """量文字宽度 (单位 = user units，与源图像 px 同尺度)。用与导出同款 Arial 精确测量;
    取不到字体时退回「0.55 × 字号 × 字数」粗估 (与 _draw_top_header_svg 的估法一致)。"""
    text = text or ''
    if not text:
        return 0.0
    size = int(max(1, round(font_units)))
    font = _TXT_MEASURE_CACHE.get(size)
    if font is None:
        font = False
        try:
            from PIL import ImageFont
            try:
                font = ImageFont.truetype('arial.ttf', size)
            except Exception:
                font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', size)
        except Exception:
            font = False
        _TXT_MEASURE_CACHE[size] = font                # False = 不可用, 走粗估
    if font is False:
        return 0.55 * font_units * len(text)
    try:
        return float(font.getlength(text))
    except Exception:
        return 0.55 * font_units * len(text)


def _label_font_units(label, panel_w):
    """面板标签字号 (user units): 基准 = 一张单图的宽度 panel_w。
    ① 默认 7.5% × panel_w (单图显示 100pt 时即 7.5pt, 清晰合适);
    ② 若按该字号量出的文字宽度超过一张单图宽, 等比缩小到恰好不超宽 (用户要求「不超过一张图宽」)。
    """
    try:
        pw = float(panel_w)
    except (TypeError, ValueError):
        pw = 0.0
    if pw <= 0:
        return 12.0
    base = LABEL_FONT_RATIO * pw
    if not label:
        return base
    w = _measure_text_units(label, base)
    if w <= pw or w <= 0:
        return base
    return max(LABEL_FONT_MIN, base * pw / w)


def _panel_labels(panels, panel_meta, panel_w):
    """为每个面板算 (标签文字, 字号 user units), 并给出所需标签条高 (user units)。"""
    labels, max_fs = {}, 0.0
    for key, _img in panels:
        meta = (panel_meta or {}).get(key, {}) or {}
        lab = meta.get('label', '') or ''
        fs_units = _label_font_units(lab, panel_w)
        labels[key] = (lab, fs_units, meta.get('color', (0, 0, 0)))
        if lab:
            max_fs = max(max_fs, fs_units)
    return labels, (int(math.ceil(max_fs * LABEL_LINE_RATIO)) if max_fs else 0)


def _roi_stroke_pt(roi_config, scale_bar_config):
    """ROI 描边线宽 (pt): 单独指定优先 (roi.stroke_width_pt), 否则沿用界面「线宽」设置
    (scale bar thickness_pt), 都取不到则 1.0。供 SVG 单图与排版导出共用。"""
    pt = (roi_config or {}).get('stroke_width_pt')
    if pt is None and scale_bar_config:
        pt = scale_bar_config.get('thickness_pt')
    try:
        pt = float(pt)
    except (TypeError, ValueError):
        pt = 1.0
    return pt if pt > 0 else 1.0


def _build_svg_single_structured(base_img, roi_config, scale_bar_config, inset_config,
                                 pixel_size_um, panel_type, dpi=300, svg_display_pt=100):
    """结构化 SVG 单图导出: 底图 + ROI / Scale Bar / Inset 各自为独立 <g> 组,
    可在 Illustrator / Inkscape 中作为独立图层自由移动/编辑 (而非一整张扁平栅格)。

    base_img: 未叠加任何 overlay 的原始合并/单通道图 (RGB)。
    svg_display_pt: AI 打开时的默认画布尺寸 (较长边 pt), 默认 100; <=0 用原生像素。
    底图无损嵌入原生分辨率, viewBox 保留全分辨率坐标系, ROI/标尺/Inset 的描边与字号
    按 sc (用户单位/显示pt) 缩放, 故 100pt 画布下呈现 1pt 视觉线宽, 同时像素零丢失。
    """
    W, H = base_img.size
    dw, dh, sc = _svg_display_dims(W, H, svg_display_pt)
    if svg_display_pt and svg_display_pt > 0:
        # 单图统一画板: 宽与高都固定为设定值 (默认 100pt), 与原始长宽比无关。
        # 否则非正方形图会导出 100x50 之类的画板, 多图排版时各块尺寸参差不齐。
        # 内容由 preserveAspectRatio 等比缩放居中, 绝不拉伸变形;
        # 且 sc 仍 = max(W,H)/display_pt, 线宽/字号按 sc 缩放后呈现值不变。
        dw = dh = float(svg_display_pt)
    svg = ET.Element('svg', {
        'xmlns': 'http://www.w3.org/2000/svg',
        'xmlns:xlink': 'http://www.w3.org/1999/xlink',
        'width': f'{dw:g}pt', 'height': f'{dh:g}pt',
        'viewBox': f'0 0 {W} {H}',
        'preserveAspectRatio': 'xMidYMid meet'
    })
    # 不再加「等画布的白底」rect (2026-09-17 用户要求去掉): 底图 <image> 已覆盖整个 viewBox,
    # 该白底纯属多余; 非正方形图在方形画布下还会露出白色 letterbox 条。

    # 1) 底图 (无损嵌入, 独立组, 可单独移动)
    g_base = ET.SubElement(svg, 'g', {'id': 'microscope-image', 'class': 'ecs-layer'})
    b64 = base64.b64encode(_encode_lossless(base_img).getvalue()).decode('ascii')
    uri = f'data:image/png;base64,{b64}'
    ET.SubElement(g_base, 'image', {
        'x': '0', 'y': '0', 'width': str(W), 'height': str(H),
        'xlink:href': uri, 'href': uri
    })

    # 2) ROI 选框 (矢量, 独立组, 可移动; 默认 1pt)
    # 虚线: 界面 canvas 为 setLineDash([5,3]) @ 线宽2 → 比例 dash=2.5×线宽, gap=1.5×线宽;
    # dasharray 写在图像用户单位 (乘 sc 抵消 viewBox 缩放), AI 中呈现值恒为 2.5pt/1.5pt, 缩放不丢失
    is_main = panel_type in ('channel', 'merge') or (isinstance(panel_type, str) and panel_type.startswith('ch'))
    if roi_config and roi_config.get('width', 0) > 0 and roi_config.get('height', 0) > 0 and is_main:
        x, y, w, h = int(roi_config['x']), int(roi_config['y']), int(roi_config['width']), int(roi_config['height'])
        x2, y2 = min(x + w, W), min(y + h, H)
        x, y, w, h = max(0, x), max(0, y), x2 - x, y2 - y
        if w > 0 and h > 0:
            line_color = _as_rgb(roi_config.get('color')) if roi_config.get('color') else (255, 255, 255)
            lc = f'rgb({line_color[0]},{line_color[1]},{line_color[2]})'
            # 线宽 (pt): 单独指定优先, 否则沿用界面「线宽」设置, 缺省 1.0。
            # 乘 sc 抵消画布缩放 → 最终呈现恰为设定 pt 值, 不会被缩放/坐标变换拉伸、加粗或压扁。
            roi_pt = _roi_stroke_pt(roi_config, scale_bar_config)
            # 虚线: dash/gap 与线宽同比 (dash=2.5×线宽, gap=1.5×线宽, 同界面 [5,3]@2px), 乘 sc 抵消缩放。
            # 切勿写 "3,0" (间隔 0 = 实线): 旧版即因该写法在 Illustrator 中丢虚线。
            _sw = roi_pt * sc
            g_roi = ET.SubElement(svg, 'g', {'id': 'roi', 'class': 'ecs-layer'})
            ET.SubElement(g_roi, 'rect', {
                'x': str(x), 'y': str(y), 'width': str(w - 1), 'height': str(h - 1),
                'fill': 'none', 'stroke': lc, 'stroke-width': f'{_sw:g}',
                'stroke-dasharray': f'{_sw * 2.5:g},{_sw * 1.5:g}',
                'stroke-linecap': 'butt'
            })

    # 3) Scale Bar (矢量, 独立组, 可移动)
    if scale_bar_config:
        bar = _svg_scale_bar(W, H, scale_bar_config, pixel_size_um, disp_scale=sc)
        if bar:
            g_sb = ET.SubElement(svg, 'g', {'id': 'scale-bar', 'class': 'ecs-layer'})
            ET.SubElement(g_sb, 'rect', {
                'x': str(bar['x']), 'y': str(bar['y']),
                'width': str(bar['w']), 'height': str(bar['th']), 'fill': bar['color']
            })
            t = ET.SubElement(g_sb, 'text', {
                'x': str(bar['x']), 'y': str(bar['text_y']),
                'fill': bar['color'], 'font-family': 'Arial, Helvetica, sans-serif', 'font-size': str(bar['fs'])
            })
            t.text = bar['label']

    # 4) Inset 放大图 (独立组, 可移动)
    if inset_config and inset_config.get('enabled') and roi_config and roi_config.get('width', 0) > 0:
        ins = _svg_inset(base_img, roi_config, inset_config)
        if ins:
            g_ins = ET.SubElement(svg, 'g', {'id': 'inset', 'class': 'ecs-layer'})
            ET.SubElement(g_ins, 'image', {
                'x': str(ins['x']), 'y': str(ins['y']),
                'width': str(ins['w']), 'height': str(ins['h']),
                'xlink:href': ins['uri'], 'href': ins['uri']
            })
            # 单层白色 1pt 描边 (替代原先的外白内黑双层框, 适配 AI 编辑); 100pt 下按 sc 缩放
            ET.SubElement(g_ins, 'rect', {
                'x': str(ins['x']), 'y': str(ins['y']),
                'width': str(ins['w'] - 1), 'height': str(ins['h'] - 1),
                'fill': 'none', 'stroke': 'white', 'stroke-width': f'{sc:g}'
            })

    xml_str = ET.tostring(svg, encoding='utf-8')
    pretty = minidom.parseString(xml_str).toprettyxml(indent='  ')
    buf = io.BytesIO(pretty.encode('utf-8'))
    buf.seek(0)
    return buf


def _is_main_panel(key):
    """判断某面板是否为"主图"(通道/merge), 主图才叠加 ROI/ScaleBar/Inset"""
    return key in ('channel', 'merge', 'merged') or (isinstance(key, str) and key.startswith('ch'))


def _css_to_rgb(c):
    """CSS 颜色名 / hex / 列表 -> (r,g,b) int; 供 PDF 矢量着色用 (reportlab 无内置字符串色名)"""
    if isinstance(c, (list, tuple)) and len(c) >= 3:
        return _as_rgb(c)
    if isinstance(c, str):
        cc = c.lower().strip()
        named = {'white': (255, 255, 255), 'black': (0, 0, 0), 'red': (255, 0, 0),
                 'green': (0, 255, 0), 'blue': (0, 0, 255), 'yellow': (255, 255, 0),
                 'cyan': (0, 255, 255), 'magenta': (255, 0, 255)}
        if cc in named:
            return named[cc]
        if cc.startswith('#'):
            return _as_rgb(c)
    return _as_rgb(c)


def _overlay_geometry(base, roi_config, scale_bar_config, inset_config, pixel_size_um, is_main, disp_scale=1.0):
    """统一计算某面板 (底图 base, 未叠加) 的 ROI/ScaleBar/Inset 几何, 坐标系为 base 的像素空间 (image space)。
    返回 dict, 各模块为 None 表示不绘制。SVG 与 PDF 结构化导出共用, 保证与栅格叠加同一套坐标算法。
    disp_scale: 用户单位/显示pt (SVG 100pt 默认显示时 >1), 仅影响 scale bar 的厚度/字号显示属性。"""
    W, H = base.size
    geom = {'roi': None, 'scale_bar': None, 'inset': None}
    # ROI
    if is_main and roi_config and roi_config.get('width', 0) > 0 and roi_config.get('height', 0) > 0:
        x, y, w, h = int(roi_config['x']), int(roi_config['y']), int(roi_config['width']), int(roi_config['height'])
        x2, y2 = min(x + w, W), min(y + h, H)
        x, y, w, h = max(0, x), max(0, y), x2 - x, y2 - y
        if w > 0 and h > 0:
            # 默认白色, 虚线 dash=2.5×线宽 / gap=1.5×线宽 (对应界面 [5,3]@2px 比例)
            line_color = _as_rgb(roi_config.get('color')) if roi_config.get('color') else (255, 255, 255)
            geom['roi'] = {'x': x, 'y': y, 'w': w, 'h': h, 'color': line_color}
    # Scale bar
    if scale_bar_config:
        geom['scale_bar'] = _svg_scale_bar(W, H, scale_bar_config, pixel_size_um, disp_scale=disp_scale)
    # Inset
    if inset_config and inset_config.get('enabled') and roi_config and roi_config.get('width', 0) > 0:
        geom['inset'] = _svg_inset(base, roi_config, inset_config)
    return geom


# ===================== 结构化 SVG 排版导出 (各模块独立可编辑) =====================

def _svg_add_overlay_groups(svg_root, geom, ox, oy, s, pid, disp_scale=1.0, roi_stroke_pt=1.0):
    """把 ROI / Scale Bar / Inset 作为独立 <g> 叠加到结构化 SVG。
    坐标在 image space, 用 transform="translate(ox,oy) scale(s)" 定位 (s = 显示/原图缩放, 原生尺寸时=1)。
    disp_scale: 用户单位/显示pt, 仅缩放 ROI/Inset 的描边线宽 (100pt 默认显示时 >1)。
    roi_stroke_pt: ROI 描边目标线宽 (pt), 乘 disp_scale 后最终呈现恰为该 pt 值。"""
    if not (geom['roi'] or geom['scale_bar'] or geom['inset']):
        return
    tr = f'translate({ox},{oy}) scale({s})'
    # ROI 选框 (独立图层; 虚线 dash=2.5×线宽 / gap=1.5×线宽, 与界面 [5,3]@2px 同比例)
    if geom['roi']:
        r = geom['roi']
        g = ET.SubElement(svg_root, 'g', {'id': f'{pid}-roi', 'class': 'ecs-layer', 'transform': tr})
        lc = f'rgb({r["color"][0]},{r["color"][1]},{r["color"][2]})'
        _sw = roi_stroke_pt * disp_scale
        ET.SubElement(g, 'rect', {
            'x': str(r['x']), 'y': str(r['y']), 'width': str(r['w'] - 1), 'height': str(r['h'] - 1),
            'fill': 'none', 'stroke': lc, 'stroke-width': f'{_sw:g}',
            'stroke-dasharray': f'{_sw * 2.5:g},{_sw * 1.5:g}',
            'stroke-linecap': 'butt'
        })
    # Scale bar (独立图层)
    if geom['scale_bar']:
        b = geom['scale_bar']
        g = ET.SubElement(svg_root, 'g', {'id': f'{pid}-scalebar', 'class': 'ecs-layer', 'transform': tr})
        ET.SubElement(g, 'rect', {
            'x': str(b['x']), 'y': str(b['y']), 'width': str(b['w']), 'height': str(b['th']), 'fill': b['color']
        })
        t = ET.SubElement(g, 'text', {
            'x': str(b['x']), 'y': str(b['text_y']),
            'fill': b['color'], 'font-family': 'Arial, Helvetica, sans-serif', 'font-size': str(b['fs'])
        })
        t.text = b['label']
    # Inset 放大图 (独立图层)
    if geom['inset']:
        ins = geom['inset']
        g = ET.SubElement(svg_root, 'g', {'id': f'{pid}-inset', 'class': 'ecs-layer', 'transform': tr})
        ET.SubElement(g, 'image', {
            'x': str(ins['x']), 'y': str(ins['y']), 'width': str(ins['w']), 'height': str(ins['h']),
            'xlink:href': ins['uri'], 'href': ins['uri']
        })
        # 单层白色 1pt 描边 (替代原先的外白内黑双层框, 适配 AI 编辑); 100pt 下按 disp_scale 缩放
        ET.SubElement(g, 'rect', {
            'x': str(ins['x']), 'y': str(ins['y']), 'width': str(ins['w'] - 1), 'height': str(ins['h'] - 1),
            'fill': 'none', 'stroke': 'white', 'stroke-width': f'{disp_scale:g}'
        })


def _build_svg_structured(panels, panel_w, panel_h, label_h, gap, cols, panel_meta,
                          scale_bar_config, inset_config, roi_config, pixel_size_um, dpi,
                          pearson_data=None, margin=10, font_size=None, layout='grid',
                          top_header_lines=None, header_align='center', native_size=True,
                          svg_display_pt=100):
    """结构化 SVG 排版导出: 每个面板的底图 + ROI / Scale Bar / Inset 各自为独立 <g> 组,
    可在 Illustrator / Inkscape 中作为独立图层自由移动/编辑 (替代原来把 overlay 烤进一张栅格的做法)。

    panels: [(key, 未叠加底图 PIL), ...]  (调用方负责不烤入 overlay)
    """
    header_h = 0
    if top_header_lines:
        fs = font_size if font_size else max(12, int(panel_w * 0.035))
        header_h = max(int(fs * 1.7), int(panel_w * 0.04)) + 6

    if layout == 'merge_left':
        return _build_svg_structured_merge_left(panels, panel_w, panel_h, gap, margin, header_h,
                                                font_size, top_header_lines, scale_bar_config,
                                                inset_config, roi_config, pixel_size_um, dpi,
                                                pearson_data, header_align)

    n = len(panels)
    rows = math.ceil(n / cols)
    # 面板标签 (通道名 / Merge 等): 字号以「一张单图宽」为基准, 用最大字号撑出标签条高
    panel_labels, label_h_needed = _panel_labels(panels, panel_meta, panel_w)
    label_h = max(label_h, label_h_needed)
    total_w = cols * panel_w + (cols - 1) * gap + 2 * margin
    total_h = rows * (panel_h + label_h) + (rows - 1) * gap + 2 * margin + header_h
    if pearson_data:
        total_h += label_h + 10

    # svg_display_pt = 每张单图的显示宽度 pt (不是整幅画布), 见 _svg_layout_display_dims
    dw, dh, sc = _svg_layout_display_dims(total_w, total_h, panel_w, svg_display_pt)
    roi_pt = _roi_stroke_pt(roi_config, scale_bar_config)
    svg = ET.Element('svg', {
        'xmlns': 'http://www.w3.org/2000/svg',
        'xmlns:xlink': 'http://www.w3.org/1999/xlink',
        'width': f'{dw:g}pt', 'height': f'{dh:g}pt',
        'viewBox': f'0 0 {total_w} {total_h}'
    })
    # 不再加等画布白底 rect (2026-09-17 用户要求去掉)

    if top_header_lines:
        _draw_top_header_svg(svg, top_header_lines, total_w, header_h, margin, font_size, panel_w,
                             align=header_align, image_top=margin + header_h + label_h)

    for i, (key, base) in enumerate(panels):
        r, c = i // cols, i % cols
        ox = margin + c * (panel_w + gap)
        oy = margin + header_h + r * (panel_h + label_h + gap) + label_h
        s = 1.0
        disp = base
        if not native_size and (base.width != panel_w or base.height != panel_h):
            s = panel_w / float(base.width) if base.width else 1.0
            disp = base.resize((panel_w, panel_h))
        # 底图 (独立组)
        g_img = ET.SubElement(svg, 'g', {'id': f'panel-{i}-{key}', 'class': 'ecs-layer'})
        b64 = base64.b64encode(_encode_lossless(disp).getvalue()).decode('ascii')
        uri = f'data:image/png;base64,{b64}'
        ET.SubElement(g_img, 'image', {
            'x': str(ox), 'y': str(oy), 'width': str(panel_w), 'height': str(panel_h),
            'xlink:href': uri, 'href': uri
        })
        # overlay 各模块独立组
        if _is_main_panel(key):
            geom = _overlay_geometry(base, roi_config, scale_bar_config, inset_config, pixel_size_um, True, disp_scale=sc)
            _svg_add_overlay_groups(svg, geom, ox, oy, s, f'panel-{i}', disp_scale=sc, roi_stroke_pt=roi_pt)
        # 面板标签 (通道名 / Merge 等): 独立图层, 可在 AI 中单独编辑; 字号基准 = 一张单图宽
        lab, fs_units, lab_col = panel_labels.get(key, ('', 0.0, (0, 0, 0)))
        if lab:
            g_lab = ET.SubElement(svg, 'g', {'id': f'panel-{i}-label', 'class': 'ecs-layer'})
            t = ET.SubElement(g_lab, 'text', {
                'x': str(ox), 'y': str(int(oy - max(2, fs_units * 0.28))),
                'fill': 'rgb(%d,%d,%d)' % (int(lab_col[0]), int(lab_col[1]), int(lab_col[2])),
                'font-family': 'Arial, Helvetica, sans-serif',
                'font-size': f'{fs_units:g}'
            })
            t.text = lab

    if pearson_data:
        r_val = pearson_data.get('pearson_r', 0)
        n_val = pearson_data.get('n_pixels', 0)
        pearson_font = int(font_size * 0.85) if font_size else max(11, int(panel_w * 0.03))
        text = ET.SubElement(svg, 'text', {
            'x': str(total_w // 2 - 100), 'y': str(total_h - 8),
            'fill': 'black', 'font-family': 'Arial, Helvetica, sans-serif', 'font-size': str(pearson_font)
        })
        text.text = f'Pearson r = {r_val:.4f}, N = {n_val}'

    xml_str = ET.tostring(svg, encoding='utf-8')
    pretty = minidom.parseString(xml_str).toprettyxml(indent='  ')
    buf = io.BytesIO(pretty.encode('utf-8'))
    buf.seek(0)
    return buf


def _build_svg_structured_merge_left(panels, panel_w, panel_h, gap, margin, header_h,
                                     font_size, top_header_lines, scale_bar_config, inset_config,
                                     roi_config, pixel_size_um, dpi, pearson_data, header_align='center',
                                     svg_display_pt=100):
    merge_data, right_panels = _split_merge_left(panels, panel_w, margin)
    if merge_data is None:
        return None
    merge_img, merge_w, merge_h, right_total_h, small_w, right_heights, gap_eff = merge_data
    total_w = 2 * margin + merge_w + gap_eff + small_w
    total_h = 2 * margin + right_total_h + header_h

    # svg_display_pt = 每张单图的显示宽度 pt (不是整幅画布), 见 _svg_layout_display_dims
    dw, dh, sc = _svg_layout_display_dims(total_w, total_h, panel_w, svg_display_pt)
    roi_pt = _roi_stroke_pt(roi_config, scale_bar_config)
    svg = ET.Element('svg', {
        'xmlns': 'http://www.w3.org/2000/svg', 'xmlns:xlink': 'http://www.w3.org/1999/xlink',
        'width': f'{dw:g}pt', 'height': f'{dh:g}pt', 'viewBox': f'0 0 {total_w} {total_h}'
    })
    # 不再加等画布白底 rect (2026-09-17 用户要求去掉)
    if top_header_lines:
        _draw_top_header_svg(svg, top_header_lines, total_w, header_h, margin, font_size, panel_w,
                             align=header_align, image_top=margin + header_h)

    def _embed(img):
        b64 = base64.b64encode(_encode_lossless(img).getvalue()).decode('ascii')
        return f'data:image/png;base64,{b64}'

    # Merge 左大格
    g = ET.SubElement(svg, 'g', {'id': 'panel-merge', 'class': 'ecs-layer'})
    ET.SubElement(g, 'image', {
        'x': str(margin), 'y': str(margin + header_h), 'width': str(merge_w), 'height': str(merge_h),
        'xlink:href': _embed(merge_img), 'href': _embed(merge_img)
    })
    if _is_main_panel('merge'):
        geom = _overlay_geometry(merge_img, roi_config, scale_bar_config, inset_config, pixel_size_um, True, disp_scale=sc)
        _svg_add_overlay_groups(svg, geom, margin, margin + header_h,
                                merge_w / float(merge_img.width) if merge_img.width else 1.0, 'panel-merge',
                                disp_scale=sc, roi_stroke_pt=roi_pt)

    # 右列
    cursor = margin + header_h
    for i, (k, img) in enumerate(right_panels):
        sh = right_heights[i]
        x = margin + merge_w + gap_eff
        y = cursor
        g = ET.SubElement(svg, 'g', {'id': f'panel-{i}-{k}', 'class': 'ecs-layer'})
        ET.SubElement(g, 'image', {
            'x': str(x), 'y': str(y), 'width': str(small_w), 'height': str(sh),
            'xlink:href': _embed(img), 'href': _embed(img)
        })
        if _is_main_panel(k):
            geom = _overlay_geometry(img, roi_config, scale_bar_config, inset_config, pixel_size_um, True, disp_scale=sc)
            _svg_add_overlay_groups(svg, geom, x, y, small_w / float(img.width) if img.width else 1.0, f'panel-{i}', disp_scale=sc, roi_stroke_pt=roi_pt)
        cursor += sh + gap_eff

    xml_str = ET.tostring(svg, encoding='utf-8')
    pretty = minidom.parseString(xml_str).toprettyxml(indent='  ')
    buf = io.BytesIO(pretty.encode('utf-8'))
    buf.seek(0)
    return buf


# ===================== 结构化 PDF 排版导出 (各模块独立可编辑) =====================

def _pdf_draw_overlays(cvs, geom, x_pt, y_pt, disp_w, disp_h, base, dpi):
    """reportlab: 在画布上把 ROI/ScaleBar/Inset 作为独立矢量对象绘制 (坐标 image space -> PDF 点, 含 y 翻转)。"""
    pp = 72.0 / dpi
    scale_pp = disp_w / float(base.width) if base.width else 1.0

    def to_pts(rx, ry, rw, rh):
        dx = rx * scale_pp * pp
        dyb = (disp_h - (ry + rh) * scale_pp) * pp
        return x_pt + dx, y_pt + dyb, rw * scale_pp * pp, rh * scale_pp * pp

    # ROI 选框 (线宽固定 1pt, 不随画布缩放; 与 SVG 同为真虚线)
    if geom['roi']:
        r = geom['roi']
        lw_pt = 1.0
        cx, cy, cw, ch = to_pts(r['x'], r['y'], r['w'] - 1, r['h'] - 1)
        cvs.setStrokeColorRGB(r['color'][0] / 255.0, r['color'][1] / 255.0, r['color'][2] / 255.0)
        cvs.setLineWidth(lw_pt)
        # 真虚线: dash=2.5×线宽, gap=1.5×线宽 (同界面 setLineDash([5,3])@2px 与 SVG 导出比例);
        # setDash 单位即 PDF 点, 与 setLineWidth(1pt) 同比 → 呈现 2.5pt / 1.5pt。
        # 切勿写 [3, 0] (间隔 0 = 实线, 用户实测 PDF 中看不到虚线)。
        cvs.setDash([lw_pt * 2.5, lw_pt * 1.5], 0)
        cvs.rect(cx, cy, cw, ch, stroke=1, fill=0)
        cvs.setDash()
    # Scale bar
    if geom['scale_bar']:
        b = geom['scale_bar']
        bx, by, bw, bh = to_pts(b['x'], b['y'], b['w'], b['th'])
        col = _css_to_rgb(b['color'])
        cvs.setFillColorRGB(col[0] / 255.0, col[1] / 255.0, col[2] / 255.0)
        cvs.rect(bx, by, bw, bh, stroke=0, fill=1)
        tx, ty, _, _ = to_pts(b['x'], b['text_y'], 0, 0)
        cvs.setFont(_SCALE_BAR_PDF_FONT, max(4, b['fs'] * scale_pp * pp))
        cvs.drawString(tx, ty, b['label'])
    # Inset 放大图 (单层白色 1pt 描边, 替代原先的外白内黑双层框)
    if geom['inset']:
        ins = geom['inset']
        ix, iy, iw, ih = to_pts(ins['x'], ins['y'], ins['w'], ins['h'])
        crop_png = _encode_lossless(ins['crop']).getvalue()
        cvs.drawImage(ImageReader(io.BytesIO(crop_png)), ix, iy, width=iw, height=ih, preserveAspectRatio=True)
        cvs.setStrokeColorRGB(1, 1, 1)
        cvs.setLineWidth(1)  # 1pt 固定 (不随图像缩放)
        cvs.rect(ix, iy, iw, ih, stroke=1, fill=0)


def _build_pdf_single_structured(base_img, roi_config, scale_bar_config, inset_config,
                                 pixel_size_um, panel_type, dpi=300, svg_display_pt=100):
    """结构化 PDF 单图导出: 底图无损嵌入 + ROI/标尺/Inset 独立矢量对象 (可在 Illustrator 编辑)。

    svg_display_pt: 画布尺寸 (较长边 pt), 默认 100, 与 SVG 导出一致; <=0 时画布=原生像素 pt。
    底图 PNG 始终原生分辨率无损嵌入, 仅画布显示尺寸变化, 像素零丢失。
    """
    W, H = base_img.size
    dw, dh, sc = _svg_display_dims(W, H, svg_display_pt)
    buf = io.BytesIO()
    cvs = pdf_canvas.Canvas(buf, pagesize=(dw, dh))
    cvs.drawImage(ImageReader(_encode_lossless(base_img)), 0, 0, width=dw, height=dh,
                  preserveAspectRatio=True)
    if _is_main_panel(panel_type):
        # disp_scale=sc + dpi=72: 标尺厚度/字号换算后恰为设定 pt 值 (与 SVG 100pt 画布一致)
        geom = _overlay_geometry(base_img, roi_config, scale_bar_config, inset_config,
                                 pixel_size_um, True, disp_scale=sc)
        _pdf_draw_overlays(cvs, geom, 0, 0, dw, dh, base_img, 72)
    cvs.save()
    buf.seek(0)
    return buf


def _build_pdf_structured(panels, panel_w, panel_h, label_h, gap, cols, panel_meta,
                          scale_bar_config, inset_config, roi_config, pixel_size_um, dpi,
                          pearson_data=None, margin=10, font_size=None, layout='grid',
                          top_header_lines=None, header_align='center', native_size=True):
    """结构化 PDF 排版导出: 每个面板底图无损嵌入 + ROI/ScaleBar/Inset 为独立矢量对象 (Illustrator 可编辑)。"""
    header_h = 0
    if top_header_lines:
        fs = font_size if font_size else max(12, int(panel_w * 0.035))
        header_h = max(int(fs * 1.7 / dpi * 72), int(panel_w * 0.04 / dpi * 72)) + 4

    if layout == 'merge_left':
        return _build_pdf_structured_merge_left(panels, panel_w, panel_h, gap, margin, header_h,
                                                font_size, top_header_lines, scale_bar_config,
                                                inset_config, roi_config, pixel_size_um, dpi,
                                                pearson_data, header_align=header_align)

    n = len(panels)
    rows = math.ceil(n / cols)
    # 面板标签 (通道名 / Merge 等): 字号基准 = 一张单图宽, 用最大字号撑出标签条高
    panel_labels, label_h_needed = _panel_labels(panels, panel_meta, panel_w)
    label_h = max(label_h, label_h_needed)
    total_w_pt = (cols * panel_w + (cols - 1) * gap + 2 * margin) / dpi * 72
    total_h_pt = (rows * (panel_h + label_h) + (rows - 1) * gap + 2 * margin) / dpi * 72 + header_h
    if pearson_data:
        total_h_pt += (label_h + 10) / dpi * 72

    buf = io.BytesIO()
    cvs = pdf_canvas.Canvas(buf, pagesize=(total_w_pt, total_h_pt))

    if top_header_lines:
        _draw_top_header_pdf(cvs, top_header_lines, total_w_pt, header_h, margin, font_size, panel_w, dpi,
                             total_h_pt, align=header_align, image_top=header_h + margin + label_h + gap)

    for i, (key, base) in enumerate(panels):
        r, c = i // cols, i % cols
        x = (margin + c * (panel_w + gap)) / dpi * 72
        y = total_h_pt - header_h - (margin + (r + 1) * (panel_h + label_h + gap)) / dpi * 72
        disp = base if (native_size or (base.width == panel_w and base.height == panel_h)) else base.resize((panel_w, panel_h))
        cvs.drawImage(ImageReader(_encode_lossless(disp)), x, y,
                      width=panel_w / dpi * 72, height=panel_h / dpi * 72, preserveAspectRatio=True)
        if _is_main_panel(key):
            geom = _overlay_geometry(base, roi_config, scale_bar_config, inset_config, pixel_size_um, True)
            _pdf_draw_overlays(cvs, geom, x, y, panel_w, panel_h, base, dpi)
        # 面板标签 (通道名 / Merge 等): 字号基准 = 一张单图宽, 过长自动缩小不超宽
        lab, fs_units, lab_col = panel_labels.get(key, ('', 0.0, (0, 0, 0)))
        if lab:
            fs_pt = fs_units / dpi * 72
            cvs.setFillColorRGB(lab_col[0] / 255.0, lab_col[1] / 255.0, lab_col[2] / 255.0)
            cvs.setFont(_SCALE_BAR_PDF_FONT, fs_pt)
            cvs.drawString(x, y + panel_h / dpi * 72 + max(1.0, fs_pt * 0.28), lab)

    if pearson_data:
        r_val = pearson_data.get('pearson_r', 0)
        n_val = pearson_data.get('n_pixels', 0)
        cvs.setFillColorRGB(0, 0, 0)
        pearson_font = (font_size / dpi * 72 * 0.75) if font_size else panel_w / dpi * 72 * 0.06
        cvs.setFont('Helvetica', max(7, pearson_font))
        cvs.drawString(total_w_pt / 2 - 70, 8, f'Pearson r = {r_val:.4f}, N = {n_val}')

    cvs.save()
    buf.seek(0)
    return buf


def _build_pdf_structured_merge_left(panels, panel_w, panel_h, gap, margin, header_h,
                                     font_size, top_header_lines, scale_bar_config, inset_config,
                                     roi_config, pixel_size_um, dpi, pearson_data, header_align='center'):
    merge_data, right_panels = _split_merge_left(panels, panel_w, margin)
    if merge_data is None:
        return None
    merge_img, merge_w, merge_h, right_total_h, small_w, right_heights, gap_eff = merge_data
    total_w_pt = (2 * margin + merge_w + gap_eff + small_w) / dpi * 72
    total_h_pt = (2 * margin + right_total_h) / dpi * 72 + header_h

    buf = io.BytesIO()
    cvs = pdf_canvas.Canvas(buf, pagesize=(total_w_pt, total_h_pt))

    if top_header_lines:
        _draw_top_header_pdf(cvs, top_header_lines, total_w_pt, header_h, margin, font_size, panel_w, dpi,
                             total_h_pt, align=header_align, image_top=margin + header_h)

    # Merge 左大格
    cvs.drawImage(ImageReader(_encode_lossless(merge_img)), margin / dpi * 72,
                  total_h_pt - header_h - (margin + merge_h) / dpi * 72,
                  width=merge_w / dpi * 72, height=merge_h / dpi * 72, preserveAspectRatio=True)
    if _is_main_panel('merge'):
        y_m = total_h_pt - header_h - (margin + merge_h) / dpi * 72
        geom = _overlay_geometry(merge_img, roi_config, scale_bar_config, inset_config, pixel_size_um, True)
        _pdf_draw_overlays(cvs, geom, margin / dpi * 72, y_m, merge_w, merge_h, merge_img, dpi)

    # 右列
    cursor_px = margin
    for i, (k, img) in enumerate(right_panels):
        sh = right_heights[i]
        y_pt = total_h_pt - header_h - (cursor_px + sh) / dpi * 72
        x_pt = (margin + merge_w + gap_eff) / dpi * 72
        cvs.drawImage(ImageReader(_encode_lossless(img)), x_pt, y_pt,
                      width=small_w / dpi * 72, height=sh / dpi * 72, preserveAspectRatio=True)
        if _is_main_panel(k):
            geom = _overlay_geometry(img, roi_config, scale_bar_config, inset_config, pixel_size_um, True)
            _pdf_draw_overlays(cvs, geom, x_pt, y_pt, small_w, sh, img, dpi)
        cursor_px += sh + gap_eff

    cvs.save()
    buf.seek(0)
    return buf


def _build_layout(panels, panel_w, panel_h, label_h, gap, cols, draw_labels=True,
               font_label=None, font_small=None, panel_meta=None, scale_bar_config=None,
               pixel_size_um=None, panel_callbacks=None, margin=10,
               layout='grid', top_header_lines=None, header_align='center'):
    """
    通用布局构建器
    panels: list of (key, PIL.Image)
    panel_meta: {key: {'label': 'B', 'color': (0,0,255)}}
    layout: 'grid' (cols 网格) 或 'merge_left' (Merge 大图在左, 通道竖排右)
    top_header_lines: [(text, color)] 顶部一行, 各段按颜色, '/' 分隔
    header_align: 'center' 或 'left' (short 模式左对齐)
    """
    # 计算顶部 header 区域高度
    header_h = 0
    if top_header_lines:
        try:
            fs = font_label.size if font_label else 14
        except Exception:
            fs = 14
        header_h = max(int(fs * 1.7), int(panel_w * 0.04)) + 6

    # ===== merge_left 特殊布局 =====
    if layout == 'merge_left':
        return _build_layout_merge_left(
            panels, panel_w, panel_h, gap, margin, header_h,
            font_label, top_header_lines, scale_bar_config, pixel_size_um,
            header_align=header_align
        )

    n = len(panels)
    if n == 0:
        return None
    rows = math.ceil(n / cols)
    total_w = cols * panel_w + (cols - 1) * gap + 2 * margin
    total_h = rows * (panel_h + label_h) + (rows - 1) * gap + 2 * margin + header_h
    final = Image.new('RGB', (total_w, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(final)
    # 顶部 header
    if top_header_lines and font_label:
        _draw_top_header_pil(draw, top_header_lines, total_w, header_h, margin, font_label, align=header_align, image_top=margin + header_h + label_h)
    for i, (key, img) in enumerate(panels):
        r, c = i // cols, i % cols
        x = margin + c * (panel_w + gap)
        y = margin + header_h + r * (panel_h + label_h + gap)
        # 缩放到目标尺寸
        resized = img.resize((panel_w, panel_h), Image.LANCZOS)
        final.paste(resized, (x, y + label_h))
        if draw_labels and panel_meta and font_label:
            meta = panel_meta.get(key, {})
            label = meta.get('label', '')
            color = meta.get('color', (0, 0, 0))
            if label:
                _draw_label(draw, label, x, y, font_label, color)
    return final


def _draw_top_header_pil(draw, lines, total_w, header_h, margin, font, align='center', image_top=None):
    """PIL: 画顶部一行多色文本 (斜杠分隔); align: center/left;
    image_top: 首行图片顶部 y (px), 传入后标签底部与图片保持 1px"""
    if not lines:
        return
    sep_w = int(draw.textlength(' / ', font=font))
    seg_widths = []
    total_text_w = 0
    for text, _color in lines:
        w = int(draw.textlength(text, font=font))
        seg_widths.append(w)
        total_text_w += w + sep_w
    total_text_w -= sep_w  # 最后一个不要 sep
    if align == 'left':
        x = margin
    else:
        x = max(margin, (total_w - total_text_w) // 2)
    # 垂直位置: 底部与图片保持 1px (image_top 提供时); 否则居中
    try:
        bbox = draw.textbbox((0, 0), 'Ag', font=font)
        text_h = bbox[3] - bbox[1]
    except Exception:
        text_h = font.size if hasattr(font, 'size') else 14
    if image_top:
        y = image_top - 1 - text_h
    else:
        y = margin + max(2, (header_h - text_h) // 2)
    for i, (text, color) in enumerate(lines):
        draw.text((x, y), text, fill=_as_rgb(color), font=font)
        x += seg_widths[i]
        if i < len(lines) - 1:
            draw.text((x, y), ' / ', fill=(120, 120, 120), font=font)
            x += sep_w


def _build_layout_merge_left(panels, panel_w, panel_h, gap, margin, header_h,
                              font_label, top_header_lines, scale_bar_config, pixel_size_um,
                              header_align='center'):
    """Merge 大图在左 + 其余面板竖排右 (各按自身比例) (PIL 版)"""
    merge_data, right_panels = _split_merge_left(panels, panel_w, margin)
    if merge_data is None:
        return None
    merge_img, merge_w, merge_h, right_total_h, small_w, right_heights, gap_eff = merge_data

    total_w = 2 * margin + merge_w + gap_eff + small_w
    total_h = 2 * margin + right_total_h + header_h

    final = Image.new('RGB', (total_w, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(final)
    if top_header_lines and font_label:
        _draw_top_header_pil(draw, top_header_lines, total_w, header_h, margin, font_label, align=header_align, image_top=margin + header_h)

    # Merge 左大格
    merge_resized = merge_img.resize((merge_w, merge_h), Image.LANCZOS)
    final.paste(merge_resized, (margin, margin + header_h))
    # 右列
    cursor = margin + header_h
    for i, (_k, img) in enumerate(right_panels):
        sh = right_heights[i]
        resized = img.resize((small_w, sh), Image.LANCZOS)
        final.paste(resized, (margin + merge_w + gap_eff, cursor))
        cursor += sh + gap_eff
    return final


@app.route('/api/export', methods=['POST'])
def export_image():
    """导出排版图 - 支持 6 面板布局、Inset Overlay、SVG/PDF 矢量导出"""
    data = request.json or {}
    session_id = data.get('session_id')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在'}), 404

    sd = session_data[session_id]
    h, w = sd['metadata']['height'], sd['metadata']['width']
    dpi = int(data.get('dpi', 300))
    format_type = data.get('format', 'png').lower()
    scale_bar_config = data.get('scale_bar') or None
    inset_config = data.get('inset') or None
    roi_config = data.get('roi') or None
    label_style = data.get('labels', 'A')
    selected_panels = data.get('panels', ['ch0', 'ch1', 'ch2', 'merge'])

    # 优先采用前端当前值 (与预览一致), 回写后端避免两侧漂移
    pixel_size_um = data.get('pixel_size_um') or sd['metadata'].get('pixel_size_um')
    if pixel_size_um is not None:
        try:
            sd['metadata']['pixel_size_um'] = float(pixel_size_um)
            pixel_size_um = sd['metadata']['pixel_size_um']
        except (TypeError, ValueError):
            pass

    # 生成每个通道的彩色图 + 叠加 Scale Bar + Inset (仅活跃通道数)
    # channel_images = 烤入 overlay 的图 (供栅格导出 / 派生面板使用)
    # channel_base   = 未叠加底图 (供结构化 SVG/PDF 矢量导出, overlay 改为独立矢量层)
    ac = int(sd.get('active_count') or len(sd['channels']))
    channel_images = []
    channel_base = []
    for i, ch in enumerate(sd['channels'][:ac]):
        cfg = sd['configs'][i]
        colored = _make_color_channel_image(ch, cfg['rgb'], cfg.get('brightness', 1.0), cfg.get('contrast', 1.0),
                                            cfg.get('black', 0), cfg.get('white', 255), cfg.get('gamma', 1.0))
        channel_base.append(colored)
        channel_images.append(_apply_overlays(colored, scale_bar_config, inset_config, roi_config, pixel_size_um, 'channel'))

    # Merge + 叠加 (base = 未叠加; merged_img = 烤入 overlay 供栅格)
    chs_ac, cfgs_ac = _active_slice(sd)
    merged_base = create_merge_image(chs_ac, cfgs_ac, h, w)
    merged_img = _apply_overlays(merged_base, scale_bar_config, inset_config, roi_config, pixel_size_um, 'merge')

    # Profile plot (保持原逻辑)
    profile_img = None
    if 'profile' in selected_panels:
        line_data = data.get('line_data')
        if line_data and line_data.get('profiles'):
            if pixel_size_um and pixel_size_um > 0:
                for name, prof in line_data['profiles'].items():
                    prof['positions'] = [p * pixel_size_um for p in prof['positions']]
            profile_img = _make_profile_plot(
                line_data['profiles'], sd['configs'], [c['name'] for c in sd['configs']],
                pixel_size_um, target_w=400, target_h=300
            )

    # 构建面板
    panels = []
    panel_meta = {}
    for key in selected_panels:
        if key.startswith('ch') and key[2:].isdigit():
            idx = int(key[2:])
            if 0 <= idx < len(channel_images):
                panels.append((key, channel_images[idx]))
                cfg = sd['configs'][idx]
                panel_meta[key] = {'label': cfg.get('name', f'Ch{idx+1}'),
                                   'color': _as_rgb(cfg.get('rgb', (0, 0, 0)))}
        elif key == 'merge' or key == 'merged':
            panels.append(('merge', merged_img))
            panel_meta['merge'] = {'label': 'Merge', 'color': (0, 0, 0)}
        elif key == 'roi' and roi_config:
            x, y, rw, rh = int(roi_config['x']), int(roi_config['y']), int(roi_config['width']), int(roi_config['height'])
            x2, y2 = min(x + rw, merged_img.width), min(y + rh, merged_img.height)
            roi_img = merged_img.crop((x, y, x2, y2))
            panels.append(('roi', roi_img))
            panel_meta['roi'] = {'label': 'ROI', 'color': (243, 156, 18)}
        elif key == 'inset' and inset_config and roi_config:
            # 兼容旧版: 生成独立的 Inset 堆叠面板
            inset_stacked = _make_inset_panel(merged_img, channel_images, roi_config, inset_height_px=h,
                                              scale_bar_config=scale_bar_config, pixel_size_um=pixel_size_um)
            if inset_stacked:
                panels.append(('inset', inset_stacked))
                panel_meta['inset'] = {'label': 'Inset', 'color': (0, 0, 0)}
        elif key == 'profile' and profile_img is not None:
            panels.append(('profile', profile_img))
            panel_meta['profile'] = {'label': 'Profile plot', 'color': (0, 0, 0)}

    if not panels:
        panels = [('merge', merged_img)]
        panel_meta = {'merge': {'label': 'Merge', 'color': (0, 0, 0)}}

    # ===== 结构化矢量导出用的"未叠加"面板表 =====
    # 主图(ch/merge)用 base (未烤 overlay), overlay 由结构化 builder 画成独立矢量层;
    # 派生面板(roi/inset/profile)本就是独立合成图, 直接复用即可。
    _base_by_key = {}
    for i, ch in enumerate(sd['channels'][:ac]):
        _base_by_key[f'ch{i}'] = channel_base[i]
    _base_by_key['merge'] = merged_base
    _base_by_key['merged'] = merged_base
    for (k, img) in panels:
        if k not in _base_by_key:
            _base_by_key[k] = img
    panels_base = [(k, _base_by_key[k]) for (k, _) in panels]

    # ===== 标签: 一律画在各自面板上方 (v1.4.5) =====
    # letter: A/B/C… (黑) | name: 通道名 (随通道色) | short: 短码 (随通道色, 文献常用) | none: 不画
    # 字号由 builder 按「一张单图宽」基准算 (_label_font_units), 并保证不超出一张图宽。
    # 旧版把标签集中成整幅顶部的居中一行 (与面板对不齐, 且与面板之间留出空白带), 已改为每面板各一份。
    label_mode = (data.get('labels') or 'letter').lower()
    # 兼容旧值: 'A' -> letter, colored_slash -> name, 未知值视作 letter
    if label_mode == 'a':
        label_mode = 'letter'
    if label_mode == 'colored_slash':
        label_mode = 'name'
    if label_mode not in ('letter', 'name', 'short', 'none'):
        label_mode = 'letter'
    for idx, (key, _p) in enumerate(panels):
        meta = panel_meta.setdefault(key, {})
        if label_mode == 'none':
            meta['label'] = ''
        elif label_mode == 'letter':
            meta['label'] = chr(ord('A') + idx)
            meta['color'] = (0, 0, 0)
        else:
            lab = (meta.get('label', '') or key).strip()
            low = lab.lower()
            if label_mode == 'short':
                if low == 'merge':
                    lab = 'Merge'
                elif low in ('roi', 'inset', 'profile'):
                    lab = lab.capitalize() if low != 'profile' else 'Profile'
                else:
                    lab = _short_channel_code(lab, meta.get('color', (0, 0, 0)))
            meta['label'] = lab
    top_header_lines = []   # 不再使用整幅居中表头行 (标签已改为每面板各自一份)
    header_align = 'center'

    n = len(panels)
    # 布局: grid(原 cols 网格) / merge_left(Merge 大图在左, 通道竖排右)
    layout = (data.get('layout') or 'auto').lower()
    if layout == 'merge_left':
        # 特殊几何, 跳过 cols 计算
        cols = 1  # 占位, merge_left 分支不使用
    elif layout == '1xn':
        cols = n
    elif layout == '2col':
        cols = 2
    elif layout == '3col':
        cols = 3
    elif layout == '4col':
        cols = 4
    else:  # auto: 保持原有单行优先逻辑
        if n <= 2:
            cols = n
        elif n <= 4:
            cols = min(n, 4)
        elif n <= 6:
            cols = min(n, 6)
        else:
            cols = min(n, 4)
    cols = max(1, cols)
    rows = math.ceil(n / cols)

    # 用户可覆盖的排版参数 (0 / 缺省 = 自动)
    margin = max(4, int(data.get('margin', 10) or 10))
    single_col_px = int(8.5 * dpi / 2.54)
    ref_w = single_col_px // cols   # 排版参考宽度: 字号/间距按它算, 与面板实际像素尺寸解耦
    img_ratio = h / w if w > 0 else 1
    # native_size: 面板直接用源图像素尺寸 (1:1, 不重采样), DPI 只决定物理打印尺寸
    native_size = bool(data.get('native_size'))
    if native_size:
        panel_w, panel_h = int(w), int(h)
    else:
        panel_w = ref_w
        panel_h = int(ref_w * img_ratio)
    gap_req = data.get('gap')
    gap = int(gap_req) if (gap_req and int(gap_req) > 0) else max(6, int(ref_w * 0.012))
    font_req = data.get('font_size')
    font_size = int(font_req) if (font_req and int(font_req) > 0) else max(12, int(ref_w * 0.035))
    label_h = max(int(font_size * 1.35), int(ref_w * 0.035))

    pearson_data = data.get('pearson')

    # ===== 矢量输出 (PDF/SVG) — 结构化: 底图 + ROI/标尺/Inset 各自独立可编辑图层 =====
    if format_type == 'pdf':
        buf = _build_pdf_structured(panels_base, panel_w, panel_h, label_h, gap, cols, panel_meta,
                                    scale_bar_config, inset_config, roi_config, pixel_size_um, dpi,
                                    pearson_data=pearson_data, margin=margin, font_size=font_size,
                                    layout=layout, top_header_lines=top_header_lines,
                                    header_align=header_align, native_size=native_size)
        return send_file(buf, as_attachment=True,
                         download_name='microscope_layout.pdf',
                         mimetype='application/pdf')

    if format_type == 'svg':
        _raw = data.get('svg_display_pt', 100)
        svg_display_pt = 100 if _raw is None else int(_raw)
        buf = _build_svg_structured(panels_base, panel_w, panel_h, label_h, gap, cols, panel_meta,
                                    scale_bar_config, inset_config, roi_config, pixel_size_um, dpi,
                                    pearson_data=pearson_data, margin=margin, font_size=font_size,
                                    layout=layout, top_header_lines=top_header_lines,
                                    header_align=header_align, native_size=native_size,
                                    svg_display_pt=svg_display_pt)
        return send_file(buf, as_attachment=True,
                         download_name='microscope_layout.svg',
                         mimetype='image/svg+xml')

    # ===== 栅格输出 (PNG/TIFF/JPG) =====
    font_label = _load_arial_font(font_size)

    final = _build_layout(
        panels=panels, panel_w=panel_w, panel_h=panel_h,
        label_h=label_h, gap=gap, cols=cols,
        draw_labels=True, font_label=font_label,
        panel_meta=panel_meta, margin=margin,
        layout=layout, top_header_lines=top_header_lines,
        header_align=header_align
    )

    if pearson_data and isinstance(pearson_data, dict):
        r_val = pearson_data.get('pearson_r', 0)
        n_val = pearson_data.get('n_pixels', 0)
        font_pearson = _load_arial_font(max(11, font_size - 4))
        new_h = final.height + label_h + 10
        new_img = Image.new('RGB', (final.width, new_h), (255, 255, 255))
        new_img.paste(final, (0, 0))
        d = ImageDraw.Draw(new_img)
        d.text((final.width // 2 - 100, final.height + 5),
               f'Pearson r = {r_val:.4f}, N = {n_val}',
               fill=(0, 0, 0), font=font_pearson)
        final = new_img

    buf = io.BytesIO()
    fmt_map = {'png': 'PNG', 'tiff': 'TIFF', 'jpg': 'JPEG', 'jpeg': 'JPEG'}
    fmt = fmt_map.get(format_type, 'PNG')
    save_kwargs = {'dpi': (dpi, dpi)}
    if fmt == 'JPEG':
        save_kwargs['quality'] = 95
    final.save(buf, format=fmt, **save_kwargs)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True,
        download_name=f'microscope_layout.{format_type}',
        mimetype=f'image/{format_type}'
    )


@app.route('/api/export_single', methods=['POST'])
def export_single_channel():
    data = request.json or {}
    session_id = data.get('session_id')
    if session_id not in session_data:
        return jsonify({'error': '会话不存在'}), 404

    sd = session_data[session_id]
    h, w = sd['metadata']['height'], sd['metadata']['width']
    panel_type = data.get('panel_type', 'merge')
    dpi = int(data.get('dpi', 300))
    format_type = data.get('format', 'png').lower()
    scale_bar_config = data.get('scale_bar') or None
    inset_config = data.get('inset') or None
    roi_config = data.get('roi') or None
    # 优先采用前端当前值 (与预览一致), 回写后端避免两侧漂移
    pixel_size_um = data.get('pixel_size_um') or sd['metadata'].get('pixel_size_um')
    if pixel_size_um is not None:
        try:
            sd['metadata']['pixel_size_um'] = float(pixel_size_um)
            pixel_size_um = sd['metadata']['pixel_size_um']
        except (TypeError, ValueError):
            pass

    if panel_type == 'merge':
        chs_ac, cfgs_ac = _active_slice(sd)
        img = create_merge_image(chs_ac, cfgs_ac, h, w)
    elif panel_type.startswith('ch'):
        try:
            idx = int(panel_type[2:])
            cfg = sd['configs'][idx]
            img = _make_color_channel_image(sd['channels'][idx], cfg['rgb'],
                                            cfg.get('brightness', 1.0), cfg.get('contrast', 1.0),
                                            cfg.get('black', 0), cfg.get('white', 255), cfg.get('gamma', 1.0))
        except (ValueError, IndexError):
            chs_ac, cfgs_ac = _active_slice(sd)
            img = create_merge_image(chs_ac, cfgs_ac, h, w)
    else:
        chs_ac, cfgs_ac = _active_slice(sd)
        img = create_merge_image(chs_ac, cfgs_ac, h, w)

    # 保留未叠加底图供结构化 SVG; 栅格/PDF 仍需叠加后的图
    base_img = img.copy()
    img = _apply_overlays(img, scale_bar_config, inset_config, roi_config, pixel_size_um, panel_type)

    # PDF/SVG 单图导出 (共用 svg_display_pt: 100pt 画布 + 原生分辨率无损嵌入, 两格式一致)
    _raw = data.get('svg_display_pt', 100)
    svg_display_pt = 100 if _raw is None else int(_raw)
    if format_type == 'pdf':
        # 结构化 PDF: 底图无损嵌入 + ROI/标尺/Inset 独立矢量对象 (可在 Illustrator 编辑字体/标尺)
        buf = _build_pdf_single_structured(base_img, roi_config, scale_bar_config, inset_config,
                                            pixel_size_um, panel_type, dpi, svg_display_pt=svg_display_pt)
        return send_file(buf, as_attachment=True, download_name=f'{panel_type}.pdf', mimetype='application/pdf')
    if format_type == 'svg':
        # 结构化矢量 SVG: 底图 + ROI/标尺/Inset 各自独立 <g>, 可在 Illustrator 中自由移动
        # 用 base_img (未叠加) 而非 img (已叠加), 避免 ROI/标尺/Inset 被烤入栅格后又叠加矢量层 (双重)
        buf = _build_svg_single_structured(base_img, roi_config, scale_bar_config, inset_config,
                                           pixel_size_um, panel_type, dpi, svg_display_pt=svg_display_pt)
        return send_file(buf, as_attachment=True, download_name=f'{panel_type}.svg', mimetype='image/svg+xml')

    buf = io.BytesIO()
    fmt_map = {'png': 'PNG', 'tiff': 'TIFF', 'jpg': 'JPEG', 'jpeg': 'JPEG'}
    fmt = fmt_map.get(format_type, 'PNG')
    save_kwargs = {'dpi': (dpi, dpi)}
    if fmt == 'JPEG':
        save_kwargs['quality'] = 95
    img.save(buf, format=fmt, **save_kwargs)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True,
        download_name=f'{panel_type}.{format_type}',
        mimetype=f'image/{format_type}'
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # 端口冲突时由 OS 临时分配一个空闲端口
    use_random_port = getattr(sys, 'frozen', False)
    host = '127.0.0.1' if use_random_port else '0.0.0.0'

    log.info(f'FluoFigFactory starting on http://{host}:{port} '
             f'({"frozen" if getattr(sys, "frozen", False) else "source"} mode)')

    # frozen 模式: 后台延迟打开默认浏览器
    if getattr(sys, 'frozen', False):
        def _open_browser():
            import time
            time.sleep(0.8)
            try:
                webbrowser.open(f'http://127.0.0.1:{port}')
            except Exception as e:
                log.warning(f'自动打开浏览器失败: {e}')
        threading.Thread(target=_open_browser, daemon=True).start()

    app.run(debug=False, host=host, port=port, use_reloader=False)
