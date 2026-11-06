"""Live parity test: preview (/api/add_scale_bar) vs export (/api/export_single) scale-bar length.

Reproduces the user-reported symptom: export scale bar shorter than preview / DAPI nucleus.
Also reproduces the desync scenario (pixel_size changed after preview render, before export).
"""
import sys, os, io, json, base64, re
import numpy as np
from PIL import Image

ROOT = r'D:\Apps\WorkBuddy_Workspace\2026-07-18-16-08-00\MicroScopeStudio'
sys.path.insert(0, ROOT)
import app as appmod

W, H = 1024, 1024
yy, xx = np.mgrid[0:H, 0:W]
cy, cx = H // 2, W // 2
nucleus_r = 60  # DAPI nucleus diameter = 120 px
data = np.zeros((H, W), dtype=np.uint8)
mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= nucleus_r ** 2
data[mask] = 220

sid = 'test-session'
appmod.session_data[sid] = {
    'channels': [data],
    'configs': [{'name': 'DAPI', 'color': 'blue', 'rgb': (50, 120, 255), 'visible': True,
                 'brightness': 1.0, 'contrast': 1.0, 'black': 0, 'white': 255, 'gamma': 1.0,
                 'detected': True}],
    'histograms': [],
    'active_count': 1,
    'metadata': {'width': W, 'height': H, 'num_channels': 1, 'pixel_size_um': 0.6219,
                 'magnification': 10, 'zoom_size': 1.0},
}

client = appmod.app.test_client()
nucleus_diam = 2 * nucleus_r
expected_bar = int(50 / 0.6219)


def measure_white_run(img):
    arr = np.array(img.convert('L'))
    H_, W_ = arr.shape
    best = 0
    for y in range(H_ - 60, H_):  # scale bar sits in bottom margin
        row = arr[y]
        run = mx = 0
        for v in row:
            if v > 200:
                run += 1
                mx = max(mx, run)
            else:
                run = 0
        best = max(best, mx)
    return best


def measure_preview(sid, length=50, psize=0.6219, send_ps=True):
    body = {
        'session_id': sid, 'bar_length_um': length, 'position': 'bottom-right',
        'bar_color': 'white', 'thickness_pt': 1.0, 'font_size_auto': True}
    if send_ps:
        body['pixel_size_um'] = psize
    r = client.post('/api/add_scale_bar', json=body)
    payload = r.get_json()
    assert 'preview' in payload, f'no preview in response: {payload}'
    b64 = payload['preview']
    if ',' in b64:
        b64 = b64.split(',', 1)[1]
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    return measure_white_run(img)


def measure_export_svg(sid, length=50, psize=0.6219, send_ps=True):
    body = {
        'session_id': sid, 'panel_type': 'ch0', 'format': 'svg', 'dpi': 300, 'svg_display_pt': 100,
        'scale_bar': {'length_um': length, 'position': 'bottom-right', 'color': 'white',
                      'thickness_pt': 1.0, 'font_size_auto': True, 'font_size_pt': None},
        'inset': None, 'roi': None}
    if send_ps:
        body['pixel_size_um'] = psize
    r = client.post('/api/export_single', json=body)
    svg = r.get_data().decode('utf-8')
    m = re.search(r'<g id="scale-bar".*?<rect[^>]*width="([\d.]+)"', svg, re.S)
    return float(m.group(1))


print('=== Scenario A: identical pixel_size (0.6219) ===')
pb = measure_preview(sid)
eb = measure_export_svg(sid)
print(f'expected_bar_px={expected_bar}  preview_bar_px={pb}  export_bar_px={eb}')
print(f'nucleus_diam={nucleus_diam}  ratio preview={pb/nucleus_diam:.4f}  ratio export={eb/nucleus_diam:.4f}')
assert abs(pb - eb) <= 2, 'PREVIEW vs EXPORT bar width diverge under identical pixel_size!'

print('\n=== Scenario B: desync (backend pixel_size stale=1.5, frontend sends correct 0.6219) ===')
# 模拟真实场景: 后端 sd 被手动改错 (1.5) 但用户前端看到的是正确值 0.6219,
# 前端在预览/导出请求中带上 pixel_size_um -> 两侧必须用前端值, 不再读后端旧值
appmod.session_data[sid]['metadata']['pixel_size_um'] = 1.5
pb2 = measure_preview(sid, psize=0.6219)
eb2 = measure_export_svg(sid, psize=0.6219)
print(f'backend=1.5 (stale)  frontend=0.6219 (sent)  expected_bar_px={expected_bar}  preview={pb2}  export={eb2}')
assert abs(pb2 - expected_bar) <= 2 and abs(eb2 - expected_bar) <= 2, \
    'Frontend pixel_size override not honored by preview/export!'
print('OK: preview & export both honor frontend pixel_size_um (ignore stale backend)')

print('\n=== Scenario C: control — neither sends pixel_size -> both read backend (reset to 1.5) ===')
appmod.session_data[sid]['metadata']['pixel_size_um'] = 1.5
pb3 = measure_preview(sid, send_ps=False)
eb3 = measure_export_svg(sid, send_ps=False)
print(f'backend=1.5  expected_bar_px={int(50/1.5)}  preview={pb3}  export={eb3}')
assert abs(pb3 - eb3) <= 2, 'preview/export diverge when reading backend only'
assert abs(pb3 - int(50 / 1.5)) <= 2, 'backend-only path did not use backend pixel_size_um'

print('\nDONE')
