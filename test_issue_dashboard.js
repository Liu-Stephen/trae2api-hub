#!/usr/bin/env node
/**
 * test_issue_dashboard.js — 独立回归：添加账号 UI + 导入契约 + 模型上下文契约。
 *
 * 不启动网关、不访问网络、不读取真实账号；只读 dashboard.html。
 * 运行：node test_issue_dashboard.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const htmlPath = path.join(__dirname, 'dashboard.html');
const html = fs.readFileSync(htmlPath, 'utf8');
let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    failures.push({ name, message: err && err.message || String(err) });
  }
}

function extract(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  const end = html.indexOf(endMarker, start + startMarker.length);
  if (start < 0 || end < 0) {
    throw new Error('missing marker: ' + startMarker + ' / ' + endMarker);
  }
  return html.slice(start, end);
}

function loadContracts() {
  const importSrc = extract('/* BEGIN-IMPORT-CONTRACT', '/* END-IMPORT-CONTRACT */');
  const contextSrc = extract('/* BEGIN-CONTEXT-CONTRACT', '/* END-CONTEXT-CONTRACT */');
  return new Function(
    importSrc + '\n' + contextSrc + '\n' +
    'return { normalizeImportPreview, summarizeImportResult, formatApiError, ' +
    'contextKeyOf, contextLabelEquals, resolveContextSelection, contextBudgetNote };'
  )();
}

let contracts = null;
check('抽取导入/上下文契约纯函数', () => {
  contracts = loadContracts();
  assert.strictEqual(typeof contracts.normalizeImportPreview, 'function');
  assert.strictEqual(typeof contracts.summarizeImportResult, 'function');
  assert.strictEqual(typeof contracts.formatApiError, 'function');
  assert.strictEqual(typeof contracts.resolveContextSelection, 'function');
});

check('所有内联脚本语法可编译', () => {
  const blocks = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
    .map(m => m[1])
    .filter(src => src.trim());
  assert.ok(blocks.length >= 2, 'expected inline scripts');
  blocks.forEach((src, index) => {
    try {
      new Function(src);
    } catch (err) {
      throw new Error('script block #' + index + ' syntax error: ' + err.message);
    }
  });
});

check('新 preview 契约：accounts[action] + errors', () => {
  const view = contracts.normalizeImportPreview({
    accounts: [
      { uid: 'a', action: 'add' },
      { uid: 'b', action: 'update' },
      { uid: 'c', action: 'skip', reason: 'exists' },
      { uid: 'd', action: 'add' }
    ],
    errors: ['row 4 invalid']
  });
  assert.deepStrictEqual(view.counts, { add: 2, update: 1, skip: 1, invalid: 0, errors: 1 });
  assert.strictEqual(view.added.length, 2);
  assert.strictEqual(view.updated.length, 1);
  assert.strictEqual(view.skipped.length, 1);
  assert.strictEqual(view.errors.length, 1);
});

check('旧 preview 契约：added/updated/skipped/invalid 数组', () => {
  const view = contracts.normalizeImportPreview({
    added: [{ uid: 'a' }],
    updated: [{ uid: 'b' }],
    skipped: [{ uid: 'c' }],
    invalid: [{ uid: 'd', reason: 'bad token' }]
  });
  assert.deepStrictEqual(view.counts, { add: 1, update: 1, skip: 1, invalid: 1, errors: 0 });
});

check('新 import 结果：imported/updated/skipped 整数 + errors', () => {
  const summary = contracts.summarizeImportResult({
    imported: 3,
    updated: 2,
    skipped: 1,
    errors: ['uid x: refreshToken expired']
  }, 6);
  assert.strictEqual(summary.imported, 3);
  assert.strictEqual(summary.updated, 2);
  assert.strictEqual(summary.skipped, 1);
  assert.strictEqual(summary.touched, 5);
  assert.strictEqual(summary.errors.length, 1);
});

check('旧 import 结果：added/updated/skipped 数组仍兼容', () => {
  const summary = contracts.summarizeImportResult({
    added: [1, 2, 3],
    updated: [4],
    skipped: [5],
    invalid: [{ reason: 'bad' }]
  }, 5);
  assert.strictEqual(summary.imported, 3);
  assert.strictEqual(summary.updated, 1);
  assert.strictEqual(summary.skipped, 1);
  assert.strictEqual(summary.errors.length, 1);
});

check('login 501 JSON 转可读错误（status/message/error.message）', () => {
  const msg = contracts.formatApiError({
    status: 501,
    message: 'device login is not supported',
    error: { message: 'unsupported in this build', type: 'invalid_request_error' }
  });
  assert.ok(msg.includes('[501]'), msg);
  assert.ok(msg.includes('device login is not supported'), msg);
  assert.ok(msg.includes('invalid_request_error'), msg);
  const nested = contracts.formatApiError({ error: { message: 'only nested message' } });
  assert.strictEqual(nested, 'only nested message');
  const asError = contracts.formatApiError(new Error('network down'), 'fallback');
  assert.strictEqual(asError, 'network down');
});

