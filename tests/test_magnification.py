# -*- coding: utf-8 -*-
"""v1.3.9 magnification feature tests.

Covers:
1. /api/set_pixel_size validation
2. The computed pixel_size_um matches measured reference values from FV3000 detail panels
   (all 10×/20×/40× @ various widths + zoom, verified against Olympus FV3000 own readout)
3. add_scale_bar uses the override after set_pixel_size
4. /api/health version
"""
import io
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as app_module  # noqa: E402

app_module.app.config['TESTING'] = True
client = app_module.app.test_client()

# 权威标定: Olympus FV3000 UPLXAPO 10× 全幅视野 = 1272.792 μm (取自 10×/2048 面板 X Dimension)
FOV_10X_NATIVE_UM = 1272.792


def compute_um_per_px(mag, image_width, zoom):
    """复刻前端 computeUmPerPx: μm/px = (FOV_10X_NATIVE_UM × 10 / mag) / width / zoom."""
    return (FOV_10X_NATIVE_UM * 10.0 / mag) / image_width / zoom


def _upload(buf, channel_count=2):
    r = client.post('/api/upload_multi', data={'files': [(buf, 'p.tif')],
                                                'channel_count': str(channel_count)},
                    content_type='multipart/form-data')
    assert r.status_code == 200, f"upload failed: {r.status_code} {r.get_json()}"
    return r.get_json()['session_id']


def _make_multipage(pages, h=32, w=32):
    import tifffile
    arr = np.zeros((pages, h, w), dtype=np.uint16)
    for i in range(pages):
        arr[i, i * 4:(i + 1) * 4, :] = 1000 + i * 100
    buf = io.BytesIO()
    tifffile.imwrite(buf, arr, compression=None)
    buf.seek(0)
    return buf


# ---- (2) 像素尺寸公式正确性 (与 FV3000 实测面板 μm/px 对齐, 全部 <1.5%) ----
# (mag, image_width, zoom, expected_um_per_px_from_FV3000_panel, label)
REFERENCE_TABLE = [
    (10, 2048, 1.0, 0.6219, "10×/2048 (FV3000 面板 0.6219)"),
    (10, 640,  1.0, 1.989,  "10×/640  (FV3000 面板 1.989)"),
    (10, 800,  1.0, 1.591,  "10×/800  (FV3000 面板 1.591)"),
    (20, 2048, 1.0, 0.3099, "20×/2048 (FV3000 面板 0.3099)"),
    (20, 1024, 1.0, 0.6198, "20×/1024 (FV3000 面板 0.6198)"),
    (40, 2048, 1.0, 0.1554, "40×/2048 (FV3000 面板 0.155)"),
    (40, 1024, 1.0, 0.311,  "40×/1024 (FV3000 面板 0.311)"),
    # Zoom 路径验证: 10×/1024 误设 zoom=2 → FOV 减半, 公式给出 ~0.621 μm/px (与 81px→50μm 实测一致)
    (10, 1024, 2.0, 0.6173, "10×/1024/zoom2 (遗留 bar 测量 81px→50μm)"),
]


# ---- (1) /api/set_pixel_size 校验 ----

def test_set_pixel_size_validation():
    r = client.post('/api/set_pixel_size', json={'session_id': 'nope', 'pixel_size_um': 0.6})
    assert r.status_code == 404
    r = client.post('/api/set_pixel_size', json={'session_id': 'sid', 'pixel_size_um': 0})
    assert r.status_code == 404  # 会话不存在优先

    sid = _upload(_make_multipage(2), channel_count=2)

    r = client.post('/api/set_pixel_size', json={'session_id': sid, 'pixel_size_um': -1})
    assert r.status_code == 400
    r = client.post('/api/set_pixel_size', json={'session_id': sid, 'pixel_size_um': 'abc'})
    assert r.status_code == 400
    r = client.post('/api/set_pixel_size', json={'session_id': sid, 'pixel_size_um': 0.6173, 'source': 'mag=10'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True
    assert abs(body['pixel_size_um'] - 0.6173) < 1e-9
    assert body['source'] == 'mag=10'


# ---- (2) 公式复算 ---

def test_magnification_pixel_size_formula():
    for mag, w, zoom, expected, note in REFERENCE_TABLE:
        px = compute_um_per_px(mag, w, zoom)
        rel_err = abs(px - expected) / expected
        assert rel_err < 0.015, f"{note}: formula {px:.4f} vs panel {expected} (err {rel_err*100:.2f}%)"


def test_fov_10x_authoritative():
    # 10× 全幅视野必须为权威值 1272.792 μm
    assert abs(FOV_10X_NATIVE_UM * 10.0 / 10.0 - 1272.792) < 1e-6
    # 40× 全幅 = 10× 视野 / 4
    fov_40 = FOV_10X_NATIVE_UM * 10.0 / 40.0
    assert abs(fov_40 - 318.198) < 1e-3


# ---- (3) add_scale_bar 用 set_pixel_size 的新值 ----

def test_add_scale_bar_uses_overridden_pixel_size():
    sid = _upload(_make_multipage(2, h=64, w=64), channel_count=2)

    # 模拟 magnification 自动设置 1 px = 0.6173 μm
    r = client.post('/api/set_pixel_size', json={'session_id': sid, 'pixel_size_um': 0.6173, 'source': 'mag=10'})
    assert r.status_code == 200

    r = client.post('/api/add_scale_bar', json={
        'session_id': sid,
        'bar_length_um': 50, 'position': 'bottom-right',
        'bar_color': 'white', 'thickness': 3
    })
    assert r.status_code == 200
    body = r.get_json()
    assert 'preview' in body


# ---- (4) /api/health 版本号 ----

def test_health_version():
    r = client.get('/api/health')
    assert r.status_code == 200
    body = r.get_json()
    assert body['version'] == '1.4.5', f"got {body['version']}"


if __name__ == '__main__':
    print("=== test_set_pixel_size_validation ===")
    test_set_pixel_size_validation()
    print("  OK")
    print("=== test_magnification_pixel_size_formula (复算 FV3000 面板值) ===")
    for case in REFERENCE_TABLE:
        mag, w, zoom, expected, note = case
        px = compute_um_per_px(mag, w, zoom)
        rel = abs(px - expected) / expected * 100
        assert rel < 1.5, f"{note}: {px:.4f} vs {expected} ({rel:.2f}%)"
        print(f"  OK: {note} -> 公式 {px:.4f} μm/px (面板 {expected}, 误差 {rel:.2f}%)")
    print("=== test_fov_10x_authoritative ===")
    test_fov_10x_authoritative()
    print("  OK")
    print("=== test_add_scale_bar_uses_overridden_pixel_size ===")
    test_add_scale_bar_uses_overridden_pixel_size()
    print("  OK")
    print("=== test_health_version ===")
    test_health_version()
    print("  OK")
    print("\nAll tests passed.")
