# -*- coding: utf-8 -*-
"""v1.4.4 导出 overlay 样式验证 (ROI 线宽沿用界面「线宽」 / 真虚线 / Inset 单白 1pt)
以及默认色与画板规则:
- ROI: stroke-width=设定 pt × sc, stroke-dasharray="dash,gap" (dash=2.5×线宽, gap=1.5×线宽,
  对应界面 canvas setLineDash([5,3])@2px 比例), stroke-linecap="butt"; 禁止再写 "3,0"(间隔0=实线)
- ROI 默认色: 白色 rgb(255,255,255) (旧为黄色 255,230,0)
- Inset: 单层白色描边, 无外白内黑双层框
- 单图画板: 非正方形也强制 width=height=100pt, viewBox 保留原生坐标系, 底图无损
- 100pt 模式: 根 <svg> width="100pt" height="100pt", viewBox 仍保留原生分辨率坐标系 (0 0 W W),
  底图 <image> width/height 仍为原生像素 (无损), 像素零丢失
- 排版(多图)画板: 「100pt」= **每张单图**的宽度, 不是整幅画布 (v1.4.4 起, 见 `_svg_layout_display_dims`)。
  旧行为把整幅画布压到 100pt → 一行 4 张 2048px 面板时每张只剩 24.8846pt (用户 2026-09-17 实测)。
- 不再输出「等画布的白底」rect: 单图与排版 SVG 均不得出现 fill="white" 的背景矩形 (v1.4.4 起)。
- PDF: ROI 与 SVG 一致为真虚线 setDash([2.5×线宽, 1.5×线宽]) (v1.4.3 起; 旧值 [3, 0] 间隔0=实线)
"""
import io
import os
import sys
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as app_module


def _build_sample_svg(svg_display_pt=0):
    """构造一个最小可用的 merge + ROI + Scale Bar + Inset 场景, 跑出 SVG 字符串"""
    base = Image.new('RGB', (256, 256), (40, 40, 40))
    roi = {'x': 50, 'y': 50, 'width': 60, 'height': 60}  # 不指定 color -> 走默认 (白色)
    inset = {'enabled': True, 'zoom': 2.0, 'position': 'top-right'}
    scale_bar = {'length_um': 50, 'position': 'bottom-right', 'color': 'white',
                 'thickness_pt': 1.0, 'font_size_auto': True}
    buf = app_module._build_svg_single_structured(
        base, roi, scale_bar, inset, pixel_size_um=0.3, panel_type='merge',
        svg_display_pt=svg_display_pt
    )
    return buf.read().decode('utf-8')


def _build_svg_custom(size=(256, 256), thickness_pt=1.0, svg_display_pt=100):
    """可指定尺寸/线宽的 SVG 构造 (用于非正方形画板与 ROI 线宽用例)"""
    base = Image.new('RGB', size, (40, 40, 40))
    roi = {'x': 50, 'y': 50, 'width': 60, 'height': 60, 'color': '#ffe600'}
    inset = {'enabled': True, 'zoom': 2.0, 'position': 'top-right'}
    scale_bar = {'length_um': 50, 'position': 'bottom-right', 'color': 'white',
                 'thickness_pt': thickness_pt, 'font_size_auto': True}
    buf = app_module._build_svg_single_structured(
        base, roi, scale_bar, inset, pixel_size_um=0.3, panel_type='merge',
        svg_display_pt=svg_display_pt
    )
    return buf.read().decode('utf-8')


def test_svg_single_nonsquare_canvas_100pt():
    """非正方形图: 画板宽高都必须是 100pt (旧行为会出 100x50, 多图排版尺寸不齐),
    且内容等比缩放居中, 绝不拉伸变形"""
    svg = _build_svg_custom(size=(512, 256), svg_display_pt=100)
    assert 'width="100pt"' in svg, '非正方形图画板宽应为 100pt'
    assert 'height="100pt"' in svg, '非正方形图画板高必须同为 100pt (旧行为 50pt)'
    assert 'viewBox="0 0 512 256"' in svg, 'viewBox 须保留原生坐标系 (像素零丢失)'
    assert 'preserveAspectRatio="xMidYMid meet"' in svg, '须等比缩放居中, 禁止拉伸变形'


