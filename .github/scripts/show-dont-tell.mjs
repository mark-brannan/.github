// show-dont-tell: a pull request that touches a rendered path must show it.
// Node built-ins only. Used by workflows/show-dont-tell.yml.
//
//   node show-dont-tell.mjs <config-file>   (env: GH_TOKEN, REPO, PR)
//
// Exits 1 (red) when a changed file matches `paths:` and the PR body carries
// neither an image nor a preview URL, or when the opt-in file sets `serve:`
// and the body has no "Try it" block naming this PR's head; 0 otherwise.
// Writes markdown to stdout.
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// Glob -> RegExp. `**` crosses `/`, `*` and `?` do not; `**/` may match nothing.
export function globToRegExp(glob) {
  let re = '';
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === '*') {
      if (glob[i + 1] === '*') {
        i++;
        if (glob[i + 1] === '/') { i++; re += '(?:.*/)?'; } else re += '.*';
      } else re += '[^/]*';
    } else if (c === '?') re += '[^/]';
    else re += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${re}$`);
}

export function matchPaths(files, globs) {
  const res = globs.map(globToRegExp);
  return files.filter((f) => res.some((r) => r.test(f)));
}

// The opt-in file: top-level `paths:` and optional `urls:`, each a "- item"
// list, and optional scalars `checkout:`, `install:`, `serve:` (set only when
// present). Comments (#) and quotes are tolerated; nothing else is YAML.
export function parseConfig(text) {
  const out = { paths: [], urls: [] };
  let key = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/(^|\s)#.*$/, '').trimEnd();
    if (!line.trim()) continue;
    const scalar = /^(checkout|install|serve):\s*(.*)$/.exec(line);
    if (scalar) {
      out[scalar[1]] = scalar[2].trim().replace(/^(["'])(.*)\1$/, '$2');
      key = null;
      continue;
    }
    const head = /^(paths|urls):\s*$/.exec(line);
    if (head) { key = head[1]; continue; }
    const item = /^\s*-\s+(.*)$/.exec(line);
    if (item && key) out[key].push(item[1].trim().replace(/^(["'])(.*)\1$/, '$2'));
    else key = null;
  }
  return out;
}

const IMAGE = [
  /!\[[^\]]*\]\(\s*<?[^)\s>]+/,                              // markdown image
  /<img\b[^>]*\bsrc\s*=/i,                                    // html image
  /https:\/\/github\.com\/user-attachments\/(?:assets|files)\/[\w-]+/, // dragged-in upload
  /https:\/\/user-images\.githubusercontent\.com\/[^\s)"'>]+/, // legacy upload
];

// What the body shows: {kind: 'image'|'url'|null, found}. A URL counts only
// when `urls` names its prefix: "any https link" was satisfied by the agent
// attribution link (https://claude.com/claude-code) in every agent-written body.
export function scanBody(body, urls = []) {
  const text = body || '';
  // Code spans, fences and HTML comments (a template's "drop a screenshot
  // here" hint) only describe a picture; they do not show one.
  const shown = text.replace(/```[\s\S]*?```|`[^`\n]*`|<!--[\s\S]*?-->/g, '');
  for (const re of IMAGE) {
    const m = re.exec(shown);
    if (m) return { kind: 'image', found: m[0] };
  }
  const all = urls.length ? shown.match(/https:\/\/[^\s)<>"'\]]+/g) || [] : [];
  const hit = all.find((u) => urls.some((p) => u.startsWith(p)));
  return hit ? { kind: 'url', found: hit } : { kind: null, found: '' };
}

// The "Try it" block every PR of a repo with `serve:` carries: one paste that
// fetches the PR head onto refs/pr/N (never a branch, so a second paste cannot
// hit "refusing to fetch into checked-out branch"), adds or moves a detached
// worktree, installs and serves on port 10000 + PR number.
export function tryItBlock({ repo, pr, head, checkout, install, serve }) {
  const port = 10000 + Number(pr);
  const wt = `${checkout}/.claude/worktrees/pr-${pr}`;
  const cmd = serve.replaceAll('{port}', String(port));
  const run = [install, cmd].filter(Boolean).join(' && ');
  return [
    '## Try it',
    '',
    `    git -C ${checkout} fetch origin +pull/${pr}/head:refs/pr/${pr} \\`,
    `      && { [ -d ${wt} ] || git -C ${checkout} worktree add --detach ${wt} refs/pr/${pr}; } \\`,
    `      && git -C ${wt} checkout -q --detach refs/pr/${pr} \\`,
    `      && cd ${wt} \\`,
    `      && ${run}`,
    '',
    `Serves PR #${pr} (head ${String(head).slice(0, 7)} when this was written) on http://localhost:${port} from`,
    'its own worktree. Paste again after a push to the PR: it fetches and moves the',
    `worktree. Remove: \`git -C ${checkout} worktree remove --force ${wt}\``,
    '',
  ].join('\n');
}