check('context_window_selected 优先于本地缓存', () => {
  const selected = contracts.resolveContextSelection(
    { id: 'm1', upstream_key: 'k1', context_window_selected: '256K' },
    { k1: '128K' }
  );
  assert.strictEqual(selected, '256K');
  assert.strictEqual(contracts.contextKeyOf({ id: 'm1', upstream_key: 'k1' }), 'k1');
});

check('上下文缓存回退支持 upstream key / model id，数值标签可等价比较', () => {
  assert.strictEqual(
    contracts.resolveContextSelection({ id: 'm1', upstream_key: 'k1' }, { k1: '128K' }),
    '128K'
  );
  assert.strictEqual(
    contracts.resolveContextSelection({ id: 'm1', upstream_key: 'k1' }, { m1: 128000 }),
    '128000'
  );
  assert.strictEqual(contracts.resolveContextSelection({ id: 'm1' }, {}), '');
  assert.ok(contracts.contextLabelEquals('128K', '128K'));
  assert.ok(contracts.contextLabelEquals(128000, '128000'));
  assert.ok(!contracts.contextLabelEquals('128K', '256K'));
  assert.ok(!contracts.contextLabelEquals('', ''));
});

check('预算说明明确是本机保守估算、不截断历史', () => {
  const note = contracts.contextBudgetNote();
  assert.ok(note.includes('UTF-8'), note);
  assert.ok(note.includes('保守'), note);
  assert.ok(note.includes('不截断历史'), note);
  assert.ok(note.includes('不等于'), note);
});

const mustContain = [
  'openAddAccount',
  'openRefreshTokenImport',
  'doImportRefreshToken',
  'beginAddByScan',
  'parseImportPaste',
  'importAllDesktop',
  'refresh_token: token',
  'context_window_selected',
  'window.MODEL_REALM = realm',
  'realm: realm',
  'resolveContextSelection',
  'contextBudgetNote',
  'ctxEq(ctxCur, l)',
  'id="btnCheckin"',
  'id="sectionGrowthTasks"',
  'adminGroupAction(\'checkin\',this)',
  'async function doCheckin(btn)',
  'function checkinBadge(a)',
  'async function runGrowthTasks(btn)',
  'async function triggerCatTravel(btn)',
  'normalizeImportPreview',
  'summarizeImportResult',
  'desktopImportSummary',
  "d.valid",
  "data-path",
  "data-realm",
  '浏览器 OAuth 登录',
  'async function oauthStart('
];
mustContain.forEach(token => {
  check('dashboard 必须包含: ' + token, () => {
    assert.ok(html.includes(token), 'missing token: ' + token);
  });
});

const mustNotContain = [
  '添加账号 (OAuth)',
  'OAuth 设备授权',
  'Personal Access Token',
  '导入 PAT',
  'id="patModal"',
  'openPatImport',
  'doImportPat',
  'probeLoginUnsupported(this)',
  '设备授权登录（当前构建不支持）',

];
mustNotContain.forEach(token => {
  check('dashboard 不得包含: ' + token, () => {
    assert.ok(!html.includes(token), 'unexpected token: ' + token);
  });
});

