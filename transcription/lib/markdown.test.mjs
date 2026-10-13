import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';

const formatSelector = '<select id="summary-format"><option value="markdown">Markdown</option><option value="plain">Plain text</option></select>';
const bundle = readFileSync(new URL('../static/markdown.min.js', import.meta.url), 'utf8');
function render(source) {
  const dom = new JSDOM('<div id="summary"></div>', { runScripts: 'outside-only' });
  dom.window.eval(bundle);
  const element = dom.window.document.getElementById('summary');
  dom.window.TranscriptionMarkdown.render(element, source);
  return element;
}

test('browser bundle renders headings, lists, tables and fenced code', () => {
  const element = render('# Decisions\n\n- **Ship** it\n\n| Owner | Task |\n| --- | --- |\n| Alice | Review |\n\n```js\nconst x = 1;\n```');
  assert.equal(element.querySelector('h1').textContent, 'Decisions');
  assert.equal(element.querySelector('li strong').textContent, 'Ship');
  assert.equal(element.querySelector('tbody td').textContent, 'Alice');
  assert.match(element.querySelector('pre code').textContent, /const x = 1;/);
});

test('removes executable HTML, unsafe links, and injected styles', () => {
  const element = render('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n[unsafe](javascript:alert%281%29)\n\n<svg onload="alert(1)"></svg><style>body{display:none}</style><p style="position:fixed" onclick="alert(1)">Safe text</p>');
  assert.equal(element.querySelector('script, svg, style, [onerror], [onload], [onclick], [style]'), null);
  assert.equal(element.querySelector('a').hasAttribute('href'), false);
  assert.match(element.textContent, /Safe text/);
});

test('updates replace old output and ordinary text remains readable', () => {
  const element = render('## Old analysis');
  element.ownerDocument.defaultView.TranscriptionMarkdown.render(element, 'New analysis & next steps');
  assert.equal(element.querySelector('h2'), null);
  assert.equal(element.textContent.trim(), 'New analysis & next steps');
});

test('job template renders initial and updated Markdown and copies the source', async () => {
  const template = readFileSync(new URL('../templates/job.html', import.meta.url), 'utf8');
  const scripts = [...template.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  const dom = new JSDOM(formatSelector + '<div id="summary"># Initial</div><pre id="transcript-timeline" hidden></pre><button data-copy-target="summary"></button>', { runScripts: 'outside-only' });
  const { window } = dom;
  window.eval(bundle);
  window.eval(scripts[0][1] + '\n' + scripts[1][1]);
  const summary = window.document.getElementById('summary');
  assert.equal(summary.querySelector('h1').textContent, 'Initial');
  window.renderSummary('## Updated\n\n**Decision**');
  assert.equal(summary.querySelector('h2').textContent, 'Updated');
  const format = window.document.getElementById('summary-format');
  format.value = 'plain';
  format.dispatchEvent(new window.Event('change'));
  assert.equal(summary.children.length, 0);
  assert.equal(summary.textContent, '## Updated\n\n**Decision**');
  window.renderSummary('# New result\n\n<img src=x onerror="alert(1)">');
  assert.equal(summary.children.length, 0);
  assert.match(summary.textContent, /onerror/);
  format.value = 'markdown';
  format.dispatchEvent(new window.Event('change'));
  assert.equal(summary.querySelector('h1').textContent, 'New result');
  assert.equal(summary.querySelector('[onerror]'), null);
  window.renderSummary('## Updated\n\n**Decision**');
  let copied;
  Object.defineProperty(window, 'isSecureContext', { value: true });
  Object.defineProperty(window.navigator, 'clipboard', { value: { writeText: async value => { copied = value; } } });
  await window.copyOutput(window.document.querySelector('button'));
  assert.equal(copied, '## Updated\n\n**Decision**');
  window.close();
});

test('job template falls back to plain text if the bundle is unavailable', () => {
  const template = readFileSync(new URL('../templates/job.html', import.meta.url), 'utf8');
  const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];
  const dom = new JSDOM(formatSelector + '<div id="summary"># Initial</div>', { runScripts: 'outside-only' });
  dom.window.eval(script);
  dom.window.renderSummary('<img src=x onerror="alert(1)">');
  const summary = dom.window.document.getElementById('summary');
  assert.equal(summary.children.length, 0);
  assert.match(summary.textContent, /onerror/);
  dom.window.close();
});