def test_svg_roi_stroke_follows_ui_thickness():
    """ROI 线宽沿用界面「线宽」(thickness_pt): 字面值 = pt × sc, 呈现值恰为设定 pt"""
    # 256px -> 100pt 画布: sc = 2.56; 线宽设 2pt -> 字面值 5.12, 缩放后呈现 2pt
    svg = _build_svg_custom(size=(256, 256), thickness_pt=2.0, svg_display_pt=100)
    roi_idx = svg.find('id="roi"')
    assert roi_idx > 0, '应存在 ROI 图层'
    roi_block = svg[roi_idx:roi_idx + 400]
    assert 'stroke-width="5.12"' in roi_block, '100pt 下 ROI 线宽应为 thickness_pt(2) × sc(2.56) = 5.12'

    # 原生尺寸模式 (sc=1): 字面值即设定 pt 值, 不被缩放改变
    svg0 = _build_svg_custom(size=(256, 256), thickness_pt=2.0, svg_display_pt=0)
    roi_idx0 = svg0.find('id="roi"')
    roi_block0 = svg0[roi_idx0:roi_idx0 + 400]
    assert 'stroke-width="2"' in roi_block0, '原生模式下 ROI 线宽字面值应等于设定 pt (2)'


def _roi_rect_attrs(svg, anchor='id="roi"'):
    """从 SVG 里抓出 ROI <rect> 的 stroke-width / dasharray (呈现值 = 字面值 / sc)"""
    import re
    i = svg.find(anchor)
    assert i > 0, '应存在 ROI 图层'
    blk = svg[i:i + 400]
    sw = float(re.search(r'stroke-width="([\d.]+)"', blk).group(1))
    dash, gap = re.search(r'stroke-dasharray="([\d.]+),([\d.]+)"', blk).groups()
    return sw, float(dash), float(gap)


def test_svg_roi_dash_ratio_scales_with_linewidth():
    """虚线段:间隔恒为 5:3 (界面 setLineDash([5,3])@2px), 且与线宽同比, 缩放/归一化后不丢失"""
    cases = [(1.0, 0, 1.0), (2.0, 0, 1.0), (1.0, 100, 2.56), (3.0, 100, 2.56)]
    for thickness, display, sc in cases:
        svg = _build_svg_custom(size=(256, 256), thickness_pt=thickness, svg_display_pt=display)
        sw, dash, gap = _roi_rect_attrs(svg)
        # 线宽呈现值 = 设定 pt
        assert abs(sw / sc - thickness) < 0.01, f'线宽呈现值应={thickness}pt, 实为 {sw/sc}'
        # dash/gap 呈现值 = 2.5× / 1.5× 线宽
        assert abs(dash / sc - thickness * 2.5) < 0.01, f'dash 应为 {thickness*2.5}pt, 实为 {dash/sc}'
        assert abs(gap / sc - thickness * 1.5) < 0.01, f'gap 应为 {thickness*1.5}pt, 实为 {gap/sc}'
        # 间隔必须 > 0 (旧写法 3,0 即实线, 是 AI 中虚线丢失的根因)
        assert gap > 0, 'dasharray 间隔必须 > 0, 否则 AI 中渲染为实线'


