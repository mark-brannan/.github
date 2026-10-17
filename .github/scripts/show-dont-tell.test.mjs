// Tests for show-dont-tell.mjs. Run: node --test .github/scripts/show-dont-tell.test.mjs
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { globToRegExp, matchPaths, parseConfig, report, scanBody } from './show-dont-tell.mjs';

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
});

test('scanBody: images count anywhere', () => {
  assert.equal(scanBody('hi ![shot](https://x.test/a.png)').kind, 'image');
  assert.equal(scanBody('<img width="3" src="a.png">').kind, 'image');
  assert.equal(scanBody('https://github.com/user-attachments/assets/1234-abcd').kind, 'image');
  assert.equal(scanBody('https://user-images.githubusercontent.com/1/a.png').kind, 'image');
});

test('scanBody: default preview URL is any https link off github.com', () => {
  assert.equal(scanBody('see https://pr-4.preview.example.com/x').kind, 'url');
  assert.equal(scanBody('see https://github.com/o/r/actions/runs/1').kind, null);
  assert.equal(scanBody('see https://gist.github.com/o/1').kind, null);
  assert.equal(scanBody('see http://insecure.example.com').kind, null);
  assert.equal(scanBody('').kind, null);
  assert.equal(scanBody(null).kind, null);
});

test('scanBody: with urls, only those prefixes count', () => {
  const urls = ['https://preview.example.com/'];
  assert.equal(scanBody('https://other.example.org/x', urls).kind, null);
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
});
