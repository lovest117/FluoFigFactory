# -*- coding: utf-8 -*-
"""v1.4.0 文件名元数据解析测试 (parse_filename_metadata)。

覆盖:
1. 用户典型命名 `sham4-6ileum2.2-20X-asyn488chat594.tif.frames.tiff`
   → magnification=20, channels=[asyn/488, chat/594]
2. 样本号 `sham4` / `ileum2.2` 不得被误判为倍数 (无 X/x 后缀)
3. 多扩展名 `.frames.tiff` 正确剥离
4. 小写 `x` 也能识别倍数
5. 未知波长 (777) 通道只回名字, 不强配色 (color=None)
6. 无倍数文件名 mag=None
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as app_module  # noqa: E402

parse = app_module.parse_filename_metadata


def _ch_map(meta):
    return {c['name']: c['wavelength'] for c in meta['channels']}


def test_user_typical_filename():
    fn = "sham4-6ileum2.2-20X-asyn488chat594.tif.frames.tiff"
    meta = parse(fn)
    assert meta['magnification'] == 20, meta
    ch = _ch_map(meta)
    assert ch.get('asyn') == 488, ch
    assert ch.get('chat') == 594, ch
    # 顺序: asyn 在前, chat 在后
    assert [c['name'] for c in meta['channels']] == ['asyn', 'chat']


def test_sample_numbers_not_misread_as_mag():
    # 无 X/x 后缀的数字不应是倍数
    assert parse("sham4-6ileum2.2.tif")['magnification'] is None
    # 即便有数字也不应误判
    assert parse("ileum2-control-3.tif")['magnification'] is None


def test_multiext_stripped():
    fn = "mouse-brain-40X-gfp594.tif.frames.tiff"
    meta = parse(fn)
    assert meta['magnification'] == 40, meta
    assert _ch_map(meta).get('gfp') == 594


def test_lowercase_x():
    assert parse("sample-10x-dapi405.tif")['magnification'] == 10
    # 405 一律强制命名为 DAPI (大写, 用户规则)
    assert _ch_map(parse("sample-10x-dapi405.tif")).get('DAPI') == 405


def test_405_forced_dapi():
    # 用户规则 (2026-09-16): 405/蓝色通道不采纳文件名字母, 一律默认 DAPI
    # (`t405` 曾被误识别为通道 "T")
    meta = parse("sample-20X-t405chat594.tif")
    names = [c['name'] for c in meta['channels']]
    assert 'T' not in names, names
    ch = _ch_map(meta)
    assert ch.get('DAPI') == 405, ch
    assert ch.get('chat') == 594, ch
    c405 = [c for c in meta['channels'] if c['wavelength'] == 405][0]
    assert c405['color'] == '#0000FF'
    assert c405['default_name'] == 'DAPI'


def test_unknown_wavelength_no_color():
    meta = parse("weird-xyz777.tif")
    assert meta['magnification'] is None
    assert len(meta['channels']) == 1
    c = meta['channels'][0]
    assert c['name'] == 'xyz'
    assert c['wavelength'] == 777
    assert c['color'] is None
    assert c['rgb'] is None


def test_no_mag_filename():
    assert parse("just-a-name.tiff")['magnification'] is None
    assert parse("just-a-name.tiff")['channels'] == []


def test_mag_range_guard():
    # 超过 99 不被接受 (避免异常大数)
    assert parse("zoom-200X-a488.tif")['magnification'] is None


def test_fv3000_slot_dapi_ignores_scan_code():
    # FV3000 命名 `xxx_C001T001`: C001 决定蓝色通道(405/DAPI);
    # T001 是「扫描序号」, 其数字(1)曾被误当波长 → 显示为 "T (1nm)", 必须忽略
    fn = "20240909-brain-20X_C001T001_DAPI.tif"
    meta = parse(fn)
    names = [c['name'] for c in meta['channels']]
    assert 'T' not in names, names
    assert 'T (1nm)' not in names, names
    ch = _ch_map(meta)
    assert ch.get('DAPI') == 405, ch
    c405 = [c for c in meta['channels'] if c['wavelength'] == 405][0]
    assert c405['color'] == '#0000FF'
    assert c405['default_name'] == 'DAPI'


def test_594_wavelength_red():
    # 用户规则 (2026-09-16): 文件名波长 594 一律红色 (曾错配橙色 #FF6600);
    # 与 FV3000 C003 槽位、CHANNEL_KEYWORDS['rfp'] 三路颜色必须一致
    meta = parse("ctx-20X-hucd594.tif")
    c594 = [c for c in meta['channels'] if c['wavelength'] == 594][0]
    assert c594['name'] == 'hucd', c594
    assert c594['color'] == '#FF0000', c594
    assert c594['rgb'] == (255, 0, 0), c594


if __name__ == '__main__':
    test_user_typical_filename(); print("OK test_user_typical_filename")
    test_sample_numbers_not_misread_as_mag(); print("OK test_sample_numbers_not_misread_as_mag")
    test_multiext_stripped(); print("OK test_multiext_stripped")
    test_lowercase_x(); print("OK test_lowercase_x")
    test_405_forced_dapi(); print("OK test_405_forced_dapi")
    test_594_wavelength_red(); print("OK test_594_wavelength_red")
    test_unknown_wavelength_no_color(); print("OK test_unknown_wavelength_no_color")
    test_no_mag_filename(); print("OK test_no_mag_filename")
    test_mag_range_guard(); print("OK test_mag_range_guard")
    test_fv3000_slot_dapi_ignores_scan_code(); print("OK test_fv3000_slot_dapi_ignores_scan_code")
    print("\nAll filename parser tests passed.")
