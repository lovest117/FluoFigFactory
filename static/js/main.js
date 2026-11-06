/**
 * FluoFigFactory - 前端核心逻辑
 * 支持: 多文件上传、6 面板排版、Inset、Profile plot
 */

const APP = {
    sessionId: null,
    channels: [],
    configs: [],
    activeCount: 0,
    metadata: {},
    mergedPreview: null,
    scaleBarApplied: false,
    scaleBarConfig: null,
    currentView: 'merge',
    activeChannel: 0,
    tool: 'pan',
    zoom: 1,
    canvasOffset: { x: 0, y: 0 },
    roi: null,
    linePoints: [],
    lineData: null,    // 后端返回的 profile 数据
    pearsonResult: null,
    isDragging: false,
    dragStart: null,
    roiMode: null,      // 'create' | 'move' | 'resize-tl/tr/bl/br'
    isSettingInset: false,
    insetSourceImg: null,  // 缓存当前显示图像对象 (用于前端实时放大预览)
    // 通道显示模式: 'color' 按通道色伪彩着色 | 'gray' 原始灰度 (看清弱信号细节)
    // 后端 channel_previews 统一返回灰度, 着色在前端完成, 故切换零延迟
    displayMode: localStorage.getItem('ecs_display_mode') || 'color',
};

// ======== 通道着色 (灰度 → 伪彩) ========
// 取通道的 RGB 三元组 (configs 里可能是 hex 字符串或 rgb 数组)
function _channelRgbArr(idx) {
    const cfg = (APP.configs && APP.configs[idx]) || {};
    if (Array.isArray(cfg.rgb) && cfg.rgb.length >= 3) {
        return [cfg.rgb[0], cfg.rgb[1], cfg.rgb[2]];
    }
    const hex = String(cfg.color || '#ffffff').replace('#', '');
    if (hex.length < 6) return [255, 255, 255];
    return [
        parseInt(hex.substr(0, 2), 16),
        parseInt(hex.substr(2, 2), 16),
        parseInt(hex.substr(4, 2), 16)
    ];
}

/**
 * 把灰度图着色为通道伪彩, 返回离屏 canvas (可直接 drawImage)
 * 用 multiply 混合模式: result = gray * rgb / 255 —— 与后端着色公式完全等价, 且走 GPU
 * @param {HTMLImageElement|HTMLCanvasElement} img 灰度源图
 * @param {number[]} rgb [r,g,b]
 */
function _colorizeImg(img, rgb) {
    if (!rgb || (rgb[0] === 255 && rgb[1] === 255 && rgb[2] === 255)) return img;  // 白色无需处理
    const off = document.createElement('canvas');
    off.width = img.width;
    off.height = img.height;
    const c = off.getContext('2d');
    c.drawImage(img, 0, 0);
    c.globalCompositeOperation = 'multiply';
    c.fillStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
    c.fillRect(0, 0, off.width, off.height);
    c.globalCompositeOperation = 'source-over';
    return off;
}

// 当前显示模式下, 通道 idx 应使用的着色参数 (灰度模式返回 null = 不着色)
function _paintRgbFor(idx) {
    return APP.displayMode === 'color' ? _channelRgbArr(idx) : null;
}

// 切换灰度 / 彩色显示
function toggleDisplayMode() {
    APP.displayMode = APP.displayMode === 'color' ? 'gray' : 'color';
    localStorage.setItem('ecs_display_mode', APP.displayMode);
    syncDisplayModeBtn();
    if (APP.sessionId) renderMainCanvas();
}

function syncDisplayModeBtn() {
    const btn = document.getElementById('view-colormode');
    if (!btn) return;
    const isColor = APP.displayMode === 'color';
    btn.textContent = isColor ? '彩色' : '灰度';
    btn.title = isColor ? '当前: 伪彩色显示 — 点击切为原始灰度' : '当前: 原始灰度 — 点击切为伪彩色';
    btn.classList.toggle('btn-active', isColor);
}

// 通道参数防抖更新 (拖动滑块时减少后端请求频率, 仍保持实时反馈)
function debouncedUpdateParam(idx, param, value) {
    if (!APP._paramTimers) APP._paramTimers = {};
    const key = idx + '_' + param;
    clearTimeout(APP._paramTimers[key]);
    APP._paramTimers[key] = setTimeout(() => updateChannelParam(idx, param, value), 60);
}

function getInsetConfig() {
    return {
        enabled: document.getElementById('inset-enabled').checked,
        zoom: parseFloat(document.getElementById('inset-zoom').value) || 2.0,
        position: document.getElementById('inset-position').value
    };
}

const canvas = document.getElementById('main-canvas');
const ctx = canvas.getContext('2d');
const container = document.getElementById('canvas-container');

// ======== 上传 ========
function triggerUploadSingle() { document.getElementById('file-input-single').click(); }
function triggerUploadMulti() { document.getElementById('file-input-multi').click(); }

async function handleUploadSingle(event) {
    const file = event.target.files[0];
    if (!file) return;
    await uploadFiles([file], '/api/upload');
    event.target.value = '';
}

async function handleUploadMulti(event) {
    const files = Array.from(event.target.files);
    if (!files.length) return;
    await uploadFiles(files, '/api/upload_multi');
    event.target.value = '';
}

function triggerUploadFolder() { document.getElementById('file-input-folder').click(); }

async function handleUploadFolder(event) {
    const all = Array.from(event.target.files);
    if (!all.length) return;
    // 浏览器端先过滤 TIFF, 避免上传文件夹内的 Results.csv 等无关文件
    const tiffFiles = all.filter(f => {
        const ext = (f.name.split('.').pop() || '').toLowerCase();
        return ext === 'tif' || ext === 'tiff';
    });
    if (!tiffFiles.length) {
        showError('未找到 TIFF', '所选文件夹内没有 .tif / .tiff 文件');
        event.target.value = '';
        return;
    }
    const channelCount = parseInt(document.getElementById('ch-count-select').value) || null;
    await uploadFiles(tiffFiles, '/api/upload_multi', channelCount, all.length - tiffFiles.length);
    event.target.value = '';
}

async function uploadFiles(files, endpoint, channelCount = null, skippedOnClient = 0) {
    if (!files || !files.length) return;
    hideEmptyChannelsHint();   // 新一次导入: 先清掉上一轮的「已忽略空通道」提示
    showLoading(`正在处理 ${files.length} 个文件...`);
    setStatus(`正在处理 ${files.length} 个 TIFF 文件...`);

    const formData = new FormData();
    if (endpoint.includes('multi')) {
        files.forEach(f => formData.append('files', f));
        if (channelCount) formData.append('channel_count', String(channelCount));
    } else {
        formData.append('file', files[0]);
    }

    try {
        const resp = await fetch(endpoint, { method: 'POST', body: formData });
        const text = await resp.text();

        let data;
        try {
            data = JSON.parse(text);
        } catch (e) {
            // 关键: 即使后端出问题, 也给用户看得懂的错误
            hideLoading();
            showError('服务器返回了非 JSON 数据', `${resp.status} ${resp.statusText}\n${text.substring(0, 300)}`);
            return;
        }

        if (data.error) {
            hideLoading();
            showError('上传失败', data.error);
            return;
        }

        // 成功
        APP.sessionId = data.session_id;
        APP.channels = data.channel_previews;
        APP.configs = data.configs;
        // 用后端返回的有效通道数 (已被自动过滤掉无信号的空通道),
        // 而不是 configs.length —— 否则多通道图里的空通道会被算进显示/导出
        APP.activeCount = (data.active_count != null ? data.active_count : data.configs.length);
        APP.activeChannel = 0;
        APP.metadata = data.metadata;
        // FV3000 TIFF 自动解析出物镜倍数 / Zoom 时, 预填 magnification + zoom 输入框,
        // 并刷新换算读数。像素尺寸已由后端用 X Dimension 真实值设定, 此处仅做展示/校验, 不覆盖。
        if (data.metadata && data.metadata.magnification) {
            const magEl = document.getElementById('scalebar-mag');
            const zoomEl = document.getElementById('scalebar-zoom');
            if (magEl) magEl.value = data.metadata.magnification;
            if (zoomEl) zoomEl.value = (data.metadata.zoom_size != null ? data.metadata.zoom_size : 1.0);
            onMagnificationChange();
        }
        APP.mergedPreview = data.merged_preview;
        APP.histograms = data.histograms || [];
        // 记录每个通道"上次后端渲染时使用的色阶" (用于本地预览的 delta 变换)
        APP._lastLevels = APP.configs.map(c => ({
            black: c.black ?? 0, white: c.white ?? 255, gamma: c.gamma ?? 1.0
        }));
        APP.roi = null;
        APP.linePoints = [];
        APP.lineData = null;
        APP.pearsonResult = null;
        APP.scaleBarApplied = false;

        // 标题里通道数选择器同步为当前通道数
        refreshChCountSelect();

        // FV3000 默认值 1.0 不可靠时, 自动应用用户保存的默认像素尺寸
        const savedPS = loadDefaultPixelSize();
        if (savedPS && (APP.metadata.pixel_size_um == null || APP.metadata.pixel_size_um === 1.0)) {
            APP.metadata.pixel_size_um = savedPS;
            // 同步回后端, 否则导出读取的仍是不可靠的 1.0, 导致标尺物理比例失真
            syncPixelSizeToServer(savedPS, 'default');
        }

        renderChannelList();
        updateImageInfo();
        showFilenameSuggestions(APP.metadata && APP.metadata.filename_meta);
        renderMainCanvas();
        renderLevelsPanel();
        updateAnalysisUI();
        updateExportPanels();
        zoomFit();

        document.getElementById('btn-export').disabled = false;

        // 自动忽略的空通道提示 (可一键恢复) —— 自动识别的兜底
        showEmptyChannelsHint(data.dropped_empty || 0, data.dropped_details || []);

        if (data.needs_color_check) showChannelColorPicker();

        // 通道数反馈 (先选通道数 → 导入 → 自动识别对应图片)
        const detected = data.detected_channels != null ? data.detected_channels : APP.metadata.num_channels;
        const expected = data.expected_channels;
        let msg = `已加载 ${files.length} 个 TIFF · ${APP.metadata.width}×${APP.metadata.height} · 识别 ${detected} 通道`;
        if (expected && detected !== expected) {
            msg += ` (预期 ${expected})`;
        }
        if (data.merge_detected) msg += ' · 含 Merge';
        const skippedTotal = (data.skipped_files ? data.skipped_files.length : 0) + (skippedOnClient || 0);
        if (skippedTotal > 0) msg += ` · 跳过 ${skippedTotal} 个非TIFF`;
        setStatus(msg);
    } catch (err) {
        hideLoading();
        showError('网络错误', err.message);
    }
    hideLoading();
}

function showError(title, detail) {
    const overlay = document.createElement('div');
    overlay.className = 'error-modal';
    overlay.innerHTML = `
        <div class="error-content">
            <div class="error-title">⚠️ ${title}</div>
            <div class="error-detail">${detail || ''}</div>
            <button class="btn btn-primary" onclick="this.parentElement.parentElement.remove()">关闭</button>
        </div>
    `;
    document.body.appendChild(overlay);
}

// ======== 通道控制 ========
let dragSrcIndex = null;  // 通道拖拽源索引

// ======== 通道名记忆 (历史自动补全) + 蓝色通道默认 DAPI ========
const CH_NAME_HISTORY_KEY = 'mscope_channel_name_history';
function loadChannelNameHistory() {
    try { return JSON.parse(localStorage.getItem(CH_NAME_HISTORY_KEY) || '[]'); } catch (e) { return []; }
}
function rememberChannelName(value) {
    value = (value || '').trim();
    if (!value) return;
    const h = loadChannelNameHistory().filter(v => v !== value);
    h.unshift(value);
    if (h.length > 40) h.length = 40;
    try { localStorage.setItem(CH_NAME_HISTORY_KEY, JSON.stringify(h)); } catch (e) {}
    refreshChannelNameDatalist();
}
function refreshChannelNameDatalist() {
    const dl = document.getElementById('channel-name-history');
    if (!dl) return;
    dl.innerHTML = '';
    loadChannelNameHistory().forEach(v => {
        const o = document.createElement('option');
        o.value = v;
        dl.appendChild(o);
    });
}
// 判断颜色是否以蓝为主 (DAPI / Hoechst / 405nm)
function isBlueColor(hex) {
    if (!hex || typeof hex !== 'string') return false;
    const m = hex.replace('#', '');
    if (m.length !== 6) return false;
    const r = parseInt(m.slice(0, 2), 16),
          g = parseInt(m.slice(2, 4), 16),
          b = parseInt(m.slice(4, 6), 16);
    return b > r && b > g && b >= 120;
}
// 是否仍是自动占位名 (可被 DAPI 默认覆盖, 不影响用户自定义名)
function isGenericName(n) {
    if (!n) return true;
    n = String(n).trim();
    if (/^(channel|ch|通道)\s*\d*$/i.test(n)) return true;
    if (/^\d{2,4}$/.test(n)) return true; // 纯波长如 405
    if (n === 'B') return true;           // RGB 拆平面的蓝通道自动名, 应被 DAPI 默认覆盖
    return false;
}
// 由通道主色生成短码 (文献常用 G/R/B 表示法): B/G/R/C/M/Y, 无主导色取名称首字母
function shortChannelCode(colorHex, name) {
    const fallback = (name || 'C')[0].toUpperCase();
    if (!colorHex || typeof colorHex !== 'string') return fallback;
    const m = colorHex.replace('#', '');
    if (m.length !== 6) return fallback;
    const r = parseInt(m.slice(0, 2), 16),
          g = parseInt(m.slice(2, 4), 16),
          b = parseInt(m.slice(4, 6), 16);
    const mx = Math.max(r, g, b);
    if (mx <= 60) return fallback;
    if (b > r && b > g) return 'B';
    if (g > r && g > b) return 'G';
    if (r > g && r > b) return 'R';
    if (Math.abs(g - b) < 40 && g > r + 40) return 'C';
    if (Math.abs(r - b) < 40 && r > g + 40) return 'M';
    if (Math.abs(r - g) < 40 && r > b + 40) return 'Y';
    return fallback;
}

