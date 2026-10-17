#!/usr/bin/env node
// How far each golden image a pull request re-baselines moved, scored, so a
// reviewer can tell a redesign from pixel noise without opening the images.
// Driven by .github/workflows/golden-diff.yml; runs anywhere there is a git
// checkout. A measurement, not a gate: exits 0 whatever it finds.
//
//   node golden-diff.mjs --base <sha-or-ref> [--paths '<glob> <glob>...']
//        [--out <dir>] [--json <file>] [--summary <file>]
//        [--large-ssim 0.80] [--medium-ssim 0.95]
//
// Changed PNGs are those between merge-base(base, HEAD) and HEAD whose
// repo-relative path matches one of the globs (default: any golden/ or
// goldens/ directory). Old bytes come from `git show`, new from the working
// tree. For each modified pair it writes <path>-old.png, <path>-new.png,
// <path>-diff.png (pixelmatch's red-on-dimmed picture) and <path>-strip.png
// (old | new | diff side by side) to --out, with the path's slashes turned
// into `__` since golden names repeat across directories. The markdown
// report goes to stdout and, with --summary, is appended to that file;
// --json writes every metric per image plus the headline.
//
// Scoring reproduces the spike in claude_prompts_scratch
// (drafts/2026-10-17-goldens-score-spike.md), parameters and all, so its
// measured numbers carry over: pixelmatch 8.0.0 at threshold 0.1 with
// includeAA false; ssim.js 3.5.0 with default options (mssim); the diff mask
// dilated 2 px (square), its 8-connected components counted, the fraction of
// the image under the union of their bounding boxes and under the largest
// one. SSIM separated the re-baselines in that spike best, so SSIM grades:
// `large` below --large-ssim or on any size change, `medium` below
// --medium-ssim, else `noise`. A size-changed pair is still scored on its
// top-aligned overlap (common width x min height, no resampling) so the
// table shows how much of what is still there moved. The defaults 0.80 and
// 0.95 are pencil guesses from that one spike.
//
// Dependencies (pngjs, pixelmatch, ssim.js) are pinned in
// ./golden-diff/package.json and reach this file through
// ./golden-diff/deps.mjs, which Node resolves against that directory's
// node_modules: `npm ci` there is the whole setup.
import { execFileSync } from 'node:child_process';
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { PNG, pixelmatch, ssim } from './golden-diff/deps.mjs';

const PM_THRESHOLD = 0.1;
const DILATE = 2;
const DEFAULT_PATHS = '**/golden/** **/goldens/**';
const DEFAULT_LARGE = 0.80;
const DEFAULT_MEDIUM = 0.95;

// --- CLI -------------------------------------------------------------------

function parseArgs(argv) {
  const opts = { base: null, paths: DEFAULT_PATHS, out: 'out/golden-diff', json: null, summary: null,
    largeSsim: DEFAULT_LARGE, mediumSsim: DEFAULT_MEDIUM };
  const keys = { '--base': 'base', '--paths': 'paths', '--out': 'out', '--json': 'json', '--summary': 'summary',
    '--large-ssim': 'largeSsim', '--medium-ssim': 'mediumSsim' };
  for (let i = 0; i < argv.length; i++) {
    const k = keys[argv[i]];
    if (!k) throw new Error(`unknown argument ${argv[i]}`);
    if (i + 1 >= argv.length) throw new Error(`${argv[i]} needs a value`);
    opts[k] = argv[++i];
  }
  if (!opts.base) throw new Error('--base <sha-or-ref> is required');
  for (const k of ['largeSsim', 'mediumSsim']) {
    opts[k] = Number(opts[k]);
    if (!(opts[k] >= 0 && opts[k] <= 1)) throw new Error(`--${k === 'largeSsim' ? 'large' : 'medium'}-ssim must be a number in [0, 1]`);
  }
  return opts;
}

