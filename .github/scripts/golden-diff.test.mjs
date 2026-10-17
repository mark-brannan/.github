// Tests for golden-diff.mjs. Run: node --test .github/scripts/
// (after `npm ci` in .github/scripts/golden-diff).
//
// What matters: a golden whose bytes changed but whose pixels did not is
// noise; a moved block grades medium or large by the thresholds alone; a
// size change is large yet still carries the overlap's metrics; added and
// removed goldens are listed, not scored, and alone they make the headline
// `new`; the worst modified image sets the headline; a PNG outside the globs
// is not a row; the four images per golden land under the path-as-name; no
// change still prints a report; and the exit code is 0 throughout.
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';
import { PNG } from './golden-diff/deps.mjs';
import { cell, globToRegExp } from './golden-diff.mjs';

const ENGINE = join(dirname(fileURLToPath(import.meta.url)), 'golden-diff.mjs');

// A w x h PNG, white, with a dark block at (bx, by) of size bs; `level` sets
// zlib compression so the same pixels can be written as different bytes.
function png(w, h, { bx = 20, by = 20, bs = 40, level = 6 } = {}) {
  const img = new PNG({ width: w, height: h });
  for (let i = 0; i < w * h; i++) img.data.set([255, 255, 255, 255], i * 4);
  for (let y = by; y < by + bs && y < h; y++) for (let x = bx; x < bx + bs && x < w; x++) img.data.set([30, 30, 30, 255], (y * w + x) * 4);
  return PNG.sync.write(img, { deflateLevel: level });
}

let root, scratch;
const git = (...a) => execFileSync('git', ['-C', root, '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', ...a], { stdio: ['ignore', 'pipe', 'pipe'] }).toString();
const put = (rel, bytes) => { mkdirSync(join(root, dirname(rel)), { recursive: true }); writeFileSync(join(root, rel), bytes); };
const commit = (msg) => { git('add', '-A'); git('commit', '-q', '-m', msg); };

