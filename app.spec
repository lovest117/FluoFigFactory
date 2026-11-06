# -*- mode: python ; coding: utf-8 -*-
"""
FluoFigFactory - PyInstaller spec
目标: onedir 模式 + 自启浏览器
产物: dist/FluoFigFactory/FluoFigFactory.exe
"""
import sys
import os
import glob
from PyInstaller.utils.hooks import collect_dynamic_libs


block_cipher = None

# imagecodecs 的各编解码器 (_deflate/_zstd/_jpeg8/_tiff/... 共 40+ 个 .pyd)
# 是运行期按需动态 import 的，PyInstaller 静态分析抓不到。
# 只在 hiddenimports 里写 'imagecodecs' 只会带进 _shared.pyd，
# 导致 FV3000 默认 LZW 压缩的 TIFF 在打包版里全部读取失败(报"无有效页面")。
# 必须同时: ①收集全部动态库 ②显式列出全部子模块。
_ic_dir = os.path.dirname(__import__('imagecodecs').__file__)
_ic_submodules = [
    'imagecodecs.' + os.path.splitext(os.path.basename(_p))[0]
    for _p in glob.glob(os.path.join(_ic_dir, '*.pyd'))
]

# 关键: hiddenimports 必须包含所有动态 import 的依赖
# (PyInstaller 默认会静态分析, 但 Flask/scipy 有动态 hook)
a = Analysis(
    ['app.py'],
    pathex=[],
    # 把 imagecodecs 的全部 .pyd 编解码器收进去
    binaries=collect_dynamic_libs('imagecodecs'),
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=[
        'flask', 'flask.templating', 'flask.helpers', 'flask.json',
        'numpy', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont',
        'tifffile',
        # 必须！否则 tifffile 无法解码 LZW/deflate/JPEG/zstd，
        # FV3000 默认 LZW 导出的 TIFF 会全部读取失败（报"无有效页面"）
        'imagecodecs',
        'reportlab', 'reportlab.pdfgen', 'reportlab.lib.pagesizes',
        'reportlab.lib.utils', 'xml.etree.ElementTree', 'xml.dom.minidom',
        'engineio', 'socketio',
    ] + _ic_submodules,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'scipy', 'scipy.stats', 'scipy.ndimage', 'scipy.sparse',
        'matplotlib', 'sympy', 'pandas', 'torch', 'tensorflow',
        'IPython', 'jedi', 'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FluoFigFactory',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # 关掉 UPX 压缩 (解压巨慢且常被 360 误报)
    console=False,              # 不弹控制台窗口
    disable_windowed_tracebacks=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                  # 如有 assets/icon.ico 可写死: 'assets/icon.ico'
    version=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='FluoFigFactory',
)