// A glob to a RegExp over repo-relative paths: `**` spans directories (`**/`
// also matches none), `*` and `?` stay within one path segment. Node 22 has
// path.matchesGlob; the runner may have 20, and this is all that is needed.
export function globToRegExp(glob) {
  let re = '';
  const g = glob.replace(/^\.\//, '');
  for (let i = 0; i < g.length; i++) {
    const c = g[i];
    if (c === '*') {
      if (g[i + 1] === '*') {
        i++;
        if (g[i + 1] === '/') { i++; re += '(?:.*/)?'; } else re += '.*';
      } else re += '[^/]*';
    } else if (c === '?') re += '[^/]';
    else re += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${re}$`);
}

// --- git -------------------------------------------------------------------

function git(root, args, opts = {}) {
  return execFileSync('git', ['-C', root, ...args], { maxBuffer: 256 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'], ...opts });
}

function changedPngs(root, mergeBase, globs) {
  const res = globs.map(globToRegExp);
  const out = git(root, ['diff', '--name-status', '--no-renames', '-z', mergeBase, 'HEAD']).toString();
  const parts = out.split('\0');
  const rows = [];
  for (let i = 0; i + 1 < parts.length; i += 2) {
    const status = parts[i][0], path = parts[i + 1];
    if (!/\.png$/i.test(path) || !res.some((r) => r.test(path))) continue;
    if (status === 'A') rows.push({ path, status: 'added' });
    else if (status === 'D') rows.push({ path, status: 'removed' });
    else if (status === 'M' || status === 'T') rows.push({ path, status: 'modified' });
  }
  return rows;
}

// --- scoring (the spike's algorithm, unchanged) ------------------------------

// Dilate a binary mask by r px (Chebyshev / square structuring element).
function dilate(mask, w, h, r) {
  const out = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    if (!mask[y * w + x]) continue;
    const y0 = Math.max(0, y - r), y1 = Math.min(h - 1, y + r);
    const x0 = Math.max(0, x - r), x1 = Math.min(w - 1, x + r);
    for (let yy = y0; yy <= y1; yy++) out.fill(1, yy * w + x0, yy * w + x1 + 1);
  }
  return out;
}

// 8-connected components; returns [{pixels, x0, y0, x1, y1}] (bbox inclusive).
function components(mask, w, h) {
  const seen = new Uint8Array(w * h);
  const regions = [];
  const stack = new Int32Array(w * h);
  for (let start = 0; start < w * h; start++) {
    if (!mask[start] || seen[start]) continue;
    let sp = 0; stack[sp++] = start; seen[start] = 1;
    const r = { pixels: 0, x0: w, y0: h, x1: -1, y1: -1 };
    while (sp > 0) {
      const i = stack[--sp];
      const x = i % w, y = (i - x) / w;
      r.pixels++;
      if (x < r.x0) r.x0 = x; if (x > r.x1) r.x1 = x;
      if (y < r.y0) r.y0 = y; if (y > r.y1) r.y1 = y;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        if (!dx && !dy) continue;
        const nx = x + dx, ny = y + dy;
        if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue;
        const j = ny * w + nx;
        if (mask[j] && !seen[j]) { seen[j] = 1; stack[sp++] = j; }
      }
    }
    regions.push(r);
  }
  return regions;
}

function bboxUnionArea(regions, w, h) {
  const paint = new Uint8Array(w * h);
  for (const r of regions) for (let y = r.y0; y <= r.y1; y++) paint.fill(1, y * w + r.x0, y * w + r.x1 + 1);
  let n = 0; for (let i = 0; i < paint.length; i++) n += paint[i];
  return n;
}

// Top-aligned crop of an image to w x h (no resampling).
function cropTop(img, w, h) {
  const out = new PNG({ width: w, height: h });
  for (let y = 0; y < h; y++) img.data.copy(out.data, y * w * 4, y * img.width * 4, y * img.width * 4 + w * 4);
  return out;
}

// Metrics for two images of one size, plus pixelmatch's picture of the diff.
export function scoreSameSize(a, b) {
  const { width: w, height: h } = a;
  const total = w * h;
  const mask = new PNG({ width: w, height: h });
  const diffPixels = pixelmatch(a.data, b.data, mask.data, w, h, { threshold: PM_THRESHOLD, includeAA: false, diffMask: true });
  const bits = new Uint8Array(total);
  for (let i = 0; i < total; i++) bits[i] = mask.data[i * 4 + 3] > 0 ? 1 : 0;
  const regs = components(dilate(bits, w, h, DILATE), w, h);
  const union = regs.length ? bboxUnionArea(regs, w, h) : 0;
  const bboxArea = (r) => (r.x1 - r.x0 + 1) * (r.y1 - r.y0 + 1);
  const largest = regs.length ? Math.max(...regs.map(bboxArea)) : 0;
  const { mssim } = ssim({ data: a.data, width: w, height: h }, { data: b.data, width: w, height: h });
  const picture = new PNG({ width: w, height: h });
  pixelmatch(a.data, b.data, picture.data, w, h, { threshold: PM_THRESHOLD, includeAA: false });
  return { diffPixels, diffFrac: diffPixels / total, ssim: mssim, regions: regs.length,
    bboxUnionFrac: union / total, largestBboxFrac: largest / total, picture };
}

// Score old against new; a size change is scored on the top-aligned overlap.
export function scorePair(oldPng, newPng) {
  const sizeChanged = oldPng.width !== newPng.width || oldPng.height !== newPng.height;
  if (!sizeChanged) return { sizeChanged, overlap: null, ...scoreSameSize(oldPng, newPng) };
  const w = Math.min(oldPng.width, newPng.width), h = Math.min(oldPng.height, newPng.height);
  return { sizeChanged, overlap: { width: w, height: h }, ...scoreSameSize(cropTop(oldPng, w, h), cropTop(newPng, w, h)) };
}

export function grade(score, largeSsim, mediumSsim) {
  if (score.sizeChanged || score.ssim < largeSsim) return 'large';
  if (score.ssim < mediumSsim) return 'medium';
  return 'noise';
}

// --- images ----------------------------------------------------------------

const GUTTER = 8;

function blit(dst, src, x0) {
  for (let y = 0; y < src.height; y++) src.data.copy(dst.data, (y * dst.width + x0) * 4, y * src.width * 4, (y + 1) * src.width * 4);
}

// old | new | diff in one picture, gutters between, background light grey.
function strip(oldPng, newPng, diffPng) {
  const panes = [oldPng, newPng, diffPng];
  const w = panes.reduce((n, p) => n + p.width, 0) + GUTTER * (panes.length - 1);
  const h = Math.max(...panes.map((p) => p.height));
  const out = new PNG({ width: w, height: h });
  for (let i = 0; i < w * h; i++) out.data.set([235, 235, 235, 255], i * 4);
  let x = 0;
  for (const p of panes) {
    blit(out, p, x);
    x += p.width;
    if (p !== diffPng) {
      for (let y = 0; y < h; y++) for (let g = 0; g < GUTTER; g++) out.data.set([128, 128, 128, 255], (y * w + x + g) * 4);
      x += GUTTER;
    }
  }
  return out;
}

// Output file name for a golden: its path with `/` as `__`; control characters
// and backslashes, legal in a git path, become `_` on disk.
const fileStem = (path) => path.replace(/\.png$/i, '').split('/').join('__').replace(/[\x00-\x1f\x7f\\]/g, '_');

// --- report ----------------------------------------------------------------

const RANK = { large: 3, medium: 2, noise: 1 };

// A file name or message as one markdown table cell: control characters
// replaced, `|` escaped, the whole in a code span fenced longer than any
// backtick run inside it, so a path in the PR cannot forge a row or markup.
export function cell(text) {
  const clean = String(text).replace(/[\x00-\x1f\x7f]/g, '\uFFFD').replace(/\|/g, '\\|');
  const runs = (clean.match(/`+/g) || []).map((r) => r.length);
  const fence = '`'.repeat(Math.max(0, ...runs) + 1);
  const pad = clean.startsWith('`') || clean.endsWith('`') ? ' ' : '';
  return `${fence}${pad}${clean}${pad}${fence}`;
}
const fmtSize = (p) => `${p.width}×${p.height}`;
const pct = (x, d) => `${(100 * x).toFixed(d)}%`;
const f3 = (x) => x.toFixed(3);

