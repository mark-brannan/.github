// show-dont-tell: a pull request that touches a rendered path must show it.
// Node built-ins only. Used by workflows/show-dont-tell.yml.
//
//   node show-dont-tell.mjs <config-file>   (env: GH_TOKEN, REPO, PR)
//
// Exits 1 (red) when a changed file matches `paths:` and the PR body carries
// neither an image nor a preview URL; 0 otherwise. Writes markdown to stdout.
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
// list. Comments (#) and quotes are tolerated; nothing else is YAML.
export function parseConfig(text) {
  const out = { paths: [], urls: [] };
  let key = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/(^|\s)#.*$/, '').trimEnd();
    if (!line.trim()) continue;
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

// What the body shows: {kind: 'image'|'url'|null, found}. Without `urls`, any
// https URL not on github.com counts; with it, only URLs starting with a prefix.
export function scanBody(body, urls = []) {
  const text = body || '';
  // Code spans and fences only describe a picture; they do not show one.
  const shown = text.replace(/```[\s\S]*?```|`[^`\n]*`/g, '');
  for (const re of IMAGE) {
    const m = re.exec(shown);
    if (m) return { kind: 'image', found: m[0] };
  }
  const all = text.match(/https:\/\/[^\s)<>"'\]]+/g) || [];
  const hit = all.find((u) => (urls.length
    ? urls.some((p) => u.startsWith(p))
    : !/^https:\/\/(?:[\w-]+\.)*github\.com(?:[/:]|$)/i.test(u)));
  return hit ? { kind: 'url', found: hit } : { kind: null, found: '' };
}

export function report({ matched, shown, urls }) {
  if (!matched.length) return { ok: true, md: '### show-dont-tell\n\nNo rendered path touched: nothing to show.\n' };
  const list = matched.slice(0, 20).map((f) => `- \`${f}\``).join('\n') + (matched.length > 20 ? `\n- ... and ${matched.length - 20} more` : '');
  if (shown.kind) return { ok: true, md: `### show-dont-tell\n\nRendered paths touched (${matched.length}) and the body shows ${shown.kind === 'image' ? 'an image' : 'a preview URL'}: \`${shown.found}\`\n\n${list}\n` };
  const what = urls.length ? `a link starting with one of ${urls.map((u) => `\`${u}\``).join(', ')}` : 'a link to a preview outside github.com';
  return { ok: false, md: `### show-dont-tell: red\n\nThis pull request touches rendered paths:\n\n${list}\n\nIts description carries no picture. Edit the description to add an inline image (\`![what it shows](url)\`, or drag a screenshot in) or ${what}; the check re-runs when the description is edited.\n` };
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
  const body = matched.length ? gh('api', `repos/${REPO}/pulls/${PR}`, '--jq', '.body // ""') : '';
  const r = report({ matched, shown: scanBody(body, cfg.urls), urls: cfg.urls });
  console.log(r.md);
  if (!r.ok) console.error('::error::show-dont-tell: rendered paths touched, but the PR description has no image or preview URL.');
  return r.ok ? 0 : 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) process.exit(main());