function renderChannelList() {
    const list = document.getElementById('channel-list');
    list.innerHTML = '';
    const activeCount = APP.activeCount || (APP.configs ? APP.configs.length : 0);

    APP.configs.slice(0, activeCount).forEach((config, i) => {
        const item = document.createElement('div');
        // 注意: 不能用全局 '.hidden' (CSS:607 display:none !important), 否则整个通道项会消失无法再点选
        item.className = 'channel-item' + (APP.activeChannel === i ? ' active' : '') + (config.visible ? '' : ' dimmed');
        item.onclick = (e) => {
            if (e.target.tagName === 'INPUT' || e.target.classList.contains('channel-toggle') || e.target.classList.contains('channel-color-dot') || e.target.classList.contains('channel-drag-handle')) return;
            setActiveChannel(i);
        };

        // 可见性勾选框 (取消则该通道不显示)
        const toggle = document.createElement('input');
        toggle.type = 'checkbox';
        toggle.className = 'channel-toggle';
        toggle.checked = !!config.visible;
        toggle.style.accentColor = config.color;
        toggle.title = '勾选后该通道显示, 取消则隐藏';
        toggle.onchange = () => toggleChannel(i);

        // 拖拽手柄 (仅手柄可拖, 避免与滑块/输入框冲突)
        const handle = document.createElement('div');
        handle.className = 'channel-drag-handle';
        handle.title = '拖拽调整通道顺序';
        handle.textContent = '⋮⋮';
        handle.draggable = true;
        handle.addEventListener('dragstart', (e) => {
            dragSrcIndex = i;
            item.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            try { e.dataTransfer.setData('text/plain', String(i)); } catch (_) {}
        });
        handle.addEventListener('dragend', () => {
            item.classList.remove('dragging');
            document.querySelectorAll('.channel-item').forEach(el => el.classList.remove('drag-over'));
        });
        item.addEventListener('dragover', (e) => { e.preventDefault(); item.classList.add('drag-over'); });
        item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
        item.addEventListener('drop', (e) => {
            e.preventDefault();
            item.classList.remove('drag-over');
            if (dragSrcIndex === null || dragSrcIndex === i) return;
            reorderChannels(dragSrcIndex, i);
        });

        const dot = document.createElement('input');
        dot.type = 'color';
        dot.className = 'channel-color-dot';
        dot.value = config.color;
        dot.style.width = '20px';
        dot.style.height = '20px';
        dot.style.padding = '0';
        dot.style.border = '1px solid var(--border-color)';
        dot.style.borderRadius = '4px';
        dot.style.cursor = 'pointer';
        dot.title = '点击修改通道颜色';
        dot.onchange = () => changeChannelColor(i, dot.value);
        dot.oninput = () => changeChannelColor(i, dot.value);

        const name = document.createElement('input');
        name.type = 'text';
        name.className = 'channel-name';
        name.setAttribute('list', 'channel-name-history');
        // 蓝色通道默认名为 DAPI (荧光核染)
        let effectiveName = config.name;
        if (isBlueColor(config.color) && isGenericName(config.name)) {
            effectiveName = 'DAPI';
        }
        name.value = effectiveName;
        name.onchange = () => {
            const v = name.value.trim() || effectiveName;
            name.value = v;
            updateChannelName(i, v);
            rememberChannelName(v);
        };
        // 若应用了 DAPI 默认, 同步到内存与后端
        if (effectiveName !== config.name) {
            updateChannelName(i, effectiveName);
            rememberChannelName(effectiveName);
        }

        item.appendChild(toggle);
        item.appendChild(handle);
        item.appendChild(dot);
        item.appendChild(name);
        list.appendChild(item);
    });
}

// ======== 色阶调节面板 (PS 式: 标签切换 + 直方图 + 共享滑块) ========
function shortCode(name) {
    const n = (name || '').toUpperCase();
    if (n.includes('DAPI') || n.includes('BLUE') || n === 'B') return 'B';
    if (n.includes('GFP') || n.includes('GREEN') || n === 'G') return 'G';
    if (n.includes('RFP') || n.includes('RED') || n === 'R') return 'R';
    if (n.includes('CY5') || n.includes('MAGENTA') || n === 'M') return 'M';
    if (n.includes('CFP') || n.includes('CYAN') || n === 'C') return 'C';
    if (n.includes('YFP') || n.includes('YELLOW') || n === 'Y') return 'Y';
    return (name || 'Ch').slice(0, 4);
}

function renderLevelsPanel() {
    if (!APP.configs || !APP.configs.length) return;
    renderLevelsTabs();
    renderLevelsSliders();
    drawLevelsHistogram();
}

function renderLevelsTabs() {
    const tabs = document.getElementById('levels-tabs');
    if (!tabs) return;
    const ac = APP.activeCount || APP.configs.length;
    tabs.innerHTML = '';
    APP.configs.slice(0, ac).forEach((cfg, i) => {
        const tab = document.createElement('button');
        tab.className = 'levels-tab' + (APP.activeChannel === i ? ' active' : '') + (cfg.visible ? '' : ' hidden-ch');
        tab.title = cfg.name + (cfg.visible ? '' : '（已隐藏）');
        const dot = document.createElement('span');
        dot.className = 'levels-tab-dot';
        dot.style.background = cfg.color;
        const label = document.createElement('span');
        label.className = 'levels-tab-label';
        label.textContent = shortCode(cfg.name);
        tab.appendChild(dot);
        tab.appendChild(label);
        tab.onclick = () => setActiveChannel(i);
        tabs.appendChild(tab);
    });
}

function renderLevelsSliders() {
    const box = document.getElementById('levels-sliders');
    if (!box) return;
    const i = APP.activeChannel || 0;
    const cfg = APP.configs[i];
    if (!cfg) return;
    const black0 = cfg.black ?? 0;
    const white0 = cfg.white ?? 255;
    const gamma0 = cfg.gamma ?? 1.0;

    const mkRow = (label, cls, min, max, step, val, fmt) => {
        const row = document.createElement('div');
        row.className = 'level-row';
        const lab = document.createElement('span');
        lab.className = 'level-label';
        lab.textContent = label;
        const sl = document.createElement('input');
        sl.type = 'range'; sl.className = cls;
        sl.min = min; sl.max = max; sl.step = step; sl.value = val;
        const num = document.createElement('span');
        num.className = 'level-val';
        num.textContent = fmt(parseFloat(val));
        sl.addEventListener('input', () => { num.textContent = fmt(parseFloat(sl.value)); });
        row.appendChild(lab); row.appendChild(sl); row.appendChild(num);
        return { row, sl, num };
    };

    box.innerHTML = '';
    const bk = mkRow('黑点', 'ch-black', 0, 255, 1, black0, v => String(Math.round(v)));
    bk.sl.addEventListener('input', () => {
        let v = parseInt(bk.sl.value);
        if (v >= parseInt(wh.sl.value)) { v = parseInt(wh.sl.value) - 1; bk.sl.value = v; }
        APP.configs[i].black = v;
        drawLevelsHistogram();
        debouncedUpdateParam(i, 'black', v);
    });

    const wh = mkRow('白点', 'ch-white', 0, 255, 1, white0, v => String(Math.round(v)));
    wh.sl.addEventListener('input', () => {
        let v = parseInt(wh.sl.value);
        if (v <= parseInt(bk.sl.value)) { v = parseInt(bk.sl.value) + 1; wh.sl.value = v; }
        APP.configs[i].white = v;
        drawLevelsHistogram();
        debouncedUpdateParam(i, 'white', v);
    });

    const gm = mkRow('γ', 'ch-gamma', 0.1, 5, 0.1, gamma0, v => v.toFixed(1));
    gm.sl.addEventListener('input', () => {
        const v = parseFloat(gm.sl.value);
        APP.configs[i].gamma = v;
        debouncedUpdateParam(i, 'gamma', v);
    });

    box.appendChild(bk.row);
    box.appendChild(wh.row);
    box.appendChild(gm.row);
}

function drawLevelsHistogram() {
    const canvas = document.getElementById('levels-histogram');
    if (!canvas) return;
    const i = APP.activeChannel || 0;
    const hist = APP.histograms && APP.histograms[i];
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 280;
    const cssH = 90;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    ctx.fillStyle = '#f4f6fb';
    ctx.fillRect(0, 0, cssW, cssH);

    if (!hist || !hist.length) {
        ctx.fillStyle = '#9aa3b2';
        ctx.font = '11px sans-serif';
        ctx.fillText('无数据', 8, 16);
        return;
    }
    const maxV = Math.max(1, ...hist);
    const n = hist.length;
    const bw = cssW / n;
    const cfg = APP.configs[i];
    ctx.fillStyle = cfg ? cfg.color : '#4b7bec';
    for (let x = 0; x < n; x++) {
        const h = (hist[x] / maxV) * (cssH - 2);
        ctx.fillRect(x * bw, cssH - h, Math.max(1, bw - 0.3), h);
    }

    const black = (APP.configs[i] && APP.configs[i].black) || 0;
    const white = (APP.configs[i] && APP.configs[i].white) || 255;
    const drawMarker = (val, color) => {
        const x = (val / 255) * cssW;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(x, 0); ctx.lineTo(x, cssH); ctx.stroke();
    };
    drawMarker(black, '#222');
    drawMarker(white, '#e74c3c');

    ctx.fillStyle = '#222';
    ctx.font = '10px sans-serif';
    ctx.fillText(String(Math.round(black)), Math.min(cssW - 22, (black / 255) * cssW + 2), 10);
    ctx.fillStyle = '#e74c3c';
    ctx.fillText(String(Math.round(white)), Math.max(2, (white / 255) * cssW - 20), 10);
}

function resetActiveLevels() {
    const i = APP.activeChannel || 0;
    if (!APP.configs[i]) return;
    APP.configs[i].black = 0;
    APP.configs[i].white = 255;
    APP.configs[i].gamma = 1.0;
    renderLevelsSliders();
    drawLevelsHistogram();
    updateChannelParam(i, 'black', 0);
    updateChannelParam(i, 'white', 255);
    updateChannelParam(i, 'gamma', 1.0);
}

// ======== 空通道(未启用通道)自动忽略提示 ========
// 后端按「无有效色阶波动」自动忽略未启用的通道, 这里给出可见提示 + 一键恢复入口,
// 保证「识别不好可以随时修改」。
function hideEmptyChannelsHint() {
    const el = document.getElementById('empty-ch-hint');
    if (el) el.hidden = true;
}

function showEmptyChannelsHint(count, details) {
    const el = document.getElementById('empty-ch-hint');
    const txt = document.getElementById('empty-ch-hint-text');
    if (!el || !txt) return;
    if (!count) { el.hidden = true; return; }
    const names = (details || []).map(d => d && d.name).filter(Boolean).join('、');
    txt.textContent = names
        ? `已自动忽略 ${count} 个无信号通道（${names}）—— 该通道未启用或全黑`
        : `已自动忽略 ${count} 个无信号通道（未启用或全黑）`;
    txt.title = (details || []).map(d => `${d.name}: ${d.reason || ''}`).join('\n');
    el.hidden = false;
}

async function restoreEmptyChannels() {
    if (!APP.sessionId) return;
    showLoading('正在恢复被忽略的通道...');
    try {
        const resp = await fetch('/api/restore_empty_channels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: APP.sessionId })
        });
        const data = await resp.json();
        hideLoading();
        if (data.error) { showError('恢复失败', data.error); return; }
        APP.channels = data.channel_previews;
        APP.configs = data.configs;
        APP.activeCount = data.active_count != null ? data.active_count : data.configs.length;
        APP.histograms = data.histograms || [];
        APP._lastLevels = APP.configs.map(c => ({
            black: c.black ?? 0, white: c.white ?? 255, gamma: c.gamma ?? 1.0
        }));
        APP.mergedPreview = data.merged_preview;
        hideEmptyChannelsHint();
        refreshChCountSelect();
        renderChannelList();
        renderMainCanvas();
        renderLevelsPanel();
        updateAnalysisUI();
        updateExportPanels();
        setStatus(`已恢复 ${data.restored} 个通道，当前共 ${APP.activeCount} 通道`);
    } catch (err) {
        hideLoading();
        showError('网络错误', err.message);
    }
}