check('浏览器 OAuth 提供开始、轮询、取消与回调并避免 setInterval 重叠', () => {
  ["oauthRequest('start'", "oauthRequest('poll?state='", "oauthRequest('cancel'", "oauthRequest('callback'"].forEach(token => {
    assert.ok(html.includes(token), 'missing OAuth endpoint: ' + token);
  });
  assert.ok(html.includes('AbortController'), 'polling must be abortable');
  assert.ok(!/setInterval\s*\(\s*poll/i.test(html), 'login polling must not overlap');
});

check('模型上下文保存请求携带目录区域', () => {
  const start = html.indexOf('async function saveModelContext');
  const end = html.indexOf('\n}', start);
  const body = html.slice(start, end);
  assert.ok(body.includes('/settings/model-context'), 'missing endpoint');
  assert.ok(body.includes('realm: realm'), 'missing realm in payload');
  assert.ok(body.includes('window.MODEL_REALM'), 'missing MODEL_REALM source');
  assert.ok(body.includes('context_window_selected'), 'missing selected write-back');
});

check('导入预览/提交使用统一契约函数', () => {
  assert.ok(html.includes('normalizeImportPreview(res)'), 'preview must normalize');
  assert.ok(html.includes('summarizeImportResult(r.result || r'), 'commit must summarize');
});

check('关键模态框 id 唯一', () => {
  ['loginModal', 'refreshTokenModal', 'importModal', 'desktopScanModal'].forEach(id => {
    const count = (html.match(new RegExp('id="' + id + '"', 'g')) || []).length;
    assert.strictEqual(count, 1, id + ' should appear exactly once, got ' + count);
  });
});

/* ---- 区域连坐暂停（设置页卡片）---- */
function loadRestrictionUi() {
  const src = extract('/* BEGIN-RESTRICTION-UI', '/* END-RESTRICTION-UI */');
  return new Function(
    'function esc(s){ return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")'
    + '.replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }\n' + src +
    '\nreturn { restrictionRealmLabel, restrictionClock, restrictionHoldsHtml };'
  )();
}

check('抽取连坐暂停 UI 纯函数', () => {
  const ui = loadRestrictionUi();
  assert.strictEqual(typeof ui.restrictionClock, 'function');
  assert.strictEqual(typeof ui.restrictionHoldsHtml, 'function');
});

check('倒计时格式化：分:秒，负数归零', () => {
  const ui = loadRestrictionUi();
  assert.strictEqual(ui.restrictionClock(1500), '25:00');
  assert.strictEqual(ui.restrictionClock(59), '0:59');
  assert.strictEqual(ui.restrictionClock(0), '0:00');
  assert.strictEqual(ui.restrictionClock(-5), '0:00');
});

check('区域名称映射：cn/intl/未知', () => {
  const ui = loadRestrictionUi();
  assert.strictEqual(ui.restrictionRealmLabel('cn'), '国内版');
  assert.strictEqual(ui.restrictionRealmLabel('intl'), '国际版');
  assert.strictEqual(ui.restrictionRealmLabel(''), '?');
  assert.strictEqual(ui.restrictionRealmLabel('other'), 'other');
});

check('无暂停时显示「没有区域被暂停」', () => {
  const ui = loadRestrictionUi();
  const out = ui.restrictionHoldsHtml({ hold_until: {}, events: {}, wave_min: 3 }, 1000);
  assert.ok(out.includes('没有区域被暂停'), out);
  assert.ok(!out.includes('已暂停'), out);
});

check('有暂停时显示剩余时间与自动恢复时刻', () => {
  const ui = loadRestrictionUi();
  const now = 1700000000;
  const out = ui.restrictionHoldsHtml(
    { hold_until: { cn: now + 125 }, events: {}, wave_min: 3, hold_seconds: 1800 }, now);
  assert.ok(out.includes('已暂停'), out);
  assert.ok(out.includes('国内版'), out);
  assert.ok(out.includes('2:05'), out);
  assert.ok(out.includes('data-restriction-left="cn"'), out);
  assert.ok(out.includes('自动恢复'), out);
});

check('已过期的暂停不再显示为暂停', () => {
  const ui = loadRestrictionUi();
  const now = 1700000000;
  const out = ui.restrictionHoldsHtml({ hold_until: { cn: now - 1 }, events: {} }, now);
  assert.ok(out.includes('没有区域被暂停'), out);
});

check('波次进度按「n/阈值 个账号」显示', () => {
  const ui = loadRestrictionUi();
  const out = ui.restrictionHoldsHtml(
    { hold_until: {}, events: { cn: ['a', 'b'] }, wave_min: 3 }, 1700000000);
  assert.ok(out.includes('国内版 2/3 个账号'), out);
  assert.ok(out.includes('达到 3 个即暂停该区域'), out);
});

check('设置页有连坐暂停卡片且与后端接口对齐', () => {
  assert.ok(html.includes('id="sectionRestriction"'), 'missing section');
  ['restrictionHoldSeconds', 'restrictionWaveMin', 'restrictionWaveWindow',
   'restrictionHolds'].forEach(id => {
    assert.ok(html.includes('id="' + id + '"'), 'missing input: ' + id);
  });
  assert.ok(html.includes('renderRestriction(data.restriction || null)'),
    'loadSettings must render the snapshot');
  assert.ok(html.includes("'/settings/save', {restriction: payload}"),
    'save must post the restriction block');
  assert.ok(html.includes("'/settings/save', {restriction_clear: true}"),
    'clear must post restriction_clear');
});

check('连坐暂停状态行的两种文案都有英文精确键', () => {
  // 翻译是精确匹配（trim/空白折叠之外不做标点归一）：全角（）与半角 ()
  // 是两个不同的键，写错一个字母就整行不翻译。
  const en = JSON.parse(fs.readFileSync(path.join(__dirname, 'i18n', 'en.json'), 'utf8'));
  const row = html.match(/state\.textContent = paused \? '([^']+)' : '([^']+)'/);
  assert.ok(row, 'missing restriction state row');
  [row[1], row[2]].forEach(text => {
    assert.ok(Object.prototype.hasOwnProperty.call(en.messages, text),
      'no exact en.json key for: ' + text);
  });
});

console.log('');
console.log('dashboard issue regression');
console.log('  file      : ' + htmlPath);
console.log('  checks    : ' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) {
  failures.forEach(f => console.log('  FAIL      : ' + f.name + '\n               ' + f.message));
  process.exitCode = 1;
} else {
  console.log('  result    : OK');
}