function run(args = []) {
  const out = join(scratch, 'out');
  const json = join(scratch, 'report.json');
  rmSync(out, { recursive: true, force: true });
  const r = spawnSync(process.execPath, [ENGINE, '--base', 'main', '--out', out, '--json', json, ...args], { cwd: root, encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  return { md: r.stdout, out, ...JSON.parse(readFileSync(json, 'utf8')) };
}

const BASE = {
  'test/golden/same.png': png(100, 80),
  'test/golden/moved.png': png(120, 120, { bx: 10, by: 10, bs: 30 }),
  'test/golden/grown.png': png(100, 100),
  'test/golden/gone.png': png(50, 50),
  'test/golden/sub/deep.png': png(60, 60),
  'docs/shot.png': png(40, 40),
};

before(() => {
  root = mkdtempSync(join(tmpdir(), 'golden-diff-'));
  scratch = mkdtempSync(join(tmpdir(), 'golden-diff-out-'));
  git('init', '-q', '-b', 'main');
  for (const [rel, bytes] of Object.entries(BASE)) put(rel, bytes);
  commit('base');
});
after(() => { for (const d of [root, scratch]) rmSync(d, { recursive: true, force: true }); });
beforeEach(() => {
  git('checkout', '-q', '-f', 'main');
  git('clean', '-fdq');
  git('checkout', '-q', '-B', 'work');
});

test('globs: ** spans directories, * stays in one segment', () => {
  const m = (g, p) => globToRegExp(g).test(p);
  assert.ok(m('**/golden/**', 'test/golden/a.png'));
  assert.ok(m('**/golden/**', 'golden/a.png'));
  assert.ok(m('**/golden/**', 'a/b/golden/c/d.png'));
  assert.ok(!m('**/golden/**', 'test/goldens/a.png'));
  assert.ok(m('test/golden/*.png', 'test/golden/a.png'));
  assert.ok(!m('test/golden/*.png', 'test/golden/sub/a.png'));
  assert.ok(m('screenshots/goldens/**', 'screenshots/goldens/x-1280.png'));
});

test('no change: a report that says so, grade none, exit 0', () => {
  const r = run();
  assert.match(r.md, /^## Goldens moved by this PR: none/);
  assert.equal(r.headline.grade, 'none');
  assert.equal(r.headline.changed, 0);
});

test('same pixels in different bytes is noise with SSIM 1', () => {
  put('test/golden/same.png', png(100, 80, { level: 0 }));
  commit('re-encode');
  const r = run();
  const [i] = r.images;
  assert.equal(i.path, 'test/golden/same.png');
  assert.equal(i.grade, 'noise');
  assert.equal(i.diffFrac, 0);
  assert.equal(i.ssim, 1);
  assert.equal(i.regions, 0);
  assert.equal(r.headline.grade, 'noise');
  assert.match(r.md, /\| `test\/golden\/same` \| modified \| 100×80 \| 0\.0% \| 1\.000 \| 0 \| 0% \| noise \|/);
});

test('a moved block grades by the thresholds: medium between them, large below large-ssim', () => {
  put('test/golden/moved.png', png(120, 120, { bx: 60, by: 60, bs: 30 }));
  commit('move');
  const r = run(['--large-ssim', '0', '--medium-ssim', '1']);
  const [i] = r.images;
  assert.equal(i.grade, 'medium');
  assert.ok(i.ssim > 0 && i.ssim < 1, `ssim ${i.ssim}`);
  assert.ok(i.diffFrac > 0);
  assert.ok(i.regions >= 1);
  assert.ok(i.bboxUnionFrac > 0 && i.largestBboxFrac > 0);
  assert.equal(i.sizeChanged, false);
  assert.equal(r.headline.grade, 'medium');
  const big = run(['--large-ssim', '1', '--medium-ssim', '1']);
  assert.equal(big.images[0].grade, 'large');
  assert.equal(big.headline.grade, 'large');
  for (const k of ['old', 'new', 'diff', 'strip']) assert.ok(existsSync(join(r.out, `test__golden__moved-${k}.png`)), k);
  const strip = PNG.sync.read(readFileSync(join(r.out, 'test__golden__moved-strip.png')));
  assert.equal(strip.width, 120 * 3 + 16);
  assert.equal(strip.height, 120);
});

test('a size change is large and still reports the overlap metrics', () => {
  put('test/golden/grown.png', png(100, 140));
  commit('grow');
  const r = run();
  const [i] = r.images;
  assert.equal(i.grade, 'large');
  assert.equal(i.sizeChanged, true);
  assert.deepEqual(i.overlap, { width: 100, height: 100 });
  assert.equal(i.ssim, 1);
  assert.equal(i.diffFrac, 0);
  assert.match(r.md, /\| `test\/golden\/grown` \| modified \| 100×100 → 100×140 \| 0\.0% \| 1\.000 \| 0 \| 0% \| large \|/);
  const strip = PNG.sync.read(readFileSync(join(r.out, 'test__golden__grown-strip.png')));
  assert.equal(strip.height, 140);
});

test('added and removed are listed, not scored; alone they make the headline new', () => {
  put('test/golden/fresh.png', png(30, 30));
  rmSync(join(root, 'test/golden/gone.png'));
  commit('add and remove');
  const r = run();
  assert.equal(r.headline.grade, 'new');
  assert.equal(r.headline.changed, 2);
  assert.equal(r.headline.minSsim, null);
  const by = Object.fromEntries(r.images.map((i) => [i.path, i]));
  assert.equal(by['test/golden/fresh.png'].grade, 'new');
  assert.equal(by['test/golden/fresh.png'].ssim, undefined);
  assert.equal(by['test/golden/gone.png'].grade, 'removed');
  assert.match(r.md, /^## Goldens moved by this PR: new   \(1 added, 1 removed\)$/m);
  assert.match(r.md, /\| `test\/golden\/fresh` \| added \| 30×30 \| \| \| \| \| new \|/);
  assert.match(r.md, /\| `test\/golden\/gone` \| removed \| 50×50 \| \| \| \| \| removed \|/);
  assert.ok(existsSync(join(r.out, 'test__golden__fresh-new.png')));
  assert.ok(existsSync(join(r.out, 'test__golden__gone-old.png')));
  assert.ok(!existsSync(join(r.out, 'test__golden__gone-diff.png')));
});

test('the headline is the worst modified image; the table has one row per golden in order', () => {
  put('test/golden/same.png', png(100, 80, { level: 0 }));
  put('test/golden/grown.png', png(100, 140));
  put('test/golden/fresh.png', png(30, 30));
  commit('mixed');
  const r = run();
  assert.equal(r.headline.grade, 'large');
  assert.equal(r.headline.modified, 2);
  assert.equal(r.headline.added, 1);
  assert.equal(r.headline.minSsim, 1);
  assert.match(r.md, /^## Goldens moved by this PR: large   \(2 modified, 1 added · min SSIM 1\.000\)$/m);
  const lines = r.md.split('\n');
  assert.equal(lines[2], '| golden | change | size | pixels | SSIM | regions | box union | grade |');
  assert.equal(lines[3], '|---|---|---|---|---|---|---|---|');
  assert.deepEqual(lines.slice(4, 7).map((l) => l.split(' | ')[0].slice(2)),
    ['`test/golden/fresh`', '`test/golden/grown`', '`test/golden/same`']);
  assert.match(lines[8], /^large = SSIM < 0\.80 or a size change · medium = SSIM < 0\.95 · else noise \(pencil/);
});

test('globs select: a PNG outside them is not a row; a nested one under ** is', () => {
  put('docs/shot.png', png(40, 40, { level: 0 }));
  put('test/golden/sub/deep.png', png(60, 60, { bx: 5 }));
  commit('outside and nested');
  const r = run();
  assert.deepEqual(r.images.map((i) => i.path), ['test/golden/sub/deep.png']);
  assert.ok(existsSync(join(r.out, 'test__golden__sub__deep-strip.png')));
  const narrow = run(['--paths', 'docs/*.png']);
  assert.deepEqual(narrow.images.map((i) => i.path), ['docs/shot.png']);
});

test('a path with | and a backtick cannot forge a table row', () => {
  const wild = 'test/golden/a|b`c.png';
  put(wild, png(20, 20));
  commit('wild name');
  const r = run();
  assert.equal(r.images[0].path, wild);
  const row = r.md.split('\n').find((l) => l.includes('a\\|b'));
  assert.ok(row, r.md);
  assert.equal(row, '| ``test/golden/a\\|b`c`` | added | 20×20 | | | | | new |');
  assert.equal(row.split(/(?<!\\)\|/).length, 10, 'eight cells');
  assert.ok(existsSync(join(r.out, 'test__golden__a|b`c-new.png')));
  assert.equal(cell('x\ny\x00z'), '`x\uFFFDy\uFFFDz`');
  assert.equal(cell('`tick'), '`` `tick ``');
  assert.equal(cell('plain'), '`plain`');
});

test('--summary appends the report', () => {
  put('test/golden/same.png', png(100, 80, { level: 0 }));
  commit('re-encode');
  const summary = join(root, 'summary.md');
  writeFileSync(summary, 'before\n');
  const r = run(['--summary', summary]);
  assert.equal(readFileSync(summary, 'utf8'), `before\n${r.md}`);
});

test('a bad argument is exit 2 with a message', () => {
  const r = spawnSync(process.execPath, [ENGINE, '--base', 'main', '--large-ssim', 'two'], { cwd: root, encoding: 'utf8' });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /large-ssim/);
});