// ======== 通道数徽章 (标题内 select) 同步 ========
function refreshChCountSelect() {
    const sel = document.getElementById('ch-count-select');
    if (!sel) return;
    const full = (APP.configs && APP.configs.length) || 0;
    // 免疫荧光固定 4 个通道槽位 (405/488/594/640), 上限即 4
    const max = Math.min(4, Math.max(4, full));
    const desired = APP.activeCount || APP.intendedChannelCount || 3;
    // 仅在选项数变化时重建 (避免重渲染抢焦点)
    if (sel.options.length !== max) {
        sel.innerHTML = '';
        for (let i = 1; i <= max; i++) {
            const opt = document.createElement('option');
            opt.value = String(i);
            opt.textContent = String(i);
            sel.appendChild(opt);
        }
    }
    sel.value = String(Math.min(Math.max(1, desired), max));
}

function arrayMove(arr, from, to) {
    const a = arr.slice();
    const [item] = a.splice(from, 1);
    a.splice(to, 0, item);
    return a;
}

async function reorderChannels(from, to) {
    if (!APP.sessionId) return;
    const n = APP.configs.length;
    if (from < 0 || to < 0 || from >= n || to >= n) return;
    const newOrder = arrayMove([...Array(n).keys()], from, to);
    showLoading('正在调整通道顺序...');
    try {
        const resp = await fetch('/api/reorder_channels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: APP.sessionId, order: newOrder })
        });
        if (resp.ok) {
            APP.configs = newOrder.map(idx => APP.configs[idx]);
            APP.histograms = newOrder.map(idx => (APP.histograms || [])[idx]);
            APP.activeChannel = 0;
            if (APP.currentView === 'single') renderMainCanvas();
            renderChannelList();
            renderLevelsPanel();
            updateExportPanels();
            setStatus('通道顺序已更新');
        } else {
            const d = await resp.json().catch(() => ({}));
            showError('重排失败', d.error || `HTTP ${resp.status}`);
        }
    } catch (err) {
        showError('网络错误', err.message);
    }
    hideLoading();
}

function setActiveChannel(idx) {
    APP.activeChannel = idx;
    if (APP.currentView === 'single') renderMainCanvas();
    renderChannelList();
    renderLevelsPanel();
}

function toggleChannel(idx) {
    APP.configs[idx].visible = !APP.configs[idx].visible;
    updateMergeView(idx);
    renderChannelList();
    renderLevelsPanel();
}

async function syncChannelCount() {
    const sel = document.getElementById('ch-count-select');
    if (!sel) return;
    const n = parseInt(sel.value) || 0;
    // 未导入图像: 仅记录用户意图 (供文件夹导入使用), 不发请求
    if (!APP.sessionId || !APP.configs || !APP.configs.length) {
        APP.intendedChannelCount = n;
        return;
    }
    if (n < 1 || n === (APP.activeCount || APP.configs.length)) return;
    const full = APP.configs.length;
    if (n > full) { refreshChCountSelect(); return; }  // 不能超过实际导入通道数
    showLoading('正在切换通道数...');
    try {
        const resp = await fetch('/api/set_channel_count', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: APP.sessionId, count: n })
        });
        const data = await resp.json();
        if (data.error) { showError('切换失败', data.error); sel.value = String(APP.activeCount); return; }
        APP.activeCount = data.active_count;
        APP.mergedPreview = data.merged_preview;
        APP.histograms = data.histograms || (APP.histograms || []).slice(0, APP.activeCount);
        if (APP.activeChannel >= APP.activeCount) APP.activeChannel = 0;
        refreshChCountSelect();
        renderChannelList();
        renderMainCanvas();
        renderLevelsPanel();
        updateAnalysisUI();
        updateExportPanels();
    } catch (err) {
        showError('网络错误', err.message);
    } finally {
        hideLoading();
    }
}

// ======== 通道颜色点选 (自动识别不可靠时弹出, 也可手动打开) ========
const COLOR_PRESETS = [
    { name: 'DAPI', hex: '#0000FF' },
    { name: 'GFP', hex: '#00FF00' },
    { name: 'RFP', hex: '#FF0000' },
    { name: 'Cy5', hex: '#FF00FF' },
    { name: 'CFP', hex: '#00FFFF' },
    { name: 'YFP', hex: '#FFFF00' },
    { name: 'BF', hex: '#FFFFFF' },
];
function showChannelColorPicker() {
    if (!APP.configs || !APP.configs.length) return;
    const list = document.getElementById('color-picker-list');
    const modal = document.getElementById('channel-color-modal');
    if (!list || !modal) return;
    const ac = APP.activeCount || APP.configs.length;
    let html = '';
    APP.configs.slice(0, ac).forEach((c, i) => {
        const cur = (c.color || '#FFFFFF').toUpperCase();
        const safeName = (c.name || `Ch${i+1}`).replace(/"/g, '&quot;');
        let swatches = '';
        COLOR_PRESETS.forEach(p => {
            const active = p.hex.toUpperCase() === cur ? ' active' : '';
            swatches += `<button class="cc-swatch${active}" style="background:${p.hex}" title="${p.name}" onclick="pickChannelColor(${i},'${p.hex}')"></button>`;
        });
        html += `<div class="cc-row">
            <input type="text" class="cc-name-input" value="${safeName}" maxlength="16"
                onkeydown="if(event.key==='Enter'){event.target.blur();}"
                onchange="renameChannel(${i}, this.value)" title="直接修改通道名">
            <div class="cc-swatches">${swatches}</div>
            <input type="color" class="cc-custom" value="${cur}" title="自定义颜色" onchange="pickChannelColor(${i}, this.value)">
            <span class="cc-tag">${cur}</span>
        </div>`;
    });
    list.innerHTML = html;
    modal.style.display = 'flex';
}
function closeChannelColorPicker() {
    const modal = document.getElementById('channel-color-modal');
    if (modal) modal.style.display = 'none';
}
function pickChannelColor(idx, hex) {
    if (!APP.configs || !APP.configs[idx]) return;
    hex = (hex || '').trim();
    if (!/^#[0-9a-fA-F]{6}$/.test(hex)) return;
    changeChannelColor(idx, hex);
    // 刷新弹窗内高亮与色值显示
    const modal = document.getElementById('channel-color-modal');
    if (modal && modal.style.display !== 'none') showChannelColorPicker();
}

function changeChannelColor(idx, hexColor) {
    APP.configs[idx].color = hexColor;
    updateMergeView(idx);
    renderChannelList();
    renderLevelsTabs();
}

function renameChannel(idx, newName) {
    if (!APP.configs || !APP.configs[idx]) return;
    const nm = (newName || '').trim().slice(0, 16);
    if (!nm) return;
    if (APP.configs[idx].name === nm) return;
    APP.configs[idx].name = nm;
    rememberChannelName(nm);
    renderChannelList();
    renderLevelsTabs();
    updateAnalysisUI();        // Line Profile / Pearson 选项中的通道名
    updateExportPanels();      // 排版预览里的标签
}

async function updateChannelName(idx, name) {
    APP.configs[idx].name = name;
    updateAnalysisUI();
    await updateMergeView(idx);
}

// ======== 本地 canvas 实时预览 (零延迟) ========
// 在色阶/亮度滑块 input 时立即重画 (不等后端往返);
// 适用: 单通道视图 + 当前活动通道 = 正在调整的通道
// 原理: 用上次后端返回的 channels[idx] base64 解码, 像素级 delta 反向再正向变换 (oldLevels → newLevels)
function localPreviewActiveChannel(idx) {
    if (APP.currentView !== 'single') return;
    if (idx !== APP.activeChannel) return;
    if (!APP._lastLevels || !APP._lastLevels[idx]) return;
    if (!APP.channels[idx]) return;

    const oldL = APP._lastLevels[idx];
    const cfg = APP.configs[idx];
    const newBlack = cfg.black ?? 0;
    const newWhite = cfg.white ?? 255;
    const newGamma = cfg.gamma ?? 1.0;

    // 无变化跳过
    if (oldL.black === newBlack && oldL.white === newWhite && oldL.gamma === newGamma) return;

    const imgSrc = APP.channels[idx];
    const img = new Image();
    img.onload = () => {
        try {
            const off = document.createElement('canvas');
            off.width = img.width;
            off.height = img.height;
            const offCtx = off.getContext('2d');
            offCtx.drawImage(img, 0, 0);

            const id = offCtx.getImageData(0, 0, off.width, off.height);
            const d = id.data;
            const rangeOld = Math.max(1, oldL.white - oldL.black);
            const rangeNew = Math.max(1, newWhite - newBlack);

            for (let i = 0; i < d.length; i += 4) {
                for (let c = 0; c < 3; c++) {
                    let v = d[i + c] / 255;
                    // 反向旧变换
                    if (oldL.gamma !== 1.0) v = Math.pow(v, oldL.gamma);
                    v = v * rangeOld + oldL.black;
                    // 正向新变换
                    v = (v - newBlack) / rangeNew;
                    if (v < 0) v = 0; else if (v > 1) v = 1;
                    if (newGamma !== 1.0) v = Math.pow(v, 1.0 / newGamma);
                    d[i + c] = Math.round(v * 255);
                }
            }

            offCtx.putImageData(id, 0, 0);

            // 绘制到主 canvas (仅图像, 不叠加 ROI/Inset/线等, 后端响应后 renderMainCanvas 会重画完整画面)
            const contW = container.clientWidth;
            const contH = container.clientHeight;
            const displayW = APP.metadata.width * APP.zoom;
            const displayH = APP.metadata.height * APP.zoom;
            canvas.width = contW;
            canvas.height = contH;
            ctx.fillStyle = '#1a1a2e';
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            const ox = (contW - displayW) / 2 + APP.canvasOffset.x;
            const oy = (contH - displayH) / 2 + APP.canvasOffset.y;
            // 与主渲染保持一致: 色阶变换在灰度上做完后再着色, 避免拖动时闪回灰色
            const paint = _paintRgbFor(idx);
            ctx.drawImage(paint ? _colorizeImg(off, paint) : off, ox, oy, displayW, displayH);
        } catch (e) {
            // 跨域污染或不支持 getImageData, 静默回退到后端预览
        }
    };
    img.onerror = () => {};
    img.src = 'data:image/png;base64,' + imgSrc;
}

async function updateChannelParam(idx, param, value) {
    APP.configs[idx][param] = value;
    // 本地 canvas 立即重画 (零延迟反馈, 仅在单通道视图且调整当前通道时有效)
    localPreviewActiveChannel(idx);
    await updateMergeView(idx);
}

async function updateMergeView(chIdx) {
    if (!APP.sessionId) return;
    if (chIdx === undefined) chIdx = APP.activeChannel;
    try {
        const resp = await fetch('/api/update_channel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: APP.sessionId,
                channel_index: chIdx,
                config: {
                    color: APP.configs[chIdx].color,
                    black: APP.configs[chIdx].black,
                    white: APP.configs[chIdx].white,
                    gamma: APP.configs[chIdx].gamma,
                    brightness: APP.configs[chIdx].brightness,
                    contrast: APP.configs[chIdx].contrast,
                    visible: APP.configs[chIdx].visible,
                    name: APP.configs[chIdx].name
                }
            })
        });
        const data = await resp.json();
        if (data.error) {
            showError('更新失败', data.error);
            return;
        }
        APP.mergedPreview = data.merged_preview;
        APP.channels[chIdx] = data.channel_preview;
        // 同步"上次后端色阶"以便后续本地预览做正确的 delta 变换
        if (!APP._lastLevels) APP._lastLevels = [];
        APP._lastLevels[chIdx] = {
            black: APP.configs[chIdx].black ?? 0,
            white: APP.configs[chIdx].white ?? 255,
            gamma: APP.configs[chIdx].gamma ?? 1.0
        };
        if (APP.scaleBarApplied) await applyScaleBar();
        else renderMainCanvas();
    } catch (err) {
        showError('网络错误', err.message);
    }
}

// ======== 画布渲染 ========
function renderMainCanvas() {
    if (!APP.sessionId) return;
    document.getElementById('empty-canvas').style.display = 'none';

    // 网格视图: 把所有活动通道平铺展示 (多通道预览)
    if (APP.currentView === 'grid') {
        renderCanvasGrid();
        return;
    }

    // merge 视图: 后端已合成彩色, 不再着色; 单通道视图: 灰度源图按显示模式着色
    if (APP.currentView === 'merge') {
        if (!APP.mergedPreview) return;
        _renderCanvasSingleImg(APP.mergedPreview);
        return;
    }
    const imgSrc = APP.channels[APP.activeChannel];
    if (!imgSrc) return;
    _renderCanvasSingleImg(imgSrc, _paintRgbFor(APP.activeChannel));
}

/**
 * 把所有活动通道平铺在 canvas 上, 每格一个通道 (支持 1-N 个通道)
 * - 自动选取网格列数 (按通道数和画布宽高比)
 * - 每格显示该通道原始灰度提亮后涂上该通道颜色
 * - 左下角标通道名 + 色彩徽
 * - 点击任意一格 → 切回单通道视图并选中该通道
 */
