#!/usr/bin/env node
// Export a dual-theme standalone SVG from an archify-rendered diagram HTML.
//
// Usage: node scripts/export-diagram-svg.mjs <input.html> <output.svg>
//
// Replicates the in-page "Download SVG" (autoTheme) path of the archify
// template without a browser: dark variables are the default, light swaps in
// via `@media (prefers-color-scheme: light)`, and `svg[data-theme="..."]`
// still allows a forced theme downstream. The result self-themes when
// embedded in GitHub READMEs and the VitePress site.

import { readFileSync, writeFileSync } from 'node:fs';

const [input, output] = process.argv.slice(2);
if (!input || !output) {
  console.error('usage: export-diagram-svg.mjs <input.html> <output.svg>');
  process.exit(2);
}

const html = readFileSync(input, 'utf8');

// --- 1. The diagram SVG ----------------------------------------------------
const svgMatch = html.match(/<svg\b[\s\S]*?<\/svg>/);
if (!svgMatch) {
  console.error(`no <svg> block found in ${input}`);
  process.exit(1);
}
let svg = svgMatch[0];

// --- 2. Top-level CSS rules relevant inside a standalone SVG ---------------
// Same filter as the in-page exporter: semantic classes, markers, theme
// variable blocks. @media / @keyframes blocks are skipped (rule.type !== 1).
const styleBlocks = [...html.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(m => m[1]);
const css = styleBlocks.join('\n');

function topLevelRules(cssText) {
  const rules = [];
  let i = 0;
  while (i < cssText.length) {
    const open = cssText.indexOf('{', i);
    if (open === -1) break;
    const selector = cssText.slice(i, open).trim();
    // Find the matching close brace (handles nested blocks for at-rules).
    let depth = 1;
    let j = open + 1;
    while (j < cssText.length && depth > 0) {
      if (cssText[j] === '{') depth++;
      else if (cssText[j] === '}') depth--;
      j++;
    }
    const body = cssText.slice(open + 1, j - 1);
    if (!selector.startsWith('@')) {
      rules.push({ selector: selector.replace(/\/\*[\s\S]*?\*\//g, '').trim(), body });
    }
    i = j;
  }
  return rules;
}

const rules = topLevelRules(css);
const keepRe = /(^|,)\s*(svg|:root|\[data-theme|\.c-|\.t-|\.a-|\.m-)/;
const hostRules = rules.filter(r => keepRe.test(r.selector));
const hostStyle = hostRules
  .map(r => `${r.selector} { ${r.body.replace(/\s+/g, ' ').trim()} }`)
  .join('\n');

// --- 3. Resolve dark / light variable sets ---------------------------------
function varsOf(body) {
  const out = {};
  for (const m of body.matchAll(/(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g)) {
    out[m[1]] = m[2].replace(/\/\*[\s\S]*?\*\//g, '').trim();
  }
  return out;
}

const darkVars = {};
const lightOnly = {};
for (const r of hostRules) {
  const isDark = /\[data-theme="dark"\]|:root/.test(r.selector) && !/\[data-theme="light"\]/.test(r.selector);
  const isLight = /\[data-theme="light"\]/.test(r.selector) && !/\[data-theme="dark"\]/.test(r.selector);
  if (isDark) Object.assign(darkVars, varsOf(r.body));
  else if (isLight) Object.assign(lightOnly, varsOf(r.body));
}
// Light = dark defaults overridden by the light block (matches the cascade
// the in-page off-DOM probe resolves).
const lightVars = { ...darkVars, ...lightOnly };

const fmt = vars => Object.entries(vars).map(([k, v]) => `${k}: ${v};`).join(' ');
if (Object.keys(darkVars).length === 0) {
  console.error(`no theme variables found in ${input}`);
  process.exit(1);
}

// --- 4. Compose ------------------------------------------------------------
const fontFallback = [400, 500, 600, 700]
  .map(w => `@font-face { font-family: 'JetBrains Mono'; font-weight: ${w}; src: local('JetBrains Mono'), local('JetBrainsMono-Regular'); }`)
  .join('\n');

const styleContent =
  `${fontFallback}\n` +
  `svg { font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', 'Liberation Mono', 'Noto Sans Mono CJK SC', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', monospace; }\n` +
  `${hostStyle}\n` +
  `:root, svg { ${fmt(darkVars)} }\n` +
  `@media (prefers-color-scheme: light) { :root, svg { ${fmt(lightVars)} } }\n` +
  `svg[data-theme="light"] { ${fmt(lightVars)} }\n` +
  `svg[data-theme="dark"] { ${fmt(darkVars)} }\n` +
  `rect.c-bg-rect { fill: var(--bg); }\n`;

// Ensure xmlns + intrinsic size from the viewBox, then inject style + bg rect
// as the first children (the bg rect must paint under everything else).
const vbMatch = svg.match(/viewBox="([\d.\s-]+)"/);
if (!vbMatch) {
  console.error(`no viewBox on <svg> in ${input}`);
  process.exit(1);
}
const [, , vbW, vbH] = vbMatch[1].trim().split(/\s+/).map(Number);

svg = svg.replace(/<svg\b([^>]*)>/, (m, attrs) => {
  let a = attrs
    .replace(/\s*xmlns="[^"]*"/, '')
    .replace(/\s*data-theme="[^"]*"/, '')
    .replace(/\s*width="[^"]*"/, '')
    .replace(/\s*height="[^"]*"/, '');
  return `<svg xmlns="http://www.w3.org/2000/svg"${a} width="${vbW}" height="${vbH}">` +
    `\n<style>\n${styleContent}</style>\n` +
    `<rect width="100%" height="100%" class="c-bg-rect"/>`;
});

writeFileSync(output, svg + '\n');
console.log(`wrote ${output} (${vbW}x${vbH}, ${Object.keys(darkVars).length} theme vars)`);
