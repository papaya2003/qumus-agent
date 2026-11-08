// A small Markdown renderer for the conversation (no build step, no CDN).
//
// The agents write Markdown; nothing they write is ever parsed as HTML. The
// renderer builds DOM nodes from a subset of CommonMark + GFM:
//
//   blocks   paragraphs, # headings, > quotes, - / * / + and 1. lists (nested by
//            indentation, [ ] task items), ``` fenced and 4-space code, --- rules,
//            | pipe | tables | (with :--- alignment)
//   inline   `code`, **bold**, *italic*, ~~struck~~, [links](url), <https://autolinks>,
//            bare https://links, \escapes; a single newline is a line break
//
// Links keep only http(s), mailto and same-site (/, #, ./) targets; anything else
// (javascript:, data:, vbscript:) is shown as plain text. Images are not loaded:
// an image becomes a link to its source. Literal HTML stays literal text.

const SAFE_URL = /^(https?:|mailto:|\/(?!\/)|#|\.\/)/i;

function el(tag, cls) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  return n;
}

// ------------------------------------------------------------------ inline
function safeHref(url) {
  const u = String(url || '').trim().replace(/^<|>$/g, '');
  return SAFE_URL.test(u) ? u : null;
}

// `plain`: the label is shown as it is (an autolink's own address)
function linkNode(text, url, title, plain = false) {
  const href = safeHref(url);
  if (!href) return document.createTextNode(text);
  const a = el('a');
  a.href = href;
  if (title) a.title = title;
  if (/^https?:|^mailto:/i.test(href)) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
  if (plain) a.textContent = text; else a.append(...inline(text));
  return a;
}