function renderCanvasGrid() {
    const ac = APP.activeCount || (APP.configs ? APP.configs.length : 0);
    if (!ac || !APP.channels || APP.channels.length < ac) return;

    const contW = container.clientWidth;
    const contH = container.clientHeight;

    // 按容器与原图比例选定列数: 2/3/4
    const imgRatio = APP.metadata.width / APP.metadata.height;  // w/h
    let cols = Math.min(ac, Math.max(1, Math.round(Math.sqrt(ac * imgRatio * (contW / contH)))));
    if (cols > 4) cols = 4;
    if (cols < 1) cols = 1;
    const rows = Math.ceil(ac / cols);

    // 每格间距 + 内边距 (画通道名)
    const gap = Math.max(8, Math.min(contW, contH) * 0.012);
    const pad = Math.max(6, Math.min(contW, contH) * 0.008);
    const labelH = Math.max(18, Math.min(contW, contH) * 0.035);  // 底部标签条
    const cellW = (contW - gap * (cols - 1) - pad * 2) / cols;
    const cellH = (contH - gap * (rows - 1) - pad * 2) / rows;
    const innerW = cellW;
    const innerH = cellH - labelH;

    canvas.width = contW;
    canvas.height = contH;
    ctx.fillStyle = '#1a1a1e';
    ctx.fillRect(0, 0, contW, contH);

    APP._gridCells = [];

    const loadTasks = [];
    for (let i = 0; i < ac; i++) {
        const src = APP.channels[i];
        if (!src) continue;
        const task = new Promise(resolve => {
            const im = new Image();
            im.onload = () => resolve({ i, im });
            im.onerror = () => resolve({ i, im: null });
            im.src = 'data:image/png;base64,' + src;
        });
        loadTasks.push(task);
    }

    Promise.all(loadTasks).then(results => {
        results.forEach(({ i, im }) => {
            drawCell(i, im);
        });
        updateZoomDisplay();
    });

    function drawCell(i, img) {
        const r = Math.floor(i / cols), c = i % cols;
        const x = pad + c * (cellW + gap);
        const y = pad + r * (cellH + gap);

        ctx.fillStyle = '#11121f';
        ctx.fillRect(x, y, cellW, cellH);

        if (img) {
            // 灰度源图按显示模式着色为该通道伪彩 (彩色模式) 或原样保留 (灰度模式)
            const paint = _paintRgbFor(i);
            const src = paint ? _colorizeImg(img, paint) : img;
            const ratio = Math.min(innerW / img.width, innerH / img.height);
            const dw = img.width * ratio, dh = img.height * ratio;
            const dx = x + (innerW - dw) / 2;
            const dy = y + (innerH - dh) / 2;
            ctx.drawImage(src, dx, dy, dw, dh);
        }

        const cfg = APP.configs[i] || {};
        const hex = (cfg.color || '#888888').replace('#', '');
        const col = [parseInt(hex.substr(0,2),16), parseInt(hex.substr(2,2),16), parseInt(hex.substr(4,2),16)];
        const visible = cfg.visible !== false;
        ctx.strokeStyle = visible ? `rgba(${col[0]},${col[1]},${col[2]},0.85)` : 'rgba(255,255,255,0.12)';
        ctx.lineWidth = visible ? 1.5 : 1;
        ctx.strokeRect(x + 0.5, y + 0.5, cellW - 1, cellH - 1);

        const labelY = y + innerH;
        ctx.fillStyle = 'rgba(0,0,0,0.55)';
        ctx.fillRect(x, labelY, cellW, labelH);

        ctx.fillStyle = visible ? `rgb(${col[0]},${col[1]},${col[2]})` : 'rgba(255,255,255,0.3)';
        ctx.beginPath();
        ctx.arc(x + 11, labelY + labelH/2, 4.5, 0, 2*Math.PI);
        ctx.fill();

        ctx.fillStyle = visible ? '#fff' : 'rgba(255,255,255,0.4)';
        ctx.font = 'bold 11px Arial';
        const name = (cfg.name || `Ch${i+1}`).slice(0, 14);
        ctx.textBaseline = 'middle';
        ctx.fillText(name, x + 22, labelY + labelH/2);

        APP._gridCells.push({
            x, y, w: cellW, h: cellH,
            idx: i,
            name: cfg.name || `Ch${i+1}`
        });
    }
}

// 单图 (合并 / 单通道) 渲染的公共部分
// paintRgb: 传入 [r,g,b] 则把灰度源图着色为该通道伪彩; 传 null/undefined 则原样绘制
function _renderCanvasSingleImg(imgSrc, paintRgb) {

    const img = new Image();
    img.onload = () => {
        const src = paintRgb ? _colorizeImg(img, paintRgb) : img;
        APP.insetSourceImg = src;  // 缓存供 inset 预览使用 (与主画面同色, 保证放大图一致)
        const contW = container.clientWidth;
        const contH = container.clientHeight;
        const displayW = APP.metadata.width * APP.zoom;
        const displayH = APP.metadata.height * APP.zoom;

        canvas.width = contW;
        canvas.height = contH;
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        const ox = (contW - displayW) / 2 + APP.canvasOffset.x;
        const oy = (contH - displayH) / 2 + APP.canvasOffset.y;
        ctx.drawImage(src, ox, oy, displayW, displayH);

        // ROI 框
        if (APP.roi) {
            const rx = ox + APP.roi.x * APP.zoom;
            const ry = oy + APP.roi.y * APP.zoom;
            const rw = APP.roi.width * APP.zoom;
            const rh = APP.roi.height * APP.zoom;
            const roiCol = getROIColor();
            ctx.strokeStyle = roiCol;
            ctx.lineWidth = 2;
            ctx.setLineDash([5, 3]);
            ctx.strokeRect(rx, ry, rw, rh);
            ctx.setLineDash([]);
            ctx.globalAlpha = 0.1;
            ctx.fillStyle = roiCol;
            ctx.fillRect(rx, ry, rw, rh);
            ctx.globalAlpha = 1;

            // Inset 模式额外高亮
            if (APP.tool === 'inset') {
                ctx.strokeStyle = '#3498db';
                ctx.lineWidth = 3;
                ctx.setLineDash([8, 4]);
                ctx.strokeRect(rx, ry, rw, rh);
                ctx.setLineDash([]);
                ctx.fillStyle = 'rgba(52,152,219,0.15)';
                ctx.fillRect(rx, ry, rw, rh);
            }

            // 同步绘制右上角放大预览 (所有视图都显示)
            drawCanvasInsetOverlay(img, ox, oy, displayW, displayH);

            // ROI 工具: 四角拖拽手柄 (用于移动/缩放)
            if (APP.tool === 'roi') {
                const hs = 5;
                ctx.fillStyle = '#f39c12';
                ctx.strokeStyle = '#ffffff';
                ctx.lineWidth = 1.5;
                [[rx, ry], [rx + rw, ry], [rx, ry + rh], [rx + rw, ry + rh]].forEach(([hx, hy]) => {
                    ctx.fillRect(hx - hs, hy - hs, hs * 2, hs * 2);
                    ctx.strokeRect(hx - hs, hy - hs, hs * 2, hs * 2);
                });
            }
        }

        // Line
        if (APP.linePoints.length > 0) {
            ctx.strokeStyle = '#ffffff';
            ctx.lineWidth = 1.5;
            ctx.setLineDash([]);
            ctx.beginPath();
            APP.linePoints.forEach((p, i) => {
                const px = ox + p.x * APP.zoom;
                const py = oy + p.y * APP.zoom;
                if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
            });
            ctx.stroke();

            APP.linePoints.forEach((p, i) => {
                const px = ox + p.x * APP.zoom;
                const py = oy + p.y * APP.zoom;
                ctx.fillStyle = '#ffffff';
                ctx.beginPath();
                ctx.arc(px, py, 4, 0, 2 * Math.PI);
                ctx.fill();
                ctx.fillStyle = '#f39c12';
                ctx.font = '10px Arial';
                ctx.fillText(i === 0 ? 'P1' : 'P2', px + 6, py - 6);
            });
        }

        updateZoomDisplay();
        updateInsetPreview();  // 同步更新右侧边栏预览
    };
    img.src = 'data:image/png;base64,' + imgSrc;
}

function drawCanvasInsetOverlay(sourceImg, ox, oy, displayW, displayH) {
    const cfg = getInsetConfig();
    if (!cfg.enabled || !APP.roi) return;

    // 计算 ROI 在 canvas 上的显示区域
    const rx = APP.roi.x;
    const ry = APP.roi.y;
    const rw = APP.roi.width;
    const rh = APP.roi.height;
    if (rw <= 0 || rh <= 0) return;

    // 显示区域 (按 zoom 放大)
    const insetW = rw * cfg.zoom * APP.zoom;
    const insetH = rh * cfg.zoom * APP.zoom;
    if (insetW < 5 || insetH < 5) return;

    // 放置位置: 右上角
    const margin = Math.max(8, Math.min(displayW, displayH) * 0.015);
    let px, py;
    if (cfg.position === 'top-right') {
        px = ox + displayW - insetW - margin;
        py = oy + margin;
    } else if (cfg.position === 'top-left') {
        px = ox + margin;
        py = oy + margin;
    } else if (cfg.position === 'bottom-right') {
        px = ox + displayW - insetW - margin;
        py = oy + displayH - insetH - margin;
    } else {
        px = ox + margin;
        py = oy + displayH - insetH - margin;
    }
    px = Math.max(ox, Math.min(px, ox + displayW - insetW));
    py = Math.max(oy, Math.min(py, oy + displayH - insetH));

    // 绘制白色背景底 + 阴影
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,0.5)';
    ctx.shadowBlur = 6;
    ctx.shadowOffsetX = 2;
    ctx.shadowOffsetY = 2;
    ctx.fillStyle = 'rgba(0,0,0,0.6)';
    ctx.fillRect(px - 2, py - 2, insetW + 4, insetH + 4);
    ctx.restore();

    // 裁剪 ROI 并放大绘制
    ctx.save();
    ctx.beginPath();
    ctx.rect(px, py, insetW, insetH);
    ctx.clip();
    ctx.drawImage(sourceImg,
        rx, ry, rw, rh,
        px, py, insetW, insetH
    );
    ctx.restore();

    // 白色边框
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2;
    ctx.strokeRect(px, py, insetW, insetH);
    // 内黑边
    ctx.strokeStyle = 'rgba(0,0,0,0.5)';
    ctx.lineWidth = 1;
    ctx.strokeRect(px + 1, py + 1, insetW - 2, insetH - 2);
}

function updateInsetPreview() {
    const canvas = document.getElementById('inset-preview-canvas');
    if (!canvas) return;
    const c = canvas.getContext('2d');
    const cfg = getInsetConfig();
    if (!cfg.enabled || !APP.roi || !APP.insetSourceImg) {
        c.fillStyle = '#1a1a2e';
        c.fillRect(0, 0, canvas.width, canvas.height);
        c.fillStyle = '#7f8c8d';
        c.font = '12px Arial';
        c.textAlign = 'center';
        c.fillText('未启用 / 无 ROI', canvas.width / 2, canvas.height / 2);
        return;
    }
    const rx = Math.max(0, APP.roi.x);
    const ry = Math.max(0, APP.roi.y);
    const rw = Math.min(APP.roi.width, APP.metadata.width - rx);
    const rh = Math.min(APP.roi.height, APP.metadata.height - ry);
    if (rw <= 0 || rh <= 0) return;

    // 保持宽高比填充 canvas
    const ratio = Math.min(canvas.width / (rw * cfg.zoom), canvas.height / (rh * cfg.zoom));
    const dw = rw * cfg.zoom * ratio;
    const dh = rh * cfg.zoom * ratio;
    const dx = (canvas.width - dw) / 2;
    const dy = (canvas.height - dh) / 2;

    c.fillStyle = '#1a1a2e';
    c.fillRect(0, 0, canvas.width, canvas.height);
    c.drawImage(APP.insetSourceImg, rx, ry, rw, rh, dx, dy, dw, dh);
    c.strokeStyle = '#ffffff';
    c.lineWidth = 2;
    c.strokeRect(dx, dy, dw, dh);
}

function updateZoomDisplay() {
    document.getElementById('zoom-level').textContent = Math.round(APP.zoom * 100) + '%';
}

// ======== 视图/工具 ========
function setViewMode(mode) {
    APP.currentView = mode;
    document.getElementById('view-merge').className = 'btn btn-sm' + (mode === 'merge' ? ' btn-active' : '');
    document.getElementById('view-single').className = 'btn btn-sm' + (mode === 'single' ? ' btn-active' : '');
    document.getElementById('view-grid').className = 'btn btn-sm' + (mode === 'grid' ? ' btn-active' : '');
    renderMainCanvas();
}

function setTool(tool) {
    APP.tool = tool;
    document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('tool-' + tool).classList.add('active');
    canvas.style.cursor = tool === 'pan' ? 'grab' : 'crosshair';
    renderMainCanvas();
}

