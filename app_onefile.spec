# -*- mode: python ; coding: utf-8 -*-
"""
FluoFigFactory - PyInstaller ONE-FILE spec (单文件 exe, 用于分发测试)
目标: 单个 dist/FluoFigFactory.exe (双击即启动 + 自开浏览器)
与 app.spec (onedir) 的区别: 不 COLLECT, 所有二进制/数据打进同一个 EXE。
"""
import sys
import os
import glob
from PyInstaller.utils.hooks import collect_dynamic_libs


block_cipher = None

# imagecodecs 的编解码器 (_deflate/_zstd/_jpeg8/_tiff/... 40+ 个 .pyd) 是运行期按需动态 import,
# 静态分析抓不到。只在 hiddenimports 写 'imagecodecs' 只会带进 _shared.pyd,
# 导致 FV3000 默认 LZW 压缩的 TIFF 在打包版里全部读取失败(报"无有效页面")。
# 必须: ① collect_dynamic_libs 收集全部 .pyd ② 显式列出全部子模块。
_ic_dir = os.path.dirname(__import__('imagecodecs').__file__)
_ic_submodules = [
    'imagecodecs.' + os.path.splitext(os.path.basename(_p))[0]
    for _p in glob.glob(os.path.join(_ic_dir, '*.pyd'))
]

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=collect_dynamic_libs('imagecodecs'),
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=[
        'flask', 'flask.templating', 'flask.helpers', 'flask.json',
        'numpy', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont',
        'tifffile',
        # 必须！否则 tifffile 无法解码 LZW/deflate/JPEG/zstd
        'imagecodecs',
        'reportlab', 'reportlab.pdfgen', 'reportlab.lib.pagesizes',
        'reportlab.lib.utils', 'reportlab.pdfbase', 'reportlab.pdfbase.ttfonts',
        'xml.etree.ElementTree', 'xml.dom.minidom',
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

# ONE-FILE 模式: 把 binaries / zipfiles / datas 全部并入 EXE (不再 COLLECT)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='FluoFigFactory',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # 关 UPX: 解压慢且常被 360/Windows Defender 误报
    upx_exclude=[],
    runtime_tmpdir=None,        # 解压到系统临时目录 (每次启动自解压)
    console=False,              # 不弹控制台 (浏览器自动打开)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    version=None,
)