def test_svg_layout_roi_dash_and_white_default():
    """排版导出 (_svg_add_overlay_groups) 同样写虚线 + 默认白色"""
    base = Image.new('RGB', (256, 256), (40, 40, 40))
    roi = {'x': 50, 'y': 50, 'width': 60, 'height': 60}  # 不指定 color
    geom = app_module._overlay_geometry(base, roi, None, None, 0.3, True, disp_scale=1.0)
    assert geom['roi']['color'] == (255, 255, 255), 'ROI 默认色应为白色'
    root = app_module.ET.Element('svg')
    app_module._svg_add_overlay_groups(root, geom, 0, 0, 1.0, 'p0', disp_scale=1.0, roi_stroke_pt=1.0)
    s = app_module.ET.tostring(root, encoding='unicode')
    assert 'stroke-dasharray="2.5,1.5"' in s, f'排版 ROI 虚线应为 2.5,1.5, 实际: {s}'
    assert 'stroke-dasharray="3,0"' not in s, '排版导出同样禁止 "3,0" 实线写法'
    assert 'stroke="rgb(255,255,255)"' in s, '排版 ROI 默认色应为白色'


def test_rgb_blue_plane_default_name_is_dapi():
    """蓝色通道默认名应为 DAPI (界面通道名与导出面板标签同源)"""
    assert app_module.PLANE_NAMES[2] == 'DAPI', \
        f'RGB 蓝平面默认名应为 DAPI, 实为 {app_module.PLANE_NAMES[2]}'


def test_svg_single_structured_overlay_styles():
    # 原生尺寸模式 (svg_display_pt=0): 线宽字面值保持 1, 与改动前行为一致
    svg = _build_sample_svg(svg_display_pt=0)

    # ROI: 真虚线 (dash,gap 均 > 0, 与线宽同比) + 默认白色
    assert 'stroke-dasharray="2.5,1.5"' in svg, '原生模式 dasharray 应为 2.5,1.5 (线宽1pt × 2.5 / × 1.5)'
    assert 'stroke-dasharray="3,0"' not in svg, '禁止再写 dasharray "3,0" (间隔0=实线, AI 中丢虚线)'
    assert 'stroke-linecap="butt"' in svg, '应显式写出 stroke-linecap="butt" 防止端点被拉长'
    # 默认色改为白色 (旧: 黄色 255,230,0)
    assert 'stroke="rgb(255,255,255)"' in svg, 'ROI 默认颜色应为白色 rgb(255,255,255)'
    assert 'stroke="rgb(255,230,0)"' not in svg, '不应再出现旧的默认黄色'

    # 无黑色底衬 + 无黑色内框
    assert 'stroke="black"' not in svg, '不应再有 stroke="black" (已去掉 ROI 黑色底衬和 inset 内黑框)'

    # 恰好一个白色 stroke (单层 inset 框)
    assert svg.count('stroke="white"') == 1, f'应恰好 1 个白色 stroke (inset 单层), 实得 {svg.count(chr(34))}'

    # Inset 白色描边应为 1pt
    white_rect_idx = svg.find('stroke="white"')
    assert white_rect_idx > 0
    nearby = svg[white_rect_idx:white_rect_idx + 200]
    assert 'stroke-width="1"' in nearby, 'Inset 白色描边应为 1pt'