function zoomIn() { APP.zoom = Math.min(APP.zoom * 1.3, 10); renderMainCanvas(); }
function zoomOut() { APP.zoom = Math.max(APP.zoom / 1.3, 0.1); renderMainCanvas(); }
function zoomFit() {
    if (!APP.metadata.width) return;
    const contW = container.clientWidth;
    const contH = container.clientHeight;
    APP.zoom = Math.min(contW / APP.metadata.width, contH / APP.metadata.height) * 0.9;
    APP.canvasOffset = { x: 0, y: 0 };
    renderMainCanvas();
}

// ROI 精确尺寸控制
function toggleFixedROI() {
    if (document.getElementById('roi-fixed').checked) setTool('roi');
}

// 当前 ROI 框线颜色 (取色器), 缺省 #ffffff (白色), 与后端 _draw_roi_boundary / SVG 的默认色保持一致
function getROIColor() {
    const el = document.getElementById('roi-color');
    return (el && el.value) ? el.value : '#ffffff';
}

function centerROI() {
    if (!APP.sessionId || !APP.metadata.width || !APP.roi) return;
    const w = APP.roi.width, h = APP.roi.height;
    APP.roi = clampROI({
        x: Math.round((APP.metadata.width - w) / 2),
        y: Math.round((APP.metadata.height - h) / 2),
        width: w, height: h
    });
    renderMainCanvas();
    updateROIInfo();
    updateExportPanels();
    setStatus('ROI 已置于图像中心');
}

// 按 X/Y/W/H 输入框直接设定当前 ROI 尺寸与位置 (新建或精调均可用)
function applyROIDims() {
    if (!APP.sessionId || !APP.metadata.width) return;
    const x = Math.max(0, parseInt(document.getElementById('roi-x').value) || 0);
    const y = Math.max(0, parseInt(document.getElementById('roi-y').value) || 0);
    const w = Math.max(1, parseInt(document.getElementById('roi-w').value) || 1);
    const h = Math.max(1, parseInt(document.getElementById('roi-h').value) || 1);
    APP.roi = clampROI({ x, y, width: w, height: h });
    renderMainCanvas();
    updateROIInfo();
    updateExportPanels();
}

// ======== 鼠标交互 ========
function clampROI(roi) {
    const w = APP.metadata.width, h = APP.metadata.height;
    roi.x = Math.max(0, Math.min(roi.x, w - roi.width));
    roi.y = Math.max(0, Math.min(roi.y, h - roi.height));
    return roi;
}

// 计算主画布的显示布局 (图像居中 + 缩放 + 偏移)
function getCanvasLayout() {
    const contW = container.clientWidth;
    const contH = container.clientHeight;
    const dispW = APP.metadata.width * APP.zoom;
    const dispH = APP.metadata.height * APP.zoom;
    const ox = (contW - dispW) / 2 + APP.canvasOffset.x;
    const oy = (contH - dispH) / 2 + APP.canvasOffset.y;
    return { contW, contH, dispW, dispH, ox, oy };
}

// 命中 ROI 四角手柄检测, 返回 'tl'|'tr'|'bl'|'br'|null
function hitROICorner(mx, my, rx, ry, rw, rh) {
    const H = 8;
    const corners = { tl: [rx, ry], tr: [rx + rw, ry], bl: [rx, ry + rh], br: [rx + rw, ry + rh] };
    for (const [name, [hx, hy]] of Object.entries(corners)) {
        if (Math.abs(mx - hx) <= H && Math.abs(my - hy) <= H) return name;
    }
    return null;
}

// ROI 工具悬停时光标反馈
function updateROICursor(mx, my) {
    if (!APP.sessionId || APP.tool !== 'roi' || !APP.roi) { canvas.style.cursor = 'crosshair'; return; }
    const { ox, oy } = getCanvasLayout();
    const rx = ox + APP.roi.x * APP.zoom;
    const ry = oy + APP.roi.y * APP.zoom;
    const rw = APP.roi.width * APP.zoom;
    const rh = APP.roi.height * APP.zoom;
    const handle = hitROICorner(mx, my, rx, ry, rw, rh);
    if (handle) {
        canvas.style.cursor = (handle === 'tl' || handle === 'br') ? 'nwse-resize' : 'nesw-resize';
    } else if (mx > rx && mx < rx + rw && my > ry && my < ry + rh) {
        canvas.style.cursor = 'move';
    } else {
        canvas.style.cursor = 'crosshair';
    }
}

canvas.addEventListener('mousedown', (e) => {
    if (!APP.sessionId) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    // 网格视图: 点击格子直接切到该通道的单通道视图
    if (APP.currentView === 'grid' && APP._gridCells && APP._gridCells.length) {
        const hit = APP._gridCells.find(g => mx >= g.x && mx <= g.x + g.w && my >= g.y && my <= g.y + g.h);
        if (hit) {
            setActiveChannel(hit.idx);
            setViewMode('single');
            return;
        }
    }

    if (APP.tool === 'line') {
        const imgCoord = canvasToImage(mx, my);
        if (imgCoord) {
            if (APP.linePoints.length < 2) {
                APP.linePoints.push(imgCoord);
                if (APP.linePoints.length === 2) {
                    renderMainCanvas();
                    updateLineInfo();
                    requestLineProfile();
                } else {
                    renderMainCanvas();
                    updateLineInfo();
                }
            } else {
                APP.linePoints = [imgCoord];
                renderMainCanvas();
                updateLineInfo();
            }
        }
        return;
    }

    if (APP.tool === 'inset') {
        const imgCoord = canvasToImage(mx, my);
        if (imgCoord) {
            const cfg = getInsetConfig();
            APP.roi = clampROI({
                x: Math.round(imgCoord.x - cfg.width / 2),
                y: Math.round(imgCoord.y - cfg.height / 2),
                width: cfg.width,
                height: cfg.height
            });
            APP.isDragging = true;
            APP.dragStart = { canvasX: mx, canvasY: my };
            renderMainCanvas();
            updateROIInfo();
            updateExportPanels();
        }
        return;
    }

    if (APP.tool === 'roi') {
        // 固定尺寸模式: 点击即以固定宽高放置 ROI (中心对齐点击点)
        const fixedChk = document.getElementById('roi-fixed');
        if (fixedChk && fixedChk.checked) {
            const w = Math.max(1, parseInt(document.getElementById('roi-w').value) || 100);
            const h = Math.max(1, parseInt(document.getElementById('roi-h').value) || 100);
            const imgCoord = canvasToImage(mx, my);
            if (imgCoord) {
                APP.roi = clampROI({
                    x: Math.round(imgCoord.x - w / 2),
                    y: Math.round(imgCoord.y - h / 2),
                    width: w, height: h
                });
                APP.isDragging = true;
                APP.dragStart = { canvasX: mx, canvasY: my };
                APP.roiMode = 'move';
                renderMainCanvas();
                updateROIInfo();
                updateExportPanels();
            }
            return;
        }
        // 已有选区: 命中手柄 → 缩放; 命中框内 → 移动; 否则 → 新建正方形
        if (APP.roi) {
            const layout = getCanvasLayout();
            const rx = layout.ox + APP.roi.x * APP.zoom;
            const ry = layout.oy + APP.roi.y * APP.zoom;
            const rw = APP.roi.width * APP.zoom;
            const rh = APP.roi.height * APP.zoom;
            const handle = hitROICorner(mx, my, rx, ry, rw, rh);
            const inside = mx > rx && mx < rx + rw && my > ry && my < ry + rh;
            if (handle) {
                const fx = handle === 'tl' || handle === 'bl' ? APP.roi.x + APP.roi.width : APP.roi.x;
                const fy = handle === 'tl' || handle === 'tr' ? APP.roi.y + APP.roi.height : APP.roi.y;
                APP.roiMode = 'resize-' + handle;
                APP.isDragging = true;
                APP.dragStart = { canvasX: mx, canvasY: my, fixedX: fx, fixedY: fy };
                renderMainCanvas();
                return;
            }
            if (inside) {
                APP.roiMode = 'move';
                APP.isDragging = true;
                APP.dragStart = { canvasX: mx, canvasY: my };
                renderMainCanvas();
                return;
            }
        }
        // 新建正方形 ROI
        const startImg = canvasToImage(mx, my);
        if (startImg) {
            APP.roiMode = 'create';
            APP.isDragging = true;
            APP.dragStart = { canvasX: mx, canvasY: my };
            renderMainCanvas();
        }
        return;
    }

    APP.isDragging = true;
    APP.dragStart = { canvasX: mx, canvasY: my };
});

canvas.addEventListener('mousemove', (e) => {
    if (!APP.sessionId) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const imgCoord = canvasToImage(mx, my);
    if (imgCoord) {
        document.getElementById('cursor-pos').textContent = `X: ${Math.round(imgCoord.x)} Y: ${Math.round(imgCoord.y)}`;
    }
    if (!APP.isDragging) {
        if (APP.tool === 'roi') updateROICursor(mx, my);
        return;
    }

    if (APP.tool === 'pan') {
        APP.canvasOffset.x += mx - APP.dragStart.canvasX;
        APP.canvasOffset.y += my - APP.dragStart.canvasY;
        APP.dragStart = { canvasX: mx, canvasY: my };
        renderMainCanvas();
    } else if (APP.tool === 'roi') {
        if (APP.roiMode === 'create') {
            const startImg = canvasToImage(APP.dragStart.canvasX, APP.dragStart.canvasY);
            const curImg = canvasToImage(mx, my);
            if (startImg && curImg) {
                const dx = curImg.x - startImg.x;
                const dy = curImg.y - startImg.y;
                const side = Math.max(5, Math.max(Math.abs(dx), Math.abs(dy)));
                APP.roi = clampROI({
                    x: Math.round(dx >= 0 ? startImg.x : startImg.x - side),
                    y: Math.round(dy >= 0 ? startImg.y : startImg.y - side),
                    width: Math.round(side),
                    height: Math.round(side)
                });
                renderMainCanvas();
                updateROIInfo();
            }
        } else if (APP.roiMode === 'move') {
            const startImg = canvasToImage(APP.dragStart.canvasX, APP.dragStart.canvasY);
            const curImg = canvasToImage(mx, my);
            if (startImg && curImg) {
                const dx = curImg.x - startImg.x;
                const dy = curImg.y - startImg.y;
                APP.roi = clampROI({
                    x: APP.roi.x + dx,
                    y: APP.roi.y + dy,
                    width: APP.roi.width,
                    height: APP.roi.height
                });
                APP.dragStart = { canvasX: mx, canvasY: my };
                renderMainCanvas();
                updateROIInfo();
            }
        } else if (APP.roiMode && APP.roiMode.startsWith('resize-')) {
            const curImg = canvasToImage(mx, my);
            if (curImg && APP.dragStart) {
                const fx = APP.dragStart.fixedX;
                const fy = APP.dragStart.fixedY;
                const dx = curImg.x - fx;
                const dy = curImg.y - fy;
                let side = Math.max(Math.abs(dx), Math.abs(dy));
                side = Math.max(5, Math.min(side, APP.metadata.width, APP.metadata.height));
                const nx = dx >= 0 ? fx : fx - side;
                const ny = dy >= 0 ? fy : fy - side;
                APP.roi = clampROI({
                    x: Math.round(nx),
                    y: Math.round(ny),
                    width: Math.round(side),
                    height: Math.round(side)
                });
                renderMainCanvas();
                updateROIInfo();
            }
        }
    } else if (APP.tool === 'inset' && APP.dragStart) {
        const startImg = canvasToImage(APP.dragStart.canvasX, APP.dragStart.canvasY);
        const currentImg = canvasToImage(mx, my);
        if (startImg && currentImg) {
            const cfg = getInsetConfig();
            const dx = currentImg.x - startImg.x;
            const dy = currentImg.y - startImg.y;
            APP.roi = clampROI({
                x: APP.roi.x + dx,
                y: APP.roi.y + dy,
                width: cfg.width,
                height: cfg.height
            });
            APP.dragStart = { canvasX: mx, canvasY: my };
            renderMainCanvas();
            updateROIInfo();
        }
    }
});

canvas.addEventListener('mouseup', () => { APP.isDragging = false; APP.roiMode = null; });

canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (!APP.sessionId || !APP.metadata.width) return;
    const rect = canvas.getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    const contW = container.clientWidth, contH = container.clientHeight;
    const dw = APP.metadata.width * APP.zoom, dh = APP.metadata.height * APP.zoom;
    const off = APP.canvasOffset || { x: 0, y: 0 };
    const ox = (contW - dw) / 2 + off.x;
    const oy = (contH - dh) / 2 + off.y;
    // 指针下的图像坐标, 作为缩放锚点 (缩放后该点保持在光标原位)
    const imgX = (cx - ox) / APP.zoom;
    const imgY = (cy - oy) / APP.zoom;
    const factor = (e.deltaY < 0) ? 1.3 : (1 / 1.3);
    APP.zoom = Math.max(0.1, Math.min(10, APP.zoom * factor));
    const dw2 = APP.metadata.width * APP.zoom, dh2 = APP.metadata.height * APP.zoom;
    APP.canvasOffset = {
        x: (cx - imgX * APP.zoom) - (contW - dw2) / 2,
        y: (cy - imgY * APP.zoom) - (contH - dh2) / 2
    };
    document.getElementById('zoom-level').textContent = Math.round(APP.zoom * 100) + '%';
    renderMainCanvas();
});

