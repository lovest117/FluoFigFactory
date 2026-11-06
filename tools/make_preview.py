# -*- coding: utf-8 -*-
"""生成 README 预览图: 合成 3 通道荧光数据 -> 走真实 /api 导出流程 -> docs/*.png"""
import io
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
import tifffile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import app as A  # noqa: E402

W = H = 512
rng = np.random.default_rng(20260918)


def _blur(arr, r=1.2):
    im = Image.fromarray(arr.astype(np.uint8), 'L')
    return np.asarray(im.filter(ImageFilter.GaussianBlur(r)))


def dapi_layer():
    """细胞核: 大量椭圆 + 轻微模糊"""
    im = Image.new('L', (W, H), 0)
    d = ImageDraw.Draw(im)
    for _ in range(90):
        cx, cy = rng.integers(10, W - 10), rng.integers(10, H - 10)
        rx, ry = rng.integers(5, 11), rng.integers(4, 9)
        v = int(rng.integers(110, 235))
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=v)
        d.ellipse([cx - rx // 2, cy - ry // 2, cx + rx // 2, cy + ry // 2], fill=min(255, v + 30))
    return _blur(np.asarray(im), 1.0)


def gfp_layer():
    """神经元: 胞体 + 放射状突起"""
    im = Image.new('L', (W, H), 0)
    d = ImageDraw.Draw(im)
    centers = [(150, 170), (350, 300), (300, 130)]
    for (cx, cy) in centers:
        d.ellipse([cx - 30, cy - 26, cx + 30, cy + 26], fill=210)
        n_branch = 7
        for k in range(n_branch):
            ang = 2 * np.pi * k / n_branch + rng.uniform(-0.3, 0.3)
            x, y, a = float(cx), float(cy), ang
            for step in range(46):
                a += rng.uniform(-0.22, 0.22)
                x += 4.2 * np.cos(a)
                y += 4.2 * np.sin(a)
                if not (0 <= x < W and 0 <= y < H):
                    break
                w = max(1.0, 4.0 * (1 - step / 52))
                d.line([x - w, y - w, x + w, y + w], fill=int(190 * (1 - step / 60)))
    return _blur(np.asarray(im), 0.9)


def rfp_layer():
    """突触小点: 稀疏亮点"""
    im = Image.new('L', (W, H), 0)
    d = ImageDraw.Draw(im)
    for _ in range(140):
        cx, cy = rng.integers(0, W), rng.integers(0, H)
        r = rng.integers(1, 4)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=int(rng.integers(120, 255)))
    return _blur(np.asarray(im), 0.6)


def upload(client, name, arr):
    buf = io.BytesIO()
    tifffile.imwrite(buf, arr, photometric='minisblack')
    buf.seek(0)
    return (buf, name)


def main():
    os.makedirs(os.path.join(BASE, 'docs'), exist_ok=True)
    client = A.app.test_client()

    files = [
        upload(client, 'demo_405_DAPI.tif', dapi_layer()),
        upload(client, 'demo_488_GFP.tif', gfp_layer()),
        upload(client, 'demo_594_RFP.tif', rfp_layer()),
    ]
    r = client.post('/api/upload_multi', data={'files': files}, content_type='multipart/form-data')
    meta = r.get_json()
    if 'error' in meta:
        raise SystemExit('upload failed: %s' % meta['error'])
    sid = meta['session_id']
    print('session:', sid)
    for c in meta['configs']:
        print('  channel:', c.get('name'), c.get('rgb'), c.get('color'))

    pixel = 0.3099  # 20x / 2048 宽
    client.post('/api/set_pixel_size', json={'session_id': sid, 'pixel_size_um': pixel})

    base_payload = {
        'session_id': sid,
        'panels': ['ch0', 'ch1', 'ch2', 'merge'],
        'dpi': 300,
        'labels': 'name',
        'layout': '1xn',
        'margin': 14,
        'native_size': False,
        'pixel_size_um': pixel,
        'scale_bar': {'length_um': 50, 'position': 'bottom-right',
                      'color': '#ffffff', 'thickness': 1.0, 'font_size': 0},
    }

    # 1) 纯排版预览
    payload = dict(base_payload, format='png')
    r = client.post('/api/export', json=payload)
    if r.status_code != 200:
        raise SystemExit('export png failed: %s %s' % (r.status_code, r.data[:300]))
    with open(os.path.join(BASE, 'docs', 'preview.png'), 'wb') as f:
        f.write(r.data)
    print('docs/preview.png', len(r.data), 'bytes')

    # 2) ROI + Inset 预览
    payload = dict(base_payload, format='png',
                   roi={'x': 108, 'y': 108, 'width': 170, 'height': 170,
                        'color': '#ffffff', 'stroke_width_pt': 1.0},
                   inset={'enabled': True, 'zoom': 2.0, 'position': 'top-right'})
    r = client.post('/api/export', json=payload)
    if r.status_code != 200:
        raise SystemExit('export roi failed: %s %s' % (r.status_code, r.data[:300]))
    with open(os.path.join(BASE, 'docs', 'preview-roi.png'), 'wb') as f:
        f.write(r.data)
    print('docs/preview-roi.png', len(r.data), 'bytes')


if __name__ == '__main__':
    main()