def test_svg_single_100pt_display_default():
    """v1.4.0-sub: SVG 在 Illustrator 打开默认 100pt 画布, 但原生像素零丢失"""
    svg = _build_sample_svg(svg_display_pt=100)

    # 根 <svg> 默认显示尺寸 100pt×100pt (256×256 正方形)
    assert 'width="100pt"' in svg, '100pt 模式下根 <svg> width 应为 100pt'
    assert 'height="100pt"' in svg, '100pt 模式下根 <svg> height 应为 100pt'

    # viewBox 仍保留原生分辨率坐标系 (0 0 256 256)
    assert 'viewBox="0 0 256 256"' in svg, 'viewBox 必须保留原生分辨率坐标系, 像素零丢失'

    # 底图 <image> 仍为原生 256×256 像素 (无损嵌入, 仅显示缩放)
    assert 'width="256" height="256"' in svg, '底图 image 必须为原生分辨率 256×256, 不能降采样'

    # 虚线 + 默认白: sc=2.56 -> dasharray 6.4,3.84, 缩放后恰为 2.5pt/1.5pt
    assert 'stroke-dasharray="6.4,3.84"' in svg, '100pt 模式 dasharray 应按 sc=2.56 缩放 (6.4,3.84)'
    assert 'stroke-dasharray="3,0"' not in svg, '100pt 模式下同样禁止 "3,0" 实线写法'
    assert 'stroke="rgb(255,255,255)"' in svg, 'ROI 颜色在 100pt 模式下仍为白色'
    roi_idx = svg.find('id="roi"')
    assert roi_idx > 0
    roi_block = svg[roi_idx:roi_idx + 400]
    # sc = 256/100 = 2.56 -> 线宽字面值 2.56, 在 100pt 画布下即 1pt 视觉
    assert 'stroke-width="2.56"' in roi_block, '100pt 模式 ROI 线宽应按 sc=2.56 缩放以呈 1pt 视觉'

    # 无黑色底衬
    assert 'stroke="black"' not in svg

    # 恰好一个白色 stroke (inset 单层), 线宽同样按 sc 缩放
    assert svg.count('stroke="white"') == 1
    white_rect_idx = svg.find('stroke="white"')
    nearby = svg[white_rect_idx:white_rect_idx + 200]
    assert 'stroke-width="2.56"' in nearby, '100pt 模式 Inset 白色描边应按 sc=2.56 缩放'


def test_pdf_single_100pt_pagesize():
    """PDF 单图导出与 SVG 一致: 画布固定 100pt×100pt, 底图无损嵌入"""
    base = Image.new('RGB', (256, 256), (40, 40, 40))
    roi = {'x': 50, 'y': 50, 'width': 60, 'height': 60, 'color': '#ffe600'}
    sb = {'length_um': 50, 'position': 'bottom-right', 'color': 'white',
          'thickness_pt': 1.0, 'font_size_auto': True}
    ins = {'enabled': True, 'zoom': 2.0, 'position': 'top-right'}
    buf = app_module._build_pdf_single_structured(
        base, roi, sb, ins, pixel_size_um=0.3, panel_type='merge', svg_display_pt=100
    )
    data = buf.read()
    assert data.startswith(b'%PDF'), '应输出合法 PDF'
    assert b'0 0 100 100' in data, f'PDF MediaBox 应为 100pt x 100pt'


def test_svg_single_native_roundtrip():
    """选「原始尺寸」(svg_display_pt=0) 时, 根 <svg> 宽度=原生像素, 线宽=1"""
    svg = _build_sample_svg(svg_display_pt=0)
    assert 'width="256"' in svg and 'height="256"' in svg, '原生模式根 <svg> 尺寸应为原生像素 256'
    assert 'viewBox="0 0 256 256"' in svg


class _RecordingCanvas:
    """极简 reportlab Canvas 替身: 只记录 ROI 描边相关调用 (无需真正生成 PDF)"""

    def __init__(self):
        self.line_widths = []
        self.dashes = []

    def setStrokeColorRGB(self, *a):
        pass

    def setLineWidth(self, w):
        self.line_widths.append(w)

    def setDash(self, arr=None, phase=0):
        self.dashes.append(None if arr is None else list(arr))

    def rect(self, *a, **k):
        pass

    def setFillColorRGB(self, *a):
        pass

    def setFont(self, *a):
        pass

    def drawString(self, *a):
        pass

    def drawImage(self, *a, **k):
        pass


