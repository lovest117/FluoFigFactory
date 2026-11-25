# -*- coding: utf-8 -*-
"""M1 回归: 会话字节预算 / LRU 淘汰 / 导出快照 / 孤儿上传清理。

这些行为一旦回退, 表现为「跑几组 4096² 图后进程被系统杀掉」或
「导出期间界面滑块卡死」, 都属于难复现的现场故障, 因此用单测锁住。
"""
import os
import sys
import time

import numpy as np
import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app as A  # noqa: E402


@pytest.fixture()
def clean_state():
    """隔离全局 session_data 与预算设置, 测试后还原。"""
    saved_sessions = dict(A.session_data)
    saved_budget = A.SESSION_BUDGET_MB
    A.session_data.clear()
    yield
    A.session_data.clear()
    A.session_data.update(saved_sessions)
    A.SESSION_BUDGET_MB = saved_budget


def _fake_session(px=64, channels=2, dropped=0):
    h = w = px
    sd = {
        'channels': [np.zeros((h, w), dtype=np.uint16) for _ in range(channels)],
        'configs': [{'name': f'Ch{i}', 'rgb': (255, 0, 0)} for i in range(channels)],
        'dropped': [{'data': np.zeros((h, w), dtype=np.uint16), 'name': 'dead'} for _ in range(dropped)],
        'histograms': [],
        'active_count': channels,
        'metadata': {'width': w, 'height': h, 'pixel_size_um': 0.6219},
    }
    sd['_last_access'] = time.time()
    return sd


def test_session_bytes_counts_channels_and_dropped():
    sd = _fake_session(px=64, channels=2, dropped=1)
    expected = 3 * 64 * 64 * 2  # uint16 = 2 B
    assert A._session_bytes(sd) == expected


def test_enforce_budget_evicts_least_recently_used(clean_state):
    A.SESSION_BUDGET_MB = 6  # 每个会话 4 MiB → 只能留下 1 个 (12 → 4)
    old, mid, new = 'old-session', 'mid-session', 'new-session'
    for sid, ts in ((old, 100.0), (mid, 200.0), (new, 300.0)):
        sd = _fake_session(px=1024, channels=2)  # 每个 4 MiB
        sd['_last_access'] = ts
        A.session_data[sid] = sd

    evicted = A._enforce_budget(protect=new)

    assert evicted >= 1
    assert new in A.session_data, '当前请求的会话不得被淘汰'
    assert old not in A.session_data, '最久未访问的会话应最先淘汰'
    assert A._total_session_bytes() <= A.SESSION_BUDGET_MB * 1024 * 1024


def test_enforce_budget_keeps_protected_session_when_alone(clean_state):
    A.SESSION_BUDGET_MB = 1
    sid = 'only-session'
    A.session_data[sid] = _fake_session(px=1024, channels=2)  # 4 MiB > 1 MiB

    evicted = A._enforce_budget(protect=sid)

    assert evicted == 0
    assert sid in A.session_data, '宁可超预算也不能销毁用户当前工作'


def test_snapshot_isolates_configs_but_shares_arrays(clean_state):
    sid = 'snap'
    A.session_data[sid] = _fake_session(px=32, channels=1)
    A.session_data[sid]['metadata']['pixel_size_um'] = 0.5

    snap = A._snapshot_session(sid)
    assert snap is not None

    # 改快照不影响真实会话
    snap['configs'][0]['name'] = 'MODIFIED'
    snap['metadata']['pixel_size_um'] = 9.9
    assert A.session_data[sid]['configs'][0]['name'] == 'Ch0'
    assert A.session_data[sid]['metadata']['pixel_size_um'] == 0.5

    # 改真实会话不影响已取出的快照 (色阶在导出期间被拖动)
    A.session_data[sid]['configs'][0]['rgb'] = (0, 255, 0)
    assert snap['configs'][0]['rgb'] == (255, 0, 0)

    # 数组按引用共享 (大数据不做拷贝), 因此导出只能读
    assert snap['channels'][0] is A.session_data[sid]['channels'][0]

    # 访问会刷新 LRU 时间戳
    assert A.session_data[sid]['_last_access'] >= 0


def test_snapshot_missing_session_returns_none(clean_state):
    assert A._snapshot_session('nope') is None


def test_set_pixel_size_writes_real_session(clean_state):
    sid = 'px'
    A.session_data[sid] = _fake_session(px=16, channels=1)
    assert A._set_session_pixel_size(sid, 0.3099) == 0.3099
    assert A.session_data[sid]['metadata']['pixel_size_um'] == 0.3099
    assert A._set_session_pixel_size('missing', 1.0) is None


def test_purge_orphan_uploads_respects_ttl(clean_state):
    stale = os.path.join(A.UPLOAD_FOLDER, 'stale-test.tif')
    fresh = os.path.join(A.UPLOAD_FOLDER, 'fresh-test.tif')
    for path in (stale, fresh):
        with open(path, 'wb') as f:
            f.write(b'x' * 1024)
    old = time.time() - 7200
    os.utime(stale, (old, old))

    removed, freed = A._purge_orphan_uploads(max_age_sec=3600)
    assert removed == 1 and freed == 1024
    assert not os.path.exists(stale)
    assert os.path.exists(fresh), '刚上传的文件不能被误删'

    A._purge_orphan_uploads(max_age_sec=0)
    assert not os.path.exists(fresh), '启动时上传目录里的一切都是孤儿'


def test_health_reports_session_stats(clean_state):
    A.session_data['h'] = _fake_session(px=1024, channels=1)  # 2 MiB
    payload = A.app.test_client().get('/api/health').get_json()
    assert payload['sessions'] == 1
    assert payload['session_budget_mb'] == A.SESSION_BUDGET_MB
    assert payload['session_mb'] > 0
