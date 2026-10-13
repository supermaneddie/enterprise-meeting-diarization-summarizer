import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

await build({
  entryPoints: [fileURLToPath(new URL('./markdown.js', import.meta.url))],
  outfile: fileURLToPath(new URL('../static/markdown.min.js', import.meta.url)),
  bundle: true,
  minify: true,
  format: 'iife',
  globalName: 'TranscriptionMarkdown',
  platform: 'browser',
  target: ['es2020'],
  legalComments: 'linked',
});