def test_pdf_roi_is_dashed():
    """PDF ROI 必须是真虚线 (v1.4.3 起): dash=2.5×线宽, gap=1.5×线宽, 与 SVG 一致。
    旧值 [3, 0] 间隔 0 = 实线, 用户实测 PDF 中看不到虚线。"""
    base = Image.new('RGB', (256, 256), (40, 40, 40))
    geom = {
        'roi': {'x': 50, 'y': 50, 'w': 60, 'h': 60, 'color': (255, 255, 255)},
        'scale_bar': None,
        'inset': None,
    }
    cvs = _RecordingCanvas()
    app_module._pdf_draw_overlays(cvs, geom, 0, 0, 100.0, 100.0, base, 72)

    assert cvs.line_widths and cvs.line_widths[0] == 1.0, 'PDF ROI 线宽固定 1pt'
    assert cvs.dashes[0] == [2.5, 1.5], \
        f'PDF ROI 虚线应为 [2.5, 1.5] (1pt×2.5 / 1pt×1.5), 实为 {cvs.dashes}'
    assert cvs.dashes[0][1] > 0, 'dasharray 间隔必须 > 0, 否则 PDF 中渲染为实线'
    assert cvs.dashes[-1] is None, '画完 ROI 必须复位虚线, 避免影响后续标尺/文字'


def _build_layout_svg(panel_w=2048, panel_h=2048, cols=4, gap=6, margin=10, svg_display_pt=100):
    """复刻线上排版场景: cols 张 panel_w×panel_h 面板并排 (native_size=True)"""
    base = Image.new('RGB', (panel_w, panel_h), (30, 30, 60))
    keys = ['ch%d' % i for i in range(cols - 1)] + ['merge']
    panels = [(k, base) for k in keys]
    roi = {'x': 20, 'y': 20, 'width': 200, 'height': 200, 'color': '#ffffff'}
    buf = app_module._build_svg_structured(
        panels, panel_w, panel_h, 47, gap, cols, {}, None, None, roi, 0.3, 300,
        margin=margin, font_size=12, svg_display_pt=svg_display_pt)
    return buf.read().decode('utf-8')


def _root_size(svg):
    import re
    a = ' '.join(re.search(r'<svg[^>]*>', svg, re.S).group(0).split())
    w = float(re.search(r'width="([\d.]+)pt', a).group(1))
    h = float(re.search(r'height="([\d.]+)pt', a).group(1))
    vb = re.search(r'viewBox="([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)"', a)
    return w, h, float(vb.group(3)), float(vb.group(4))


def test_svg_layout_scales_per_single_image():
    """排版 SVG 里的「100pt」= 每张单图的宽度, 不是整幅画布。
    旧行为把整幅画布压到 100pt → 一行 4 张 2048px 面板时每张只剩 24.8846pt (用户 2026-09-17 实测)。"""
    dw, dh, vb_w, vb_h = _root_size(_build_layout_svg(panel_w=2048, panel_h=2048, cols=4))
    assert vb_w == 8230.0, f'4×2048 + gap6 + margin10 的 viewBox 宽应为 8230, 实为 {vb_w}'
    per_panel = 2048.0 * dw / vb_w
    assert abs(per_panel - 100.0) < 0.01, f'每张单图应为 100pt, 实为 {per_panel:.4f}'
    assert abs(per_panel - 24.8846) > 1.0, '每张单图仍是旧行为的 24.8846pt, 修复未生效'
    assert dw > 100.0, '画布应随面板数变宽, 不再把整幅压进 100pt'
    # 容差 1e-4: 根 width/height 以 %g (6 位有效数字) 写出, 属格式化精度, 非真实失真
    assert abs((dw / dh) - (vb_w / vb_h)) < 1e-4, '画布长宽比必须与 viewBox 一致 (面板不失真)'


def test_svg_layout_native_mode_unchanged():
    """选「原始尺寸」(svg_display_pt=0) 时排版画布 = 原生像素, 不受新规则影响"""
    dw, dh, vb_w, vb_h = _root_size(_build_layout_svg(svg_display_pt=0))
    assert dw == vb_w and dh == vb_h, f'原生模式画布应等于 viewBox, 实为 {dw}x{dh}'


