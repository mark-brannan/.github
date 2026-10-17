// Tests for show-dont-tell.mjs. Run: node --test .github/scripts/show-dont-tell.test.mjs
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { globToRegExp, matchPaths, parseConfig, report, scanBody, scanTryIt, tryItBlock } from './show-dont-tell.mjs';

test('globs: ** crosses directories, * and ? do not', () => {
  const m = (g, p) => globToRegExp(g).test(p);
  assert.ok(m('ui/**', 'ui/a/b.tsx'));
  assert.ok(m('**/golden/**', 'x/y/golden/a.png'));
  assert.ok(m('**/golden/**', 'golden/a.png'));
  assert.ok(m('src/*.css', 'src/a.css'));
  assert.ok(!m('src/*.css', 'src/deep/a.css'));
  assert.ok(m('a?.md', 'ab.md'));
  assert.ok(!m('a?.md', 'a/.md'));
  assert.ok(!m('ui/**', 'other/ui/a.tsx'));
  assert.ok(m('a.b', 'a.b'));
  assert.ok(!m('a.b', 'axb'));
});

test('matchPaths returns only matching files', () => {
  assert.deepEqual(matchPaths(['ui/a.tsx', 'README.md', 'docs/x.md'], ['ui/**', 'docs/*.md']), ['ui/a.tsx', 'docs/x.md']);
  assert.deepEqual(matchPaths(['README.md'], ['ui/**']), []);
});

test('parseConfig reads paths and urls, tolerates comments and quotes', () => {
  const c = parseConfig('# rendered\npaths:\n  - "ui/**"   # the app\n  - docs/*.md\nurls:\n  - https://preview.example.com/\n');
  assert.deepEqual(c, { paths: ['ui/**', 'docs/*.md'], urls: ['https://preview.example.com/'] });
  assert.deepEqual(parseConfig(''), { paths: [], urls: [] });
});

test('parseConfig accepts unindented list items', () => {
  assert.deepEqual(parseConfig('paths:\n- web/**\n'), { paths: ['web/**'], urls: [] });
});

test('scanBody: an image only named in a code span or fence is not shown', () => {
  assert.equal(scanBody('Satisfied by an inline image (`![](..)`)').kind, null);
  assert.equal(scanBody('add `![](url)`').kind, null);
  assert.equal(scanBody('```\n![x](y.png)\n```').kind, null);
  assert.equal(scanBody('`code` then ![x](y.png)').kind, 'image');
  assert.equal(scanBody('<!-- drop a screenshot here: ![what it shows](url) -->').kind, null);
  assert.equal(scanBody('<!-- <img src="a.png"> -->').kind, null);
  assert.equal(scanBody('<!-- hint -->\n![x](y.png)').kind, 'image');
  const urls = ['https://preview.example.com/'];
  assert.equal(scanBody('urls: `https://preview.example.com/`', urls).kind, null);
  assert.equal(scanBody('```\nhttps://preview.example.com/\n```', urls).kind, null);
  assert.equal(scanBody('`code` then https://preview.example.com/', urls).kind, 'url');
  assert.equal(scanBody('<!-- https://preview.example.com/ -->', urls).kind, null);
});

test('scanBody: images count anywhere', () => {
  assert.equal(scanBody('hi ![shot](https://x.test/a.png)').kind, 'image');
  assert.equal(scanBody('<img width="3" src="a.png">').kind, 'image');
  assert.equal(scanBody('https://github.com/user-attachments/assets/1234-abcd').kind, 'image');
  assert.equal(scanBody('https://user-images.githubusercontent.com/1/a.png').kind, 'image');
});

test('scanBody: without urls, no link counts, the agent attribution link least of all', () => {
  assert.equal(scanBody('see https://pr-4.preview.example.com/x').kind, null);
  assert.equal(scanBody('🤖 Generated with [Claude Code](https://claude.com/claude-code)').kind, null);
  assert.equal(scanBody('see https://github.com/o/r/actions/runs/1').kind, null);
  assert.equal(scanBody('').kind, null);
  assert.equal(scanBody(null).kind, null);
});

test('scanBody: with urls, only those prefixes count', () => {
  const urls = ['https://preview.example.com/'];
  assert.equal(scanBody('https://other.example.org/x', urls).kind, null);
  assert.equal(scanBody('https://claude.com/claude-code', urls).kind, null);
  assert.equal(scanBody('see http://preview.example.com/pr/4', urls).kind, null);
  assert.equal(scanBody('https://preview.example.com/pr/4', urls).kind, 'url');
});