// Find the closing `)` of a link destination starting at `i` (just after `(`),
// honouring balanced parentheses; returns [url, title, end] or null.
function linkDest(s, i) {
  let depth = 0, j = i;
  for (; j < s.length; j++) {
    const c = s[j];
    if (c === '\\') { j++; continue; }
    if (c === '(') depth++;
    else if (c === ')') { if (!depth) break; depth--; }
    else if (c === '\n') return null;
  }
  if (j >= s.length) return null;
  const body = s.slice(i, j).trim();
  const m = /^(\S+?)(?:\s+("([^"]*)"|'([^']*)'))?$/.exec(body) || /^(<[^>]*>)(?:\s+"([^"]*)")?$/.exec(body);
  if (!m && body) return [body, '', j + 1];
  return [m ? m[1] : '', (m && (m[3] ?? m[4] ?? m[2])) || '', j + 1];
}

// The matching `]` for a `[` at `i`, allowing nested brackets; -1 if none.
function closeBracket(s, i) {
  let depth = 0;
  for (let j = i; j < s.length; j++) {
    const c = s[j];
    if (c === '\\') { j++; continue; }
    if (c === '`') {  // skip code spans
      const run = /^`+/.exec(s.slice(j))[0];
      const end = s.indexOf(run, j + run.length);
      if (end > 0) { j = end + run.length - 1; continue; }
    }
    if (c === '[') depth++;
    else if (c === ']') { depth--; if (!depth) return j; }
  }
  return -1;
}

// Emphasis: a run of * or _ opens when followed by non-space (and for _, not
// inside a word); closes when preceded by non-space.
function emphasisEnd(s, i, ch, n) {
  const mark = ch.repeat(n);
  let j = i + n;
  while ((j = s.indexOf(mark, j)) >= 0) {
    const before = s[j - 1], after = s[j + n];
    const closes = before && !/\s/.test(before) && (ch === '*' || !after || !/\w/.test(after));
    if (closes && s[j - 1] !== '\\') return j;
    j += n;
  }
  return -1;
}

export function inline(src) {
  const s = String(src || '');
  const out = [];
  let text = '';
  const flush = () => { if (text) { out.push(document.createTextNode(text)); text = ''; } };
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === '\\' && i + 1 < s.length && /[\\`*_{}[\]()#+\-.!|~<>]/.test(s[i + 1])) { text += s[i + 1]; i++; continue; }
    if (c === '`') {
      const run = /^`+/.exec(s.slice(i))[0];
      const end = s.indexOf(run, i + run.length);
      if (end > 0) {
        flush();
        const code = el('code');
        code.textContent = s.slice(i + run.length, end).replace(/\n/g, ' ').replace(/^ (.+) $/, '$1');
        out.push(code);
        i = end + run.length - 1;
        continue;
      }
    }
    if ((c === '*' || c === '_') && (i === 0 || !(c === '_' && /\w/.test(s[i - 1])))) {
      const n = s[i + 1] === c ? 2 : 1;
      const after = s[i + n];
      if (after && !/\s/.test(after) && after !== c) {
        const end = emphasisEnd(s, i, c, n);
        if (end > i + n) {
          flush();
          const node = el(n === 2 ? 'strong' : 'em');
          node.append(...inline(s.slice(i + n, end)));
          out.push(node);
          i = end + n - 1;
          continue;
        }
      }
    }
    if (c === '~' && s[i + 1] === '~' && s[i + 2] && !/\s/.test(s[i + 2])) {
      const end = s.indexOf('~~', i + 2);
      if (end > i + 2) {
        flush();
        const node = el('del');
        node.append(...inline(s.slice(i + 2, end)));
        out.push(node);
        i = end + 1;
        continue;
      }
    }
    if (c === '[' || (c === '!' && s[i + 1] === '[')) {
      const start = c === '!' ? i + 1 : i;
      const close = closeBracket(s, start);
      if (close > 0 && s[close + 1] === '(') {
        const dest = linkDest(s, close + 2);
        if (dest) {
          flush();
          const label = s.slice(start + 1, close);
          out.push(linkNode(label || dest[0], dest[0], dest[1]));
          i = dest[2] - 1;
          continue;
        }
      }
    }
    if (c === '<') {
      const m = /^<((?:https?:|mailto:)[^\s<>]+)>/i.exec(s.slice(i));
      if (m) { flush(); out.push(linkNode(m[1].replace(/^mailto:/i, ''), m[1], '', true)); i += m[0].length - 1; continue; }
    }
    if (c === 'h' && /^https?:\/\//i.test(s.slice(i, i + 8)) && (i === 0 || /[\s(<[]/.test(s[i - 1]))) {
      const m = /^https?:\/\/[^\s<>]*[^\s<>.,;:!?)'"\]]/i.exec(s.slice(i));
      if (m) { flush(); out.push(linkNode(m[0], m[0], '', true)); i += m[0].length - 1; continue; }
    }
    if (c === '\n') { flush(); out.push(el('br')); continue; }
    text += c;
  }
  flush();
  return out;
}

// ------------------------------------------------------------------ blocks
const FENCE = /^ {0,3}(`{3,}|~{3,})\s*(\S*)/;
const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const HR = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^ {0,3}>\s?(.*)$/;
const BULLET = /^(\s*)([-*+•])\s+(.*)$/;
const NUMBER = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

function splitRow(line) {
  const cells = [];
  let cur = '', inCode = false;
  const s = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === '\\' && s[i + 1] === '|') { cur += '|'; i++; continue; }
    if (c === '`') inCode = !inCode;
    if (c === '|' && !inCode) { cells.push(cur.trim()); cur = ''; continue; }
    cur += c;
  }
  cells.push(cur.trim());
  return cells;
}

function isTableStart(lines, i) {
  return i + 1 < lines.length && lines[i].includes('|') && TABLE_SEP.test(lines[i + 1])
    && splitRow(lines[i + 1]).length === splitRow(lines[i]).length;
}

function table(lines, i) {
  const head = splitRow(lines[i]);
  const aligns = splitRow(lines[i + 1]).map(c => (/^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'right' : /^:-+/.test(c) ? 'left' : ''));
  const wrap = el('div', 'md-table');
  const t = el('table');
  const thead = el('thead'), tr = el('tr');
  head.forEach((c, k) => { const th = el('th'); if (aligns[k]) th.style.textAlign = aligns[k]; th.append(...inline(c)); tr.append(th); });
  thead.append(tr); t.append(thead);
  const tbody = el('tbody');
  let j = i + 2;
  for (; j < lines.length && lines[j].trim() && lines[j].includes('|'); j++) {
    const row = el('tr'), cells = splitRow(lines[j]);
    head.forEach((_, k) => { const td = el('td'); if (aligns[k]) td.style.textAlign = aligns[k]; td.append(...inline(cells[k] || '')); row.append(td); });
    tbody.append(row);
  }
  t.append(tbody); wrap.append(t);
  return [wrap, j];
}

// A list: items of the same kind at the same indentation; an item's
// continuation lines (indented past its marker) are parsed as blocks of their
// own, which is how nested lists and paragraphs inside items work.
function list(lines, i, kind) {
  const first = (kind === 'ul' ? BULLET : NUMBER).exec(lines[i]);
  const indent = first[1].length;
  const node = el(kind);
  if (kind === 'ol' && +first[2] !== 1) node.start = +first[2];
  let j = i;
  while (j < lines.length) {
    const m = (kind === 'ul' ? BULLET : NUMBER).exec(lines[j]);
    if (!m || m[1].length !== indent) break;
    const li = el('li');
    const inner = m[0].length - m[3].length;  // where the item's text starts
    let body = m[3];
    const task = /^\[([ xX])\]\s+(.*)$/.exec(body);
    if (task) {
      const box = el('input'); box.type = 'checkbox'; box.disabled = true; if (task[1] !== ' ') { box.checked = true; box.setAttribute('checked', ''); }
      li.className = 'task'; li.append(box, ' ');
      body = task[2];
    }
    const sub = [body];
    j++;
    // continuation: blank lines followed by indented text, or indented lines
    while (j < lines.length) {
      const l = lines[j];
      if (!l.trim()) {
        let k = j;
        while (k < lines.length && !lines[k].trim()) k++;
        if (k < lines.length && /^\s/.test(lines[k]) && leading(lines[k]) > indent) { sub.push(...lines.slice(j, k)); j = k; continue; }
        break;
      }
      if (leading(l) > indent && !((kind === 'ul' ? BULLET : NUMBER).test(l) && leading(l) === indent)) {
        sub.push(l.slice(Math.min(inner, leading(l)))); j++; continue;
      }
      break;
    }
    const parsed = blocks(sub);
    // the item's first paragraph sits inline in it ("tight" list)
    if (parsed.length && parsed[0].tagName === 'P') li.append(...parsed[0].childNodes, ...parsed.slice(1));
    else li.append(...parsed);
    node.append(li);
  }
  return [node, j];
}

const leading = l => /^\s*/.exec(l)[0].length;

function blocks(lines) {
  const out = [];
  let para = [];
  const flushPara = () => {
    if (!para.length) return;
    const p = el('p');
    p.append(...inline(para.join('\n').replace(/ {2,}$/gm, '').replace(/\\$/gm, '')));
    out.push(p); para = [];
  };
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) { flushPara(); i++; continue; }
    const fence = FENCE.exec(line);
    if (fence) {
      flushPara();
      const code = [];
      let j = i + 1;
      for (; j < lines.length && !(lines[j].trim().startsWith(fence[1][0].repeat(3)) && lines[j].trim().replace(/\s+$/, '').split('').every(ch => ch === fence[1][0])); j++) code.push(lines[j]);
      const pre = el('pre'), c = el('code');
      if (fence[2]) c.dataset.lang = fence[2];
      c.textContent = code.join('\n');
      pre.append(c); out.push(pre);
      i = j + 1; continue;
    }
    if (!para.length && /^ {4}|^\t/.test(line) && !BULLET.test(line) && !NUMBER.test(line)) {
      flushPara();
      const code = [];
      let j = i;
      for (; j < lines.length && (/^ {4}|^\t/.test(lines[j]) || (!lines[j].trim() && j + 1 < lines.length && /^ {4}|^\t/.test(lines[j + 1]))); j++) code.push(lines[j].replace(/^ {4}|^\t/, ''));
      const pre = el('pre'), c = el('code');
      c.textContent = code.join('\n'); pre.append(c); out.push(pre);
      i = j; continue;
    }
    const head = HEADING.exec(line);
    if (head) { flushPara(); const h = el('h' + Math.min(6, head[1].length)); h.append(...inline(head[2])); out.push(h); i++; continue; }
    if (HR.test(line)) { flushPara(); out.push(el('hr')); i++; continue; }
    if (QUOTE.test(line)) {
      flushPara();
      const q = [];
      let j = i;
      for (; j < lines.length && (QUOTE.test(lines[j]) || (lines[j].trim() && q.length && !BULLET.test(lines[j]) && !HEADING.test(lines[j]))); j++) q.push((QUOTE.exec(lines[j]) || [, lines[j]])[1]);
      const b = el('blockquote'); b.append(...blocks(q)); out.push(b);
      i = j; continue;
    }
    if (isTableStart(lines, i)) { flushPara(); const [t, j] = table(lines, i); out.push(t); i = j; continue; }
    if (BULLET.test(line) || NUMBER.test(line)) {
      // "1. " inside a paragraph is a list too (agents rarely put a blank line first)
      flushPara();
      const [node, j] = list(lines, i, BULLET.test(line) ? 'ul' : 'ol');
      out.push(node); i = j; continue;
    }
    para.push(line.replace(/^\s+/, ''));
    i++;
  }
  flushPara();
  return out;
}

export function markdown(src) {
  const root = el('div', 'md');
  const lines = String(src || '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n');
  root.append(...blocks(lines));
  return root;
}