function canvasToImage(canvasX, canvasY) {
    if (!APP.metadata.width) return null;
    const contW = container.clientWidth;
    const contH = container.clientHeight;
    const displayW = APP.metadata.width * APP.zoom;
    const displayH = APP.metadata.height * APP.zoom;
    const ox = (contW - displayW) / 2 + APP.canvasOffset.x;
    const oy = (contH - displayH) / 2 + APP.canvasOffset.y;
    const imgX = (canvasX - ox) / APP.zoom;
    const imgY = (canvasY - oy) / APP.zoom;
    if (imgX < 0 || imgX >= APP.metadata.width || imgY < 0 || imgY >= APP.metadata.height) return null;
    return { x: imgX, y: imgY };
}

// ======== Scale Bar ========
async function applyScaleBar() {
    if (!APP.sessionId) return;
    const length = parseFloat(document.getElementById('scalebar-length').value) || 50;
    const position = document.getElementById('scalebar-position').value;
    const color = document.getElementById('scalebar-color').value;
    const thickness_pt = parseFloat(document.getElementById('scalebar-thickness').value) || 1.0;
    const fontMode = (document.querySelector('input[name="scalebar-font-mode"]:checked') || {}).value || 'auto';
    const font_size_auto = (fontMode === 'auto');
    const font_size_pt = font_size_auto ? null : (parseFloat(document.getElementById('scalebar-font-size').value) || 12);

    APP.scaleBarConfig = { length_um: length, position, color, thickness_pt, font_size_auto, font_size_pt };
    APP.scaleBarApplied = true;

    try {
        const resp = await fetch('/api/add_scale_bar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: APP.sessionId,
                bar_length_um: length,
                position,
                bar_color: color,
                thickness_pt,
                font_size_auto,
                font_size_pt,
                pixel_size_um: APP.metadata.pixel_size_um
            })
        });
        const data = await resp.json();
        if (data.error) { showError('Scale bar 失败', data.error); return; }
        APP.mergedPreview = data.preview;
        if (APP.currentView === 'merge') renderMainCanvas();
        setStatus(`Scale bar 已应用: ${length} μm @ ${position}`);
    } catch (err) {
        showError('网络错误', err.message);
    }
}

function removeScaleBar() {
    APP.scaleBarApplied = false;
    APP.scaleBarConfig = null;
    updateMergeView();
}

// ======== 字号模式 (自动 / 手动) ========
function onFontSizeModeChange() {
    const mode = (document.querySelector('input[name="scalebar-font-mode"]:checked') || {}).value || 'auto';
    const input = document.getElementById('scalebar-font-size');
    const readout = document.getElementById('scalebar-font-readout');
    if (mode === 'manual') {
        if (input) { input.style.display = ''; input.disabled = false; }
        if (readout) readout.textContent = '手动字号 (pt)';
    } else {
        if (input) { input.style.display = 'none'; input.disabled = true; }
        if (readout) readout.textContent = '自动 ≈ bar 长度 × 0.3';
    }
}

// ======== 文件名识别结果 (倍镜 + 通道) ========
function showFilenameSuggestions(meta) {
    const box = document.getElementById('scalebar-filename-suggestions');
    const content = document.getElementById('scalebar-filename-content');
    if (!box || !content) return;
    if (!meta || (!meta.magnification && (!meta.channels || !meta.channels.length))) {
        box.style.display = 'none';
        return;
    }
    let html = '';
    if (meta.magnification) html += `物镜 <b>${meta.magnification}×</b><br>`;
    if (meta.channels && meta.channels.length) {
        html += '通道: ' + meta.channels.map(c => {
            const dot = c.color ? `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${c.color};margin-right:3px;vertical-align:middle"></span>` : '';
            return `${dot}${c.name} (${c.wavelength}nm)`;
        }).join('、');
    }
    content.innerHTML = html;
    box.style.display = '';
}

// 波长 → FV3000 RGB merge 里该波长所在物理平面的显示色 (与后端 WAVELENGTH_CHANNEL_MAP 一致)。
// 用途: 把文件名解析出的通道按「波长」匹配到正确的平面, 而不是按出现顺序 —— 否则
// 488(绿)/594(红) 会被顺序错配到 R/G 平面, 造成红绿通道互换。
function wavelengthToPlaneRgb(wavelength) {
    switch (Number(wavelength)) {
        case 405: return [0, 0, 255];   // 蓝
        case 488: return [0, 255, 0];   // 绿
        case 561: return [255, 0, 0];   // 红
        case 594: return [255, 0, 0];   // 红 (594 一律红色, 与后端一致)
        case 640: return [255, 0, 255]; // 远红
        default: return null;
    }
}

function isSameRgb(a, b) {
    return Array.isArray(a) && Array.isArray(b) && a.length >= 3 && b.length >= 3 &&
        a[0] === b[0] && a[1] === b[1] && a[2] === b[2];
}

// ==== PURE-FN: planFilenameSuggestionTargets (tests/test_channel_mapping.mjs 抽取测试) ====
// 为每条文件名识别建议挑选目标通道下标, 返回与 suggestions 等长的数组 (无匹配为 -1)。
// 规则: ① 按「波长 → 物理平面显示色」匹配 configs[i].rgb; ② 匹配不到再按出现顺序取下一个未占用通道。
// 不依赖 DOM / APP, 便于单测。
function planFilenameSuggestionTargets(suggestions, configs) {
    const claimed = new Set();
    const list = Array.isArray(configs) ? configs : [];
    return (suggestions || []).map((c) => {
        let idx = -1;
        const want = wavelengthToPlaneRgb(c && c.wavelength);
        if (want) {
            idx = list.findIndex((cfg, i) => !claimed.has(i) && cfg && isSameRgb(cfg.rgb, want));
        }
        if (idx < 0) idx = list.findIndex((cfg, i) => !claimed.has(i));
        if (idx >= 0) claimed.add(idx);
        return idx;
    });
}
// ==== END PURE-FN ====

function applyFilenameSuggestions() {
    const meta = (APP.metadata && APP.metadata.filename_meta) || null;
    if (!meta) return;
    // 1) 倍数 → 回填输入框并触发换算同步
    if (meta.magnification) {
        const magEl = document.getElementById('scalebar-mag');
        const zoomEl = document.getElementById('scalebar-zoom');
        if (magEl) magEl.value = meta.magnification;
        if (zoomEl) zoomEl.value = (meta.zoom_size != null ? meta.zoom_size : 1.0);
        onMagnificationChange();
    }
    // 2) 通道名/颜色: 按「波长 → 物理平面」匹配到对应通道, 而非按出现顺序 ——
    //    否则单页 RGB merge (R→红 / G→绿 / B→蓝) 会把 488(绿)/594(红) 顺序错配到 R/G 平面,
    //    导致红绿通道互换 (2026-09-16 用户实测)。无波长信息时才退回按出现顺序。
    if (meta.channels && meta.channels.length && APP.configs) {
        const targets = planFilenameSuggestionTargets(meta.channels, APP.configs);
        meta.channels.forEach((c, i) => {
            const cfg = APP.configs[targets[i]];
            if (!cfg) return;
            if (c.name) cfg.name = c.name;
            if (c.color) cfg.color = c.color;
            if (c.rgb) cfg.rgb = c.rgb;
        });
        renderChannelList();
        renderMainCanvas();
    }
    dismissFilenameSuggestions();
    setStatus('已应用文件名识别结果');
}

function dismissFilenameSuggestions() {
    const box = document.getElementById('scalebar-filename-suggestions');
    if (box) box.style.display = 'none';
}

// ======== 图像信息 ========
function updateImageInfo() {
    const content = document.getElementById('pixel-info-content');
    const ps = APP.metadata.pixel_size_um;
    const savedPS = loadDefaultPixelSize();
    content.innerHTML = `
        <div class="info-row"><span class="info-label">宽度</span><span class="info-value">${APP.metadata.width} px</span></div>
        <div class="info-row"><span class="info-label">高度</span><span class="info-value">${APP.metadata.height} px</span></div>
        <div class="info-row"><span class="info-label">通道数</span><span class="info-value">${APP.metadata.num_channels}</span></div>
        <div class="info-row"><span class="info-label">像素尺寸</span><span class="info-value">${ps ? ps.toFixed(4) + ' μm/px' : '未知'}</span></div>
        <details class="adv-details" style="margin-top:8px">
            <summary style="cursor:pointer;font-size:12px;color:#888">▸ 高级 — 手动覆盖像素尺寸 (仅当自动换算不准时)</summary>
            <div class="manual-ps" style="margin-top:8px">
                <input type="number" id="manual-pixel-size" placeholder="μm/px" step="0.001" min="0.0001" value="${ps ? ps.toFixed(4) : ''}">
                <button class="btn btn-sm" onclick="setManualPixelSize()">设置</button>
                <button class="btn btn-sm" onclick="saveDefaultPixelSize()">保存为默认</button>
            </div>
            ${savedPS ? `<div class="inset-hint">已保存默认像素尺寸: ${savedPS} μm/px</div>` : ''}
            ${ps === 1.0 ? '<div class="inset-hint">⚠️ FV3000 默认值 1.0 通常不准确，请手动输入真实值或设置默认值</div>' : ''}
        </details>
    `;
}

async function setManualPixelSize() {
    const val = parseFloat(document.getElementById('manual-pixel-size').value);
    if (val > 0) {
        APP.metadata.pixel_size_um = val;
        await syncPixelSizeToServer(val, 'manual');
        updateImageInfo();
        setStatus(`像素尺寸已设置为 ${val.toFixed(4)} μm/px`);
        // 像素尺寸变化会改变物理比例, 必须同步刷新预览标尺, 否则预览与导出漂移
        if (APP.scaleBarApplied) await applyScaleBar();
    }
}

async function syncPixelSizeToServer(val, source) {
    if (!APP.sessionId) return;
    try {
        const resp = await fetch('/api/set_pixel_size', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: APP.sessionId, pixel_size_um: val, source: source || 'manual' })
        });
        const data = await resp.json();
        if (!data.ok) {
            showError('同步像素尺寸失败', data.error || '');
        }
    } catch (e) {
        showError('网络错误', e.message);
    }
}

// ======== 物镜放大倍数 → 像素尺寸 ========
// Olympus FV3000 UPLXAPO 物镜标准标定 (从用户提供的 4 张 FV3000 详细面板截图交叉验证):
//   10× Zoom=1.0: FOV = 1272.792 μm (权威值) → 0.6219 μm/px @ 2048-wide
//   40× Zoom=1.0: FOV = 318.198 μm (权威值)  → 0.1554 μm/px @ 2048-wide (= 10× FOV / 4 ✓)
// 通用公式: μm/px = (FOV_10X_NATIVE_UM × 10 / mag) / image_width / zoom
//   zoom = 1.0 默认; >1 缩小 FOV (用户拍 10×/1024 误设 zoom=2 → FOV 减半)
// FV3000 TIFF 的 ImageDescription 含 Magnification / Zoom Size / 真实 μm/px, 自动解析后优先用其覆盖
const FOV_10X_NATIVE_UM = 1272.792;
const FOV_REF_WIDTH = 2048;
function computeUmPerPx(mag, imageWidth, zoom) {
    const m = parseFloat(mag);
    const w = parseInt(imageWidth);
    const z = parseFloat(zoom);
    if (!isFinite(m) || m <= 0 || !isFinite(w) || w <= 0) return null;
    if (!isFinite(z) || z <= 0) return null;
    return (FOV_10X_NATIVE_UM * 10.0 / m) / w / z;
}

let _magSyncTimer = null;
function onMagnificationChange() {
    const magEl = document.getElementById('scalebar-mag');
    const zoomEl = document.getElementById('scalebar-zoom');
    const readoutEl = document.getElementById('scalebar-mag-readout');
    if (!magEl || !readoutEl) return;
    const mag = parseFloat(magEl.value);
    const zoom = zoomEl ? parseFloat(zoomEl.value) : 1.0;
    const imgW = (APP.metadata && APP.metadata.width) || 0;
    if (!mag || mag <= 0 || !imgW) {
        readoutEl.textContent = '填写物镜倍数后自动换算 1 px = ? μm (Olympus FV3000 UPLXAPO 标定, FOV_10×=1272.792μm)';
        readoutEl.classList.remove('ok');
        return;
    }
    const px = computeUmPerPx(mag, imgW, zoom);
    if (!px) {
        readoutEl.textContent = '请检查 magnification / zoom / 图像宽度';
        readoutEl.classList.remove('ok');
        return;
    }
    const fovUm = FOV_10X_NATIVE_UM * 10.0 / mag / (zoom || 1.0);
    readoutEl.textContent = `→ 1 px = ${px.toFixed(4)} μm/px  (物镜 ${mag}×, Zoom ${zoom}×, 视野 ${fovUm.toFixed(1)} μm)`;
    readoutEl.classList.add('ok');
    // 自动防抖同步: 500ms 内无新输入才回写服务器 (避免每次按键都打请求)
    if (_magSyncTimer) clearTimeout(_magSyncTimer);
    _magSyncTimer = setTimeout(async () => {
        APP.metadata.pixel_size_um = px;
        const manualEl = document.getElementById('manual-pixel-size');
        if (manualEl) manualEl.value = px.toFixed(4);
        await syncPixelSizeToServer(px, `mag=${mag}/zoom=${zoom}`);
        if (APP.scaleBarApplied) await applyScaleBar();
    }, 500);
}