export function headlineOf(images) {
  const modified = images.filter((i) => i.status === 'modified');
  const counts = { modified: modified.length,
    added: images.filter((i) => i.status === 'added').length,
    removed: images.filter((i) => i.status === 'removed').length };
  let grade = 'none';
  if (modified.length) grade = modified.reduce((g, i) => (RANK[i.grade] > RANK[g] ? i.grade : g), 'noise');
  else if (images.length) grade = 'new';
  const ssims = modified.map((i) => i.ssim).filter((s) => typeof s === 'number');
  return { grade, minSsim: ssims.length ? Math.min(...ssims) : null, changed: images.length, ...counts };
}

export function report(images, headline, opts) {
  const lines = [];
  if (!images.length) {
    lines.push('## Goldens moved by this PR: none', '',
      `No PNG under ${cell(opts.paths)} differs from the merge base.`, '');
    return lines.join('\n');
  }
  const counts = ['modified', 'added', 'removed'].filter((k) => headline[k]).map((k) => `${headline[k]} ${k}`).join(', ');
  const tail = headline.minSsim === null ? '' : ` · min SSIM ${f3(headline.minSsim)}`;
  lines.push(`## Goldens moved by this PR: ${headline.grade}   (${counts}${tail})`, '',
    '| golden | change | size | pixels | SSIM | regions | box union | grade |', '|---|---|---|---|---|---|---|---|');
  for (const i of images) {
    const name = cell(i.path.replace(/\.png$/i, ''));
    if (i.status === 'modified' && i.error) {
      lines.push(`| ${name} | modified | unreadable: ${cell(i.error)} | | | | | ${i.grade} |`);
    } else if (i.status === 'modified') {
      const size = i.sizeChanged ? `${fmtSize(i.old)} → ${fmtSize(i.new)}` : fmtSize(i.new);
      lines.push(`| ${name} | modified | ${size} | ${pct(i.diffFrac, 1)} | ${f3(i.ssim)} | ${i.regions} | ${pct(i.bboxUnionFrac, 0)} | ${i.grade} |`);
    } else {
      const dims = i.status === 'added' ? i.new : i.old;
      lines.push(`| ${name} | ${i.status} | ${dims ? fmtSize(dims) : ''} | | | | | ${i.grade} |`);
    }
  }
  lines.push('', `large = SSIM < ${opts.largeSsim.toFixed(2)} or a size change · medium = SSIM < ${opts.mediumSsim.toFixed(2)} · else noise`
    + ' (pencil thresholds from one spike; the workflow\'s large-ssim / medium-ssim inputs move them)', '');
  return lines.join('\n');
}