def test_svg_no_canvas_white_background():
    """单图与排版 SVG 都不应再有「等画布的白底」矩形 (用户 2026-09-17 要求去掉)"""
    import re
    single = _build_sample_svg(svg_display_pt=100)      # 256×256, 含白色标尺 (fill="white" 属标尺)
    assert not re.search(r'<rect[^>]*width="256"[^>]*height="256"[^>]*fill="white"', single), \
        '单图 SVG 不应再有等画布白底 rect'
    layout = _build_layout_svg(svg_display_pt=100)
    assert 'fill="white"' not in layout, '排版 SVG 不应再有白底 rect'
    # 去掉白底后底图仍须覆盖整个 viewBox (不能露空)
    assert '<image' in layout and 'width="2048" height="2048"' in layout, '面板底图仍须原生分辨率'


def test_svg_layout_draws_panel_labels_within_one_image_width():
    """每个面板要有通道名标签 (独立 <g id="panel-i-label">), 字号以「一张单图宽」为基准 = 7.5%,
    且文字宽度不得超过一张单图宽 (用户 2026-09-18 明确要求)。"""
    panel_w = 2048
    base = Image.new('RGB', (panel_w, panel_w), (30, 30, 60))
    panels = [('ch0', base), ('ch1', base), ('merge', base)]
    meta = {'ch0': {'label': 'ayn', 'color': (0, 255, 0)},
            'ch1': {'label': 'hucd', 'color': (255, 0, 0)},
            'merge': {'label': 'Merge', 'color': (0, 0, 0)}}
    roi = {'x': 20, 'y': 20, 'width': 200, 'height': 200, 'color': '#ffffff'}
    buf = app_module._build_svg_structured(panels, panel_w, panel_w, 16, 6, 3, meta,
                                           None, None, roi, 0.3, 300,
                                           margin=10, font_size=12, svg_display_pt=100)
    svg = buf.read().decode('utf-8')
    for i, lab in enumerate(['ayn', 'hucd', 'Merge']):
        assert f'id="panel-{i}-label"' in svg, f'面板 {i} 应有独立标签图层'
        assert f'>{lab}<' in svg, f'面板 {i} 标签文字应为 {lab}'
    # 基准字号 = 单图宽 2048 × 7.5% = 153.6 (user units) → 单图显示 100pt 时即 7.5pt
    assert 'font-size="153.6"' in svg, '标签字号应为 一张单图宽 × 7.5%'
    for lab in ['ayn', 'hucd', 'Merge']:
        fs = app_module._label_font_units(lab, panel_w)
        assert app_module._measure_text_units(lab, fs) <= panel_w + 0.5, f'{lab} 文字宽超出一张图宽'


def test_long_label_shrinks_to_fit_one_image_width():
    """超长标签自动缩小, 保证文字长度不超过一张图宽; 短标签用基准字号。"""
    panel_w = 2048
    long_lab = 'asyn488hucd594DAPI405mergecomposite'
    fs = app_module._label_font_units(long_lab, panel_w)
    assert fs < app_module.LABEL_FONT_RATIO * panel_w, '超长标签应小于基准字号'
    assert app_module._measure_text_units(long_lab, fs) <= panel_w + 0.5, '缩小后仍须不超出一张图宽'
    assert abs(app_module._label_font_units('R', panel_w)
               - app_module.LABEL_FONT_RATIO * panel_w) < 1e-6, '短标签直接用基准字号'


if __name__ == '__main__':
    test_svg_single_structured_overlay_styles()
    test_svg_single_100pt_display_default()
    test_svg_single_nonsquare_canvas_100pt()
    test_svg_roi_stroke_follows_ui_thickness()
    test_svg_roi_dash_ratio_scales_with_linewidth()
    test_svg_layout_roi_dash_and_white_default()
    test_rgb_blue_plane_default_name_is_dapi()
    test_pdf_single_100pt_pagesize()
    test_pdf_roi_is_dashed()
    test_svg_single_native_roundtrip()
    test_svg_layout_scales_per_single_image()
    test_svg_layout_native_mode_unchanged()
    test_svg_no_canvas_white_background()
    test_svg_layout_draws_panel_labels_within_one_image_width()
    test_long_label_shrinks_to_fit_one_image_width()
    print('OK all export overlay style + 100pt display tests passed.')