function loadDefaultPixelSize() {
    try {
        const v = localStorage.getItem('mss_default_pixel_size');
        return v ? parseFloat(v) : null;
    } catch (e) {
        return null;
    }
}

function saveDefaultPixelSize() {
    const val = parseFloat(document.getElementById('manual-pixel-size').value);
    if (val > 0) {
        try {
            localStorage.setItem('mss_default_pixel_size', val.toString());
            setStatus(`默认像素尺寸已保存为 ${val} μm/px`);
            updateImageInfo();
        } catch (e) {
            setStatus('保存失败：浏览器存储不可用');
        }
    } else {
        setStatus('请输入有效的像素尺寸');
    }
}

// ======== 分析 ========
function updateAnalysisUI() {
    const chSelect = document.getElementById('line-ch-select');
    chSelect.innerHTML = '';
    APP.configs.forEach((c, i) => {
        const btn = document.createElement('div');
        btn.className = 'ch-check selected';
        btn.textContent = c.name;
        btn.dataset.channel = i;
        btn.onclick = () => btn.classList.toggle('selected');
        chSelect.appendChild(btn);
    });

    const ch1sel = document.getElementById('pearson-ch1');
    const ch2sel = document.getElementById('pearson-ch2');
    ch1sel.innerHTML = ''; ch2sel.innerHTML = '';
    APP.configs.forEach((c, i) => {
        ch1sel.innerHTML += `<option value="${i}">${c.name}</option>`;
        ch2sel.innerHTML += `<option value="${i}">${c.name}</option>`;
    });
    if (APP.configs.length >= 2) ch2sel.value = '1';
}

function switchAnalysisTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('line-profile-section').classList.toggle('hidden', tab !== 'line');
    document.getElementById('pearson-section').classList.toggle('hidden', tab !== 'pearson');
}

function updateLineInfo() {
    const info = document.getElementById('line-info');
    if (APP.linePoints.length === 0) {
        info.innerHTML = '<span class="empty-state">在画布上点击两点绘制线</span>';
    } else if (APP.linePoints.length === 1) {
        info.innerHTML = `<span>起点: (${Math.round(APP.linePoints[0].x)}, ${Math.round(APP.linePoints[0].y)})<br>请点击第二个点</span>`;
    } else {
        const dx = APP.linePoints[1].x - APP.linePoints[0].x;
        const dy = APP.linePoints[1].y - APP.linePoints[0].y;
        const length = Math.sqrt(dx*dx + dy*dy);
        const ps = APP.metadata.pixel_size_um;
        info.innerHTML = `<span>P1: (${Math.round(APP.linePoints[0].x)}, ${Math.round(APP.linePoints[0].y)})<br>
            P2: (${Math.round(APP.linePoints[1].x)}, ${Math.round(APP.linePoints[1].y)})<br>
            线长: ${Math.round(length)} px ${ps ? `(${(length * ps).toFixed(1)} μm)` : ''}</span>`;
    }
}

async function requestLineProfile() {
    if (APP.linePoints.length !== 2) return;
    const selected = [];
    document.querySelectorAll('#line-ch-select .ch-check.selected').forEach(el => {
        selected.push(parseInt(el.dataset.channel));
    });
    if (selected.length === 0) selected = [0];

    try {
        const resp = await fetch('/api/line_profile', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: APP.sessionId,
                x1: APP.linePoints[0].x,
                y1: APP.linePoints[0].y,
                x2: APP.linePoints[1].x,
                y2: APP.linePoints[1].y,
                channels: selected
            })
        });
        const data = await resp.json();
        if (data.error) { showError('Line profile 失败', data.error); return; }
        APP.lineData = data;
        drawLineProfile(data.profiles);
    } catch (err) {
        showError('网络错误', err.message);
    }
}

function drawLineProfile(profiles) {
    const chartArea = document.getElementById('line-profile-chart');
    chartArea.innerHTML = '';
    const cv = document.createElement('canvas');
    cv.width = chartArea.clientWidth || 230;
    cv.height = chartArea.clientHeight || 150;
    chartArea.appendChild(cv);
    const cctx = cv.getContext('2d');
    const w = cv.width, h = cv.height;

    cctx.fillStyle = '#f5f7fa';
    cctx.fillRect(0, 0, w, h);
    cctx.strokeStyle = '#dce1e8';
    cctx.strokeRect(0, 0, w, h);

    const margin = { top: 10, right: 10, bottom: 20, left: 30 };
    const plotW = w - margin.left - margin.right;
    const plotH = h - margin.top - margin.bottom;
    let globalMax = 0;
    Object.values(profiles).forEach(p => { globalMax = Math.max(globalMax, ...p.values); });
    globalMax = globalMax || 1;

    cctx.strokeStyle = '#e8ecf1';
    cctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
        const y = margin.top + plotH * (1 - i/4);
        cctx.beginPath();
        cctx.moveTo(margin.left, y);
        cctx.lineTo(margin.left + plotW, y);
        cctx.stroke();
        cctx.fillStyle = '#7f8c8d';
        cctx.font = '9px Arial';
        cctx.fillText(Math.round(globalMax * i / 4), 2, y + 3);
    }

    const channelColors = APP.configs.map(c => c.color);
    let idx = 0;
    Object.entries(profiles).forEach(([name, profile]) => {
        const color = channelColors[idx] || '#ffffff';
        const positions = profile.positions;
        const values = profile.values;
        const maxPos = positions[positions.length - 1] || 1;
        cctx.strokeStyle = color;
        cctx.lineWidth = 1.5;
        cctx.beginPath();
        positions.forEach((pos, i) => {
            const x = margin.left + (pos / maxPos) * plotW;
            const y = margin.top + plotH * (1 - values[i] / globalMax);
            if (i === 0) cctx.moveTo(x, y); else cctx.lineTo(x, y);
        });
        cctx.stroke();
        idx++;
    });
    cctx.fillStyle = '#7f8c8d';
    cctx.font = '9px Arial';
    cctx.fillText(APP.metadata.pixel_size_um ? 'μm' : 'px', w - 20, h - 5);
}

async function calculatePearson() {
    if (!APP.sessionId) return;
    const ch1 = parseInt(document.getElementById('pearson-ch1').value);
    const ch2 = parseInt(document.getElementById('pearson-ch2').value);
    const region = document.getElementById('pearson-region').value;
    const roi = region === 'roi' ? APP.roi : null;

    try {
        const resp = await fetch('/api/pearson', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: APP.sessionId, channel1: ch1, channel2: ch2, roi })
        });
        const data = await resp.json();
        if (data.error) { showError('Pearson 失败', data.error); return; }
        APP.pearsonResult = data;
        const strength = Math.abs(data.pearson_r) > 0.7 ? '强相关' : Math.abs(data.pearson_r) > 0.4 ? '中等相关' : '弱相关';
        const color = Math.abs(data.pearson_r) > 0.7 ? '#27ae60' : Math.abs(data.pearson_r) > 0.4 ? '#f39c12' : '#e74c3c';
        document.getElementById('pearson-result').innerHTML = `
            <div style="margin-bottom:6px"><strong>Pearson r = ${data.pearson_r.toFixed(4)}</strong></div>
            <div>p-value: ${data.p_value < 0.001 ? '< 0.001' : data.p_value.toFixed(4)}</div>
            <div>像素数: ${data.n_pixels}</div>
            <div>通道: ${data.channel1_name} ↔ ${data.channel2_name}</div>
            <div style="margin-top:6px;font-size:11px;color:${color}">${strength}</div>
        `;
    } catch (err) {
        showError('网络错误', err.message);
    }
}

// ======== ROI / Inset ========
function updateROIInfo() {
    const info = document.getElementById('roi-info');
    if (!APP.roi) {
        info.innerHTML = '<span class="empty-state">使用 ROI 工具在画布上框选，或在下方输入精确尺寸</span>';
        document.getElementById('btn-roi-preview').disabled = true;
        document.getElementById('btn-set-inset').disabled = true;
        return;
    }
    // 把当前 ROI 的精确尺寸回填到输入框, 保证 W/H/X/Y 与画布一致
    document.getElementById('roi-x').value = APP.roi.x;
    document.getElementById('roi-y').value = APP.roi.y;
    document.getElementById('roi-w').value = APP.roi.width;
    document.getElementById('roi-h').value = APP.roi.height;
    const ps = APP.metadata.pixel_size_um;
    const wUm = ps ? (APP.roi.width * ps).toFixed(1) + ' μm' : APP.roi.width + ' px';
    const hUm = ps ? (APP.roi.height * ps).toFixed(1) + ' μm' : APP.roi.height + ' px';
    info.innerHTML = `
        <div>位置: (${APP.roi.x}, ${APP.roi.y})</div>
        <div>尺寸: ${wUm} × ${hUm}</div>
        <div>面积: ${APP.roi.width * APP.roi.height} px²</div>
    `;
    document.getElementById('btn-roi-preview').disabled = false;
    document.getElementById('btn-set-inset').disabled = false;
}

async function previewROI() {
    if (!APP.roi || !APP.sessionId) return;
    try {
        const resp = await fetch('/api/roi_preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: APP.sessionId, roi: APP.roi })
        });
        const data = await resp.json();
        if (data.error) { showError('ROI 预览失败', data.error); return; }
        const w = window.open('', 'ROI Preview');
        w.document.write(`<img src="data:image/png;base64,${data.preview}" style="max-width:100%">`);
        w.document.title = 'ROI Preview';
    } catch (err) {
        showError('网络错误', err.message);
    }
}

function setROIAsInset() {
    // 只要启用右上角放大并选中 ROI, 导出时自动应用
    document.getElementById('inset-enabled').checked = true;
    renderMainCanvas();
    setStatus('当前 ROI 已作为 Inset 源 (右上角放大预览已启用)');
    updateExportPanels();
}

// ======== 导出 ========
function updateExportPanels() {
    const panelSelect = document.getElementById('export-panels');
    panelSelect.innerHTML = '';
    // 默认勾选: 所有活跃通道 + Merge; ROI/Inset/Profile 用户按需勾
    const activeCount = APP.activeCount || (APP.configs ? APP.configs.length : 0);
    APP.configs.slice(0, activeCount).forEach((c, i) => {
        panelSelect.innerHTML += `<label class="panel-check"><input type="checkbox" value="ch${i}" checked onchange="buildGridPreview()"> ${c.name}</label>`;
    });
    panelSelect.innerHTML += `<label class="panel-check"><input type="checkbox" value="merge" checked onchange="buildGridPreview()"> Merge (合并图)</label>`;
    if (APP.roi) {
        panelSelect.innerHTML += `<label class="panel-check"><input type="checkbox" value="roi" onchange="buildGridPreview()"> ROI 区域</label>`;
        panelSelect.innerHTML += `<label class="panel-check"><input type="checkbox" value="inset" onchange="buildGridPreview()"> Inset (放大图)</label>`;
    }
    if (APP.linePoints.length === 2) {
        panelSelect.innerHTML += `<label class="panel-check"><input type="checkbox" value="profile" onchange="buildGridPreview()"> Profile plot (曲线)</label>`;
    }
    buildGridPreview();
}

