# -*- coding: utf-8 -*-
"""通道识别回归测试: 用示例 TIFF 验证「N 通道 TIFF → N 个通道」"""
import io
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app as A  # noqa: E402

FILES = ['示例1通道.tif', '示例2通道.tif', '示例3通道.tif', '示例4通道.tif']
# 期望: 文件内含几个有信号的通道 (伪彩 RGB 最多只能拆出 3 个平面)
EXPECT = {'示例1通道.tif': 1, '示例2通道.tif': 2, '示例3通道.tif': 3, '示例4通道.tif': 3}


def main():
    client = A.app.test_client()
    ok = True
    for fn in FILES:
        path = os.path.join(BASE, fn)
        if not os.path.exists(path):
            print('[SKIP] 缺少文件', fn)
            continue
        with open(path, 'rb') as f:
            blob = f.read()
        resp = client.post('/api/upload', data={
            'file': (io.BytesIO(blob), fn)
        }, content_type='multipart/form-data')
        d = resp.get_json()
        if d is None or 'error' in d:
            print('[FAIL]', fn, d)
            ok = False
            continue
        n = len(d['configs'])
        exp = EXPECT[fn]
        flag = 'OK ' if n == exp else 'FAIL'
        if n != exp:
            ok = False
        names = [c['name'] for c in d['configs']]
        colors = [c['color'] for c in d['configs']]
        print('[%s] %-16s 期望 %d 通道 -> 实得 %d  %s %s  dropped=%d  active=%s'
              % (flag, fn, exp, n, names, colors,
                 d.get('dropped_empty', 0), d.get('detected_channels')))
    print('\n=== 结果:', 'ALL PASS' if ok else 'HAS FAILURE', '===')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