// Does the body carry a "Try it" heading followed by text naming this PR's
// head ref? Fences are not stripped: the command is the deliverable.
export function scanTryIt(body, pr) {
  const m = /^#{1,6}\s*Try it\b/m.exec(body || '');
  return !!m && (body || '').slice(m.index + m[0].length).includes(`pull/${pr}/head`);
}

export function report({ matched, shown, urls, serve = '', tryIt = true, block = '' }) {
  if (!matched.length) return { ok: true, md: '### show-dont-tell\n\nNo rendered path touched: nothing to show.\n' };
  const list = matched.slice(0, 20).map((f) => `- \`${f}\``).join('\n') + (matched.length > 20 ? `\n- ... and ${matched.length - 20} more` : '');
  const tryItRed = serve && !tryIt;
  const tryItMsg = tryItRed ? `This repository declares a \`serve:\` command, so the description must also carry a "Try it" block: a \`## Try it\` heading followed by a command containing \`pull/N/head\` for this pull request, so the reviewer can run the page in one paste. Add this to the description (the check re-runs when it is edited):\n\n\`\`\`\`markdown\n${block}\`\`\`\`\n` : '';
  const kind = shown.kind === 'image' ? 'an image' : 'a preview URL';
  // `why` is the one-line reason for the red, for the ::error:: annotation.
  if (shown.kind && tryItRed) return { ok: false, why: 'has no "Try it" block', md: `### show-dont-tell: red\n\nThis pull request touches rendered paths:\n\n${list}\n\nThe body shows ${kind}, but has no "Try it" block.\n\n${tryItMsg}` };
  if (shown.kind) return { ok: true, md: `### show-dont-tell\n\nRendered paths touched (${matched.length}) and the body shows ${kind}: \`${shown.found}\`\n\n${list}\n` };
  const what = urls.length ? ` or a link starting with one of ${urls.map((u) => `\`${u}\``).join(', ')}` : '';
  const why = tryItRed ? 'has neither an image/preview URL nor a "Try it" block' : 'has no image or preview URL';
  return { ok: false, why, md: `### show-dont-tell: red\n\nThis pull request touches rendered paths:\n\n${list}\n\nIts description carries no picture. Edit the description to add an inline image (\`![what it shows](url)\`, or drag a screenshot in)${what}; the check re-runs when the description is edited.\n${tryItRed ? `\n${tryItMsg}` : ''}` };
}

function gh(...args) { return execFileSync('gh', args, { encoding: 'utf8', maxBuffer: 1 << 28 }); }

function main() {
  const [cfgPath] = process.argv.slice(2);
  const { REPO, PR, BASE_SHA } = process.env;
  // The opt-in file is read at the PR's base, so a PR cannot exempt itself by
  // deleting or emptying it; a change to it takes effect once it merges.
  let text = null;
  if (BASE_SHA) {
    try {
      text = gh('api', `repos/${REPO}/contents/${cfgPath}?ref=${BASE_SHA}`, '-H', 'Accept: application/vnd.github.raw');
    } catch (e) {
      if (!/HTTP 404/.test(String(e.stderr || e.message))) throw e;
    }
  } else if (existsSync(cfgPath)) text = readFileSync(cfgPath, 'utf8');
  if (text === null) {
    console.log(`### show-dont-tell\n\nNo \`${cfgPath}\` in this repository${BASE_SHA ? ' at the base commit' : ''}: not opted in, nothing checked.`);
    return 0;
  }
  const cfg = parseConfig(text);
  if (!cfg.paths.length) {
    console.log(`### show-dont-tell\n\n\`${cfgPath}\` lists no \`paths:\`: nothing checked.`);
    return 0;
  }
  const files = gh('api', `repos/${REPO}/pulls/${PR}/files`, '--paginate', '--jq', '.[] | .filename, (.previous_filename // empty)').split('\n').filter(Boolean);
  const matched = matchPaths(files, cfg.paths);
  // One read of the pull: the body the check judges and the head SHA the
  // block names come from the same response.
  const pull = matched.length ? JSON.parse(gh('api', `repos/${REPO}/pulls/${PR}`)) : {};
  const body = pull.body || '';
  const serve = cfg.serve || '';
  const tryIt = !serve || !matched.length || scanTryIt(body, PR);
  let block = '';
  if (serve && matched.length && !tryIt) {
    block = tryItBlock({ repo: REPO, pr: PR, head: pull.head.sha, checkout: cfg.checkout || '~/' + String(REPO).split('/').pop(), install: cfg.install || '', serve });
  }
  const r = report({ matched, shown: scanBody(body, cfg.urls), urls: cfg.urls, serve, tryIt, block });
  console.log(r.md);
  if (!r.ok) console.error(`::error::show-dont-tell: rendered paths touched, but the PR description ${r.why}.`);
  return r.ok ? 0 : 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) process.exit(main());