// 实时迷你网格预览: 反映当前布局 × 勾选面板
function buildGridPreview() {
    const el = document.getElementById('layout-preview');
    if (!el) return;
    const panels = [];
    document.querySelectorAll('#export-panels input:checked').forEach(c => panels.push(c.value));
    const layout = (document.getElementById('export-layout') || {}).value || 'auto';
    const n = panels.length;

    const COL = {
        merge: '#94a3b8', roi: '#f39c12', inset: '#a855f7', profile: '#64748b'
    };
    const chColor = (i) => (APP.configs && APP.configs[i] && APP.configs[i].color) ||
        ['#3b82f6', '#22c55e', '#ef4444', '#f59e0b'][i % 4];
    const rawLabel = (document.getElementById('export-labels') || {}).value || 'letter';
    // 兼容老值 'A' / 'colored_slash'
    const labelMode = rawLabel === 'A' ? 'letter' : (rawLabel === 'colored_slash' ? 'name' : rawLabel);
    const leftAlign = labelMode === 'short';
    const chName = (i) => (APP.configs && APP.configs[i] && APP.configs[i].name) || `Ch${i + 1}`;
    const panelColor = (p) => p.startsWith('ch') ? chColor(parseInt(p.slice(2))) : (COL[p] || '#cbd5e1');

    const W = 150, H = 150, pad = 6, gapg = 3;

    // ====== 顶部 header (与导出一致: letter 黑色 / name 通道色名 / short 短码左对齐) ======
    const headerH = labelMode === 'none' ? 0 : 16;
    let headerText = '';
    if (headerH) {
        panels.forEach((p, i) => {
            let t, c;
            if (labelMode === 'letter') {
                t = String.fromCharCode(65 + i); c = '#333';
            } else if (labelMode === 'short') {
                if (p === 'merge') { t = 'Merge'; c = '#333'; }
                else if (p.startsWith('ch')) {
                    const ci = parseInt(p.slice(2));
                    t = shortChannelCode(chColor(ci), chName(ci)); c = chColor(ci);
                } else {
                    t = ({ roi: 'ROI', inset: 'Inset', profile: 'Profile' })[p] || p; c = COL[p] || '#333';
                }
            } else { // name
                t = p.startsWith('ch') ? chName(parseInt(p.slice(2)))
                    : ({ merge: 'Merge', roi: 'ROI', inset: 'Inset', profile: 'Profile' }[p] || p);
                c = panelColor(p);
            }
            headerText += `<tspan fill="${c}">${t}</tspan>`;
            if (i < panels.length - 1) headerText += `<tspan fill="#777"> / </tspan>`;
        });
    }
    const headerEl = headerH
        ? `<text x="${leftAlign ? pad : W / 2}" y="11" font-size="9" font-family="Arial" text-anchor="${leftAlign ? 'start' : 'middle'}">${headerText}</text>`
        : '';

    // ====== merge_left 特殊预览 ======
    if (layout === 'merge_left') {
        const rightPanels = panels.filter(p => p !== 'merge');
        const hasMerge = panels.includes('merge');
        const N = rightPanels.length;
        if (N === 0) {
            el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%"><text x="${W/2}" y="${H/2+3}" font-size="9" font-family="Arial" fill="#94a3b8" text-anchor="middle">未选面板</text></svg>`;
            return;
        }

        // 几何: 左大格 (merge 或第一张), 右 N 列竖排 (headerH 由 labelMode 决定)
        const small_w = (W - pad * 2 - gapg) / (N + 1);
        const small_h = (H - pad * 2 - headerH - (N - 1) * gapg) / N;
        const merge_w = small_w * N;
        const merge_h = N * small_h + (N - 1) * gapg;
        const total_w = 2 * pad + merge_w + gapg + small_w;
        const total_h = H;

        let rects = '';
        // 左大格
        const leftKey = hasMerge ? 'merge' : rightPanels[0];
        if (hasMerge || rightPanels.length) {
            rects += `<rect x="${pad}" y="${(pad + headerH).toFixed(1)}" width="${merge_w.toFixed(1)}" height="${merge_h.toFixed(1)}" rx="2" fill="${panelColor(leftKey)}" opacity="0.9"/>`;
        }
        // 右列 (含通道/ROI/Inset/Profile)
        rightPanels.forEach((p, i) => {
            const x = pad + merge_w + gapg;
            const y = pad + headerH + i * (small_h + gapg);
            rects += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${small_w.toFixed(1)}" height="${small_h.toFixed(1)}" rx="2" fill="${panelColor(p)}" opacity="0.9"/>`;
        });
        el.innerHTML = `<svg viewBox="0 0 ${total_w} ${total_h}" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">
            ${headerEl}
            ${rects}
        </svg>`;
        return;
    }

    // ====== 原 cols 网格预览 ======
    let cols;
    if (layout === '1xn') cols = n;
    else if (layout === '2col') cols = 2;
    else if (layout === '3col') cols = 3;
    else if (layout === '4col') cols = 4;
    else cols = n <= 2 ? n : n <= 4 ? Math.min(n, 4) : n <= 6 ? Math.min(n, 6) : Math.min(n, 4);
    cols = Math.max(1, cols);
    const rows = Math.ceil(n / cols) || 1;

    const cellH = (H - pad * 2 - headerH - gapg * (rows - 1)) / rows;
    const cellW = (W - pad * 2 - gapg * (cols - 1)) / cols;
    let rects = '';
    panels.forEach((p, idx) => {
        const r = Math.floor(idx / cols), c = idx % cols;
        const x = pad + c * (cellW + gapg), y = pad + headerH + r * (cellH + gapg);
        rects += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${cellW.toFixed(1)}" height="${cellH.toFixed(1)}" rx="2" fill="${panelColor(p)}" opacity="0.9"/>`;
    });
    if (n === 0) rects = `<text x="${W/2}" y="${H/2+3}" font-size="9" font-family="Arial" fill="#94a3b8" text-anchor="middle">未选面板</text>`;
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">${headerEl}${rects}</svg>`;
}

function showExportPanel() {
    document.getElementById('export-panel').scrollIntoView({ behavior: 'smooth' });
}

// 模板已移除: 布局 + 标签样式两个下拉 + 实时 layout-preview 缩略图取代 6 卡片模板


async function exportLayout() {
    if (!APP.sessionId) return;
    showLoading('正在生成排版图...');

    const panels = [];
    document.querySelectorAll('#export-panels input:checked').forEach(el => panels.push(el.value));

    const dpi = parseInt(document.getElementById('export-dpi').value) || 300;
    const format = document.getElementById('export-format').value;
    const labels = document.getElementById('export-labels').value;
    const includeScalebar = document.getElementById('export-scalebar').checked;
    const includePearson = document.getElementById('export-pearson').checked;

    // 只要勾选导出 scale bar, 就发送配置 (优先用已应用的, 否则用默认)
    let scaleBar = null;
    if (includeScalebar) {
        scaleBar = APP.scaleBarConfig || {
            length_um: parseFloat(document.getElementById('scalebar-length').value) || 50,
            position: document.getElementById('scalebar-position').value,
            color: document.getElementById('scalebar-color').value,
            thickness: parseInt(document.getElementById('scalebar-thickness').value) || 3,
            font_size: 14
        };
    }
    // 带上取色器当前颜色, 后端 _draw_roi_boundary 用它绘制导出图的虚线框
    // stroke_width_pt: 未单独指定时沿用界面「线宽」设置, 供矢量导出的 ROI 描边使用
    const roi = APP.roi
        ? { ...APP.roi, color: getROIColor(), stroke_width_pt: parseFloat(document.getElementById('scalebar-thickness').value) || 1.0 }
        : null;
    const inset = roi ? getInsetConfig() : null;

    const layout = document.getElementById('export-layout').value;
    const fontSize = parseInt(document.getElementById('export-font-size').value) || 0;
    const gap = parseInt(document.getElementById('export-gap').value) || 0;
    const margin = parseInt(document.getElementById('export-margin').value) || 10;
    const nativeSize = document.getElementById('export-native-size').checked;
    const svgDisplayPt = parseInt(document.getElementById('export-svg-display').value) || 0;

    try {
        const resp = await fetch('/api/export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: APP.sessionId,
                panels, dpi, format, labels,
                layout,
                font_size: fontSize,
                gap,
                margin,
                native_size: nativeSize,
                svg_display_pt: svgDisplayPt,
                scale_bar: scaleBar,
                inset,
                roi,
                pixel_size_um: APP.metadata.pixel_size_um,
                line_data: APP.lineData,
                pearson: includePearson ? APP.pearsonResult : null
            })
        });

        if (resp.ok) {
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `microscope_layout.${format}`;
            a.click();
            URL.revokeObjectURL(url);
            setStatus(`导出完成: ${panels.length} 面板, ${format.toUpperCase()}, ${dpi} DPI`);
        } else {
            const data = await resp.json().catch(() => ({}));
            showError('导出失败', data.error || `HTTP ${resp.status}`);
        }
    } catch (err) {
        showError('网络错误', err.message);
    }
    hideLoading();
}

async function exportSingle(type) {
    if (!APP.sessionId) return;
    showLoading('正在导出...');
    const dpi = parseInt(document.getElementById('export-dpi').value) || 300;
    const format = document.getElementById('export-format').value;
    const includeScalebar = document.getElementById('export-scalebar').checked;
    let scaleBar = null;
    if (includeScalebar) {
        scaleBar = APP.scaleBarConfig || {
            length_um: parseFloat(document.getElementById('scalebar-length').value) || 50,
            position: document.getElementById('scalebar-position').value,
            color: document.getElementById('scalebar-color').value,
            thickness: parseInt(document.getElementById('scalebar-thickness').value) || 3,
            font_size: 14
        };
    }
    const inset = APP.roi ? getInsetConfig() : null;
    const svgDisplayPt = parseInt(document.getElementById('export-svg-display').value) || 0;
    // ROI 描边线宽: 未单独指定时沿用界面「线宽」设置, 保证导出与界面一致
    const roi = APP.roi
        ? { ...APP.roi, stroke_width_pt: parseFloat(document.getElementById('scalebar-thickness').value) || 1.0 }
        : null;

    try {
        const resp = await fetch('/api/export_single', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: APP.sessionId, panel_type: type, dpi, format, svg_display_pt: svgDisplayPt, scale_bar: scaleBar, inset, roi, pixel_size_um: APP.metadata.pixel_size_um })
        });

        if (resp.ok) {
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${type}.${format}`;
            a.click();
            URL.revokeObjectURL(url);
            setStatus(`已导出 ${type}.${format}`);
        } else {
            const data = await resp.json().catch(() => ({}));
            showError('导出失败', data.error || `HTTP ${resp.status}`);
        }
    } catch (err) {
        showError('网络错误', err.message);
    }
    hideLoading();
}

// ======== 辅助 ========
function clearROI() { APP.roi = null; renderMainCanvas(); updateROIInfo(); updateExportPanels(); }
function clearLine() { APP.linePoints = []; APP.lineData = null; renderMainCanvas(); updateLineInfo(); document.getElementById('line-profile-chart').innerHTML = ''; updateExportPanels(); }

function setStatus(text) { document.getElementById('status-text').textContent = text; }

function showLoading(msg) {
    const overlay = document.createElement('div');
    overlay.className = 'loading-overlay';
    overlay.id = 'loading-overlay';
    overlay.innerHTML = `<div class="loading-spinner"><div class="spinner-icon">⏳</div><div style="margin-top:8px">${msg}</div></div>`;
    document.body.appendChild(overlay);
}

function hideLoading() {
    const o = document.getElementById('loading-overlay');
    if (o) o.remove();
}

// ======== 初始化 & 全局事件 ========
setStatus('就绪 | 请导入 TIFF 图像');
setTool('pan');

// 拖拽上传
['dragover', 'dragenter'].forEach(evt => {
    container.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        container.classList.add('drag-over');
    });
});
['dragleave', 'drop'].forEach(evt => {
    container.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        container.classList.remove('drag-over');
    });
});
container.addEventListener('drop', (e) => {
    const files = Array.from(e.dataTransfer.files).filter(f => /\.tiff?$/i.test(f.name));
    if (files.length) uploadFiles(files, files.length === 1 ? '/api/upload' : '/api/upload_multi');
});

// 键盘快捷键
document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;
    if (e.key === '+' || e.key === '=') zoomIn();
    if (e.key === '-') zoomOut();
    if (e.key === '0') zoomFit();
    if (e.key === 'r') setTool('roi');
    if (e.key === 'i') setTool('inset');
    if (e.key === 'l') setTool('line');
    if (e.key === 'p') setTool('pan');
    if (e.key === 'Escape') {
        APP.roi = null; APP.linePoints = []; renderMainCanvas(); updateROIInfo(); updateLineInfo();
    }
});

// Inset 配置实时更新预览
['inset-enabled', 'inset-zoom', 'inset-position'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
        el.addEventListener('change', () => { renderMainCanvas(); });
        el.addEventListener('input', () => { renderMainCanvas(); });
    }
});

// ============ 面板折叠 ============
function initPanelCollapse() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem('ms_panel_collapsed') || '{}'); } catch (e) { saved = {}; }
    document.querySelectorAll('.panel-title[data-panel]').forEach(title => {
        const id = title.getAttribute('data-panel');
        const panel = document.getElementById(id);
        if (!panel) return;
        // 恢复保存的折叠状态
        if (saved[id]) panel.classList.add('collapsed');
        title.addEventListener('click', (e) => {
            // 点击标题里的可交互元素(如 select)不误触折叠
            if (e.target.tagName === 'SELECT' || e.target.tagName === 'OPTION') return;
            panel.classList.toggle('collapsed');
            saveCollapseState();
            // 展开色阶面板后重绘直方图 (折叠时 clientWidth 为 0)
            if (id === 'levels-panel' && !panel.classList.contains('collapsed')) {
                setTimeout(drawLevelsHistogram, 0);
            }
        });
    });
    function saveCollapseState() {
        const state = {};
        document.querySelectorAll('.panel-title[data-panel]').forEach(t => {
            const pid = t.getAttribute('data-panel');
            const p = document.getElementById(pid);
            if (p && p.classList.contains('collapsed')) state[pid] = true;
        });
        try { localStorage.setItem('ms_panel_collapsed', JSON.stringify(state)); } catch (e) {}
    }
}
initPanelCollapse();
syncDisplayModeBtn();   // 恢复上次的彩色/灰度显示偏好
refreshChannelNameDatalist();
refreshChCountSelect();
