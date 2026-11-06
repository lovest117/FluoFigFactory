// 通道匹配单测 (Node ESM): 从 static/js/main.js 抽取纯函数, 验证「按波长匹配物理平面」。
//
// 背景 (用户 2026-09-16 实测 bug): 单页 RGB merge 的通道平面固定 R→红 / G→绿 / B→蓝(DAPI),
// 但「应用文件名识别结果」原先按出现顺序套用建议, 于是 488(绿)+594(红) 被错配到 R/G 平面,
// 红绿通道互换。本测试锁定修复后的映射规则。
//
// 运行: node tests/test_channel_mapping.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

const here = path.dirname(fileURLToPath(import.meta.url));
const MAIN_JS = path.join(here, '..', 'static', 'js', 'main.js');
const src = readFileSync(MAIN_JS, 'utf8');

const START = 'function wavelengthToPlaneRgb(';
const END = '// ==== END PURE-FN ====';
const i = src.indexOf(START);
const j = src.indexOf(END);
assert.ok(i >= 0 && j > i, 'main.js 中未找到 PURE-FN 标记区, 请确认标记未被删除');

const factory = new Function(
    src.slice(i, j) + '\n; return { wavelengthToPlaneRgb, isSameRgb, planFilenameSuggestionTargets };'
);
const { wavelengthToPlaneRgb, isSameRgb, planFilenameSuggestionTargets } = factory();

// 复刻场景: 单页 RGB merge → 平面固定 R红 / G绿 / B蓝(DAPI)
const rgbMergeConfigs = () => ([
    { name: 'R',    rgb: [255, 0, 0] },
    { name: 'G',    rgb: [0, 255, 0] },
    { name: 'DAPI', rgb: [0, 0, 255] },
]);

// 1) 用户实测 bug: ayn488(绿) + hucd594(红) 必须分别落到 绿/红 平面, 不得顺序错配
{
    const configs = rgbMergeConfigs();
    const suggestions = [
        { name: 'ayn',  wavelength: 488, rgb: [0, 255, 0], color: '#00FF00' },
        { name: 'hucd', wavelength: 594, rgb: [255, 0, 0], color: '#FF0000' },
    ];
    const targets = planFilenameSuggestionTargets(suggestions, configs);
    assert.deepEqual(targets, [1, 0], '488→绿平面(1), 594→红平面(0)');

    // 模拟 applyFilenameSuggestions 的实际写入, 检查最终通道状态
    suggestions.forEach((c, k) => {
        Object.assign(configs[targets[k]], { name: c.name, rgb: c.rgb, color: c.color });
    });
    assert.equal(configs[0].name, 'hucd', '红平面应得到 594 通道名 hucd');
    assert.deepEqual(configs[0].rgb, [255, 0, 0], '红平面颜色必须保持红 (不得被强行改成绿)');
    assert.equal(configs[1].name, 'ayn', '绿平面应得到 488 通道名 ayn');
    assert.deepEqual(configs[1].rgb, [0, 255, 0], '绿平面颜色必须保持绿 (不得被强行改成红)');
    assert.equal(configs[2].name, 'DAPI', '蓝平面未被建议覆盖, 保持 DAPI');
}

// 2) 405 → 蓝平面 (索引 2)
{
    const configs = rgbMergeConfigs();
    assert.deepEqual(
        planFilenameSuggestionTargets([{ name: 'DAPI', wavelength: 405 }], configs), [2],
        '405 应匹配蓝平面');
}

// 3) 无波长信息 → 退回按出现顺序 (兼容旧命名)
{
    const configs = rgbMergeConfigs();
    assert.deepEqual(
        planFilenameSuggestionTargets(
            [{ name: 'A', wavelength: null }, { name: 'B', wavelength: null }], configs), [0, 1],
        '无波长时应按出现顺序映射');
}

// 4) 未知波长 → 取未占用通道; 同一通道不得被重复占用
{
    const configs = [{ rgb: [255, 0, 0] }, { rgb: [0, 255, 0] }];
    const targets = planFilenameSuggestionTargets(
        [{ name: 'X', wavelength: 999 }, { name: 'Y', wavelength: 488 }], configs);
    assert.deepEqual(targets, [0, 1], '未知波长先占未用通道, 488 仍取绿平面');
    assert.equal(new Set(targets).size, targets.length, '同一通道不得被两条建议重复占用');
}

// 5) 建议数多于通道数 → 多出的记为 -1, 不越界
{
    const configs = [{ rgb: [0, 255, 0] }];
    const targets = planFilenameSuggestionTargets(
        [{ name: 'A', wavelength: 488 }, { name: 'B', wavelength: 488 }], configs);
    assert.deepEqual(targets, [0, -1], '第二条建议无可用通道应返回 -1');
}

// 6) 波长表与后端 WAVELENGTH_CHANNEL_MAP 约定一致
assert.deepEqual(wavelengthToPlaneRgb(405), [0, 0, 255]);
assert.deepEqual(wavelengthToPlaneRgb(488), [0, 255, 0]);
assert.deepEqual(wavelengthToPlaneRgb(594), [255, 0, 0]);
assert.deepEqual(wavelengthToPlaneRgb(640), [255, 0, 255]);
assert.equal(wavelengthToPlaneRgb(999), null);
assert.equal(isSameRgb([1, 2, 3], [1, 2, 3]), true);
assert.equal(isSameRgb([1, 2, 3], [1, 2, 4]), false);
assert.equal(isSameRgb(null, [1, 2, 3]), false);

console.log('OK all channel-mapping tests passed.');