test('report: green with no match, green when shown, red when not', () => {
  assert.ok(report({ matched: [], shown: { kind: null }, urls: [] }).ok);
  assert.match(report({ matched: [], shown: { kind: null }, urls: [] }).md, /No rendered path touched/);
  assert.ok(report({ matched: ['ui/a'], shown: { kind: 'image', found: 'x' }, urls: [] }).ok);
  const red = report({ matched: ['ui/a'], shown: { kind: null, found: '' }, urls: [] });
  assert.ok(!red.ok);
  assert.match(red.md, /`ui\/a`/);
  assert.match(red.md, /inline image/);
  assert.doesNotMatch(red.md, /link starting with/);
  assert.match(report({ matched: ['ui/a'], shown: { kind: null, found: '' }, urls: ['https://p.example/'] }).md, /link starting with one of `https:\/\/p\.example\/`/);
});

test('parseConfig reads checkout, install, serve; lists still work', () => {
  const c = parseConfig('paths:\n  - ui/**\ncheckout: ~/searoom   # my clone\ninstall: "npm ci"\nserve: npm run dev -- --strictPort --port {port}\n');
  assert.deepEqual(c, { paths: ['ui/**'], urls: [], checkout: '~/searoom', install: 'npm ci', serve: 'npm run dev -- --strictPort --port {port}' });
  assert.equal(parseConfig('paths:\n  - a\n').serve, undefined);
  assert.equal(parseConfig('install:\npaths:\n  - a\n').install, '');
});

const ARGS = { repo: 'mark-brannan/searoom', pr: 221, head: '5c8568cdeadbeef', checkout: '~/searoom', install: 'npm ci', serve: 'npm run dev -- --strictPort --port {port}' };

test('tryItBlock renders the spec block exactly', () => {
  const want = [
    '## Try it',
    '',
    '    git -C ~/searoom fetch origin +pull/221/head:refs/pr/221 \\',
    '      && { [ -d ~/searoom/.claude/worktrees/pr-221 ] || git -C ~/searoom worktree add --detach ~/searoom/.claude/worktrees/pr-221 refs/pr/221; } \\',
    '      && git -C ~/searoom/.claude/worktrees/pr-221 checkout -q --detach refs/pr/221 \\',
    '      && cd ~/searoom/.claude/worktrees/pr-221 \\',
    '      && npm ci && npm run dev -- --strictPort --port 10221',
    '',
    'Serves PR #221 (head 5c8568c when this was written) on http://localhost:10221 from',
    'its own worktree. Paste again after a push to the PR: it fetches and moves the',
    'worktree. Remove: `git -C ~/searoom worktree remove --force ~/searoom/.claude/worktrees/pr-221`',
    '',
  ].join('\n');
  assert.equal(tryItBlock(ARGS), want);
});

test('tryItBlock omits install when empty and replaces every {port}', () => {
  const b = tryItBlock({ ...ARGS, install: '', serve: 'npx serve -l {port} --x {port}' });
  assert.match(b, /\n      && npx serve -l 10221 --x 10221\n/);
  assert.doesNotMatch(b, /npm ci/);
});

test('scanTryIt: heading plus pull/N/head after it, fences not stripped', () => {
  assert.ok(scanTryIt(tryItBlock(ARGS), 221));
  assert.ok(!scanTryIt(tryItBlock(ARGS), 22));
  assert.ok(!scanTryIt('no heading, pull/221/head', 221));
  assert.ok(!scanTryIt('pull/221/head\n\n## Try it\nnothing after', 221));
  assert.ok(scanTryIt('### try it'.replace('t', 'T') + '\n```\ngit fetch +pull/7/head\n```', 7));
  assert.ok(!scanTryIt('', 1));
});

test('report: with serve, red without a Try it block even when shown; green with it', () => {
  const block = tryItBlock(ARGS);
  const img = { kind: 'image', found: 'x' };
  const red = report({ matched: ['ui/a'], shown: img, urls: [], serve: ARGS.serve, tryIt: false, block });
  assert.ok(!red.ok);
  assert.match(red.md, /show-dont-tell: red/);
  assert.match(red.md, /## Try it/);
  assert.match(red.md, /pull\/221\/head/);
  const redNone = report({ matched: ['ui/a'], shown: { kind: null, found: '' }, urls: [], serve: ARGS.serve, tryIt: false, block });
  assert.ok(!redNone.ok);
  assert.match(redNone.md, /no picture/);
  assert.match(redNone.md, /## Try it/);
  assert.ok(report({ matched: ['ui/a'], shown: img, urls: [], serve: ARGS.serve, tryIt: true, block }).ok);
  assert.ok(report({ matched: [], shown: { kind: null }, urls: [], serve: ARGS.serve, tryIt: false, block }).ok);
  assert.ok(report({ matched: ['ui/a'], shown: img, urls: [] }).ok);
});