// --- main ------------------------------------------------------------------

export function run(opts) {
  const root = git(process.cwd(), ['rev-parse', '--show-toplevel']).toString().trim();
  const mergeBase = git(root, ['merge-base', opts.base, 'HEAD']).toString().trim();
  const globs = opts.paths.split(/\s+/).filter(Boolean);
  const images = [];
  mkdirSync(opts.out, { recursive: true });
  const dims = (p) => ({ width: p.width, height: p.height });
  for (const { path, status } of changedPngs(root, mergeBase, globs)) {
    const stem = fileStem(path);
    const row = { path, status, grade: status === 'added' ? 'new' : status === 'removed' ? 'removed' : null, old: null, new: null };
    const oldBytes = status === 'added' ? null : git(root, ['show', `${mergeBase}:${path}`]);
    const newBytes = status === 'removed' ? null : readFileSync(join(root, path));
    if (oldBytes) writeFileSync(join(opts.out, `${stem}-old.png`), oldBytes);
    if (newBytes) writeFileSync(join(opts.out, `${stem}-new.png`), newBytes);
    try {
      const oldPng = oldBytes && PNG.sync.read(oldBytes);
      const newPng = newBytes && PNG.sync.read(newBytes);
      if (oldPng) row.old = dims(oldPng);
      if (newPng) row.new = dims(newPng);
      if (status === 'modified') {
        const { picture, ...score } = scorePair(oldPng, newPng);
        Object.assign(row, score);
        row.grade = grade(score, opts.largeSsim, opts.mediumSsim);
        writeFileSync(join(opts.out, `${stem}-diff.png`), PNG.sync.write(picture));
        writeFileSync(join(opts.out, `${stem}-strip.png`), PNG.sync.write(strip(oldPng, newPng, picture)));
      }
    } catch (e) {
      // A PNG that will not decode is a change nobody can look at: graded large, named in the table.
      row.error = e.message.split('\n')[0];
      if (status === 'modified') row.grade = 'large';
    }
    images.push(row);
  }
  images.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  const headline = headlineOf(images);
  const md = report(images, headline, opts);
  if (opts.json) {
    writeFileSync(opts.json, JSON.stringify({ base: opts.base, mergeBase, paths: globs,
      thresholds: { largeSsim: opts.largeSsim, mediumSsim: opts.mediumSsim }, headline, images }, null, 2));
  }
  if (opts.summary) appendFileSync(opts.summary, md);
  return { md, headline, images };
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (e) {
    console.error(`golden-diff: ${e.message}`);
    process.exit(2);
  }
  const { md, images } = run(opts);
  process.stdout.write(md);
  console.error(`golden-diff: ${images.length} changed golden(s); images in ${opts.out}`);
}
