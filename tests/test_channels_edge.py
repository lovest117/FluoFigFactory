# -*- coding: utf-8 -*-
"""边界场景测试: 多页 TIFF 含空页 / 全黑文件 / 恢复接口 / 灰度图无回归 / 弱信号不误杀"""
import io
import os
import sys

import numpy as np
import tifffile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import app as A  # noqa: E402

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp')
os.makedirs(TMP, exist_ok=True)


def upload(client, paths):
    """用 Werkzeug 的 (file, filename) 元组列表模拟多文件上传"""
    files = []
    for p in paths:
        with open(p, 'rb') as f:
            files.append((io.BytesIO(f.read()), os.path.basename(p)))
    r = client.post('/api/upload_multi', data={'files': files},
                    content_type='multipart/form-data')
    return r.get_json()


def signal(h=256, w=256, v=200):
    rng = np.random.default_rng(0)
    a = rng.integers(0, v, size=(h, w)).astype(np.uint8)
    return a


def main():
    client = A.app.test_client()
    ok = True

    def check(label, got, exp):
        nonlocal ok
        good = got == exp
        ok = ok and good
        print('[%s] %-38s 期望 %s 实得 %s' % ('OK ' if good else 'FAIL', label, exp, got))

    # 1) 多页 TIFF: 3 页(页0/页2 有信号, 页1 全 0)
    p = os.path.join(TMP, 'multipage_3.tif')
    tifffile.imwrite(p, np.stack([signal(), np.zeros((256, 256), np.uint8), signal(v=120)]))
    d = upload(client, [p])
    check('多页 TIFF(含 1 个空页) 通道数', len(d['configs']), 2)
    check('多页 TIFF dropped_empty', d.get('dropped_empty'), 1)

    # 2) 恢复被忽略的空通道
    r = client.post('/api/restore_empty_channels', json={'session_id': d['session_id']})
    rd = r.get_json()
    check('恢复后通道数', rd.get('active_count'), 3)
    check('恢复数量', rd.get('restored'), 1)

    # 3) 文件夹导入混入一个全黑文件
    p1 = os.path.join(TMP, 'ch_a.tif'); tifffile.imwrite(p1, signal())
    p2 = os.path.join(TMP, 'ch_blank.tif'); tifffile.imwrite(p2, np.zeros((256, 256), np.uint8))
    p3 = os.path.join(TMP, 'ch_b.tif'); tifffile.imwrite(p3, signal(v=90))
    d = upload(client, [p1, p2, p3])
    check('文件夹(含全黑文件) 通道数', len(d['configs']), 2)
    check('文件夹 dropped_empty', d.get('dropped_empty'), 1)

    # 4) 弱信号通道不能被误杀: 稀疏亮点 (非零 0.3%, 但唯一值多)
    sparse = np.zeros((256, 256), np.uint8)
    sparse[40:70, 40:70] = np.linspace(10, 250, 900).reshape(30, 30).astype(np.uint8)
    p4 = os.path.join(TMP, 'sparse.tif'); tifffile.imwrite(p4, sparse)
    p0 = os.path.join(TMP, 'normal.tif'); tifffile.imwrite(p0, signal())
    d = upload(client, [p0, p4])
    check('弱信号稀疏通道被保留', len(d['configs']), 2)

    # 5) 灰度单通道无回归 (旧示例文件)
    for fn in ['示例-通道1.tif', '示例-通道3.tif']:
        fp = os.path.join(BASE, fn)
        if os.path.exists(fp):
            d = upload(client, [fp])
            check('灰度图 %s 通道数' % fn, len(d['configs']), 1)

    # 6) 默认槽位颜色 = 405蓝/488绿/594红/640紫
    ps = []
    for i in range(4):
        q = os.path.join(TMP, 'slot%d.tif' % i); tifffile.imwrite(q, signal(v=100 + i * 30)); ps.append(q)
    d = upload(client, ps)
    check('4 通道默认颜色序列',
          [c['color'] for c in d['configs']],
          ['#0000FF', '#00FF00', '#FF0000', '#FF00FF'])
    check('4 通道默认名称序列',
          [c['name'] for c in d['configs']],
          ['DAPI', 'GFP', 'RFP', 'Cy5'])

    print('\n=== 结果:', 'ALL PASS' if ok else 'HAS FAILURE', '===')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
