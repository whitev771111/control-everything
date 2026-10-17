// Two real fixed targets in an owned mini-repo; no install, browser or product emit.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const shared = require('../../scripts/frontend_compile.cjs');
const observation = require('../../scripts/build_observation.cjs');
const news = require('../../scripts/build_news.cjs');
const repo = path.resolve(__dirname, '../..');
const parent = path.join(repo, 'work/frontend-news-tests');
let root, compiler;
const names = ['tools/typescript/package.json', 'tools/typescript/package-lock.json', 'tools/typescript/observation.manifest.json', 'tools/typescript/news.manifest.json', 'tools/typescript/tsconfig.observation.json', 'tools/typescript/tsconfig.news.json', 'scripts/frontend_compile.cjs', 'scripts/build_observation.cjs', 'scripts/build_news.cjs', 'web/workspace/observation.ts', 'web/workspace/observation.types.d.ts', 'web/workspace/news.types.d.ts'];
const valid = `(function () { window.WorkspaceNews = { startWorkspace() {}, startLegacy() {}, onView() {} }; })();`;
const file = name => path.join(root, name);
const put = (name, value) => fs.writeFileSync(file(name), value);
const unchanged = callback => {
  const before = ['observation', 'news'].map(name => fs.readFileSync(file(`web/workspace/${name}.js`)));
  callback();
  for (const [i, name] of ['observation', 'news'].entries()) assert.deepEqual(fs.readFileSync(file(`web/workspace/${name}.js`)), before[i]);
};
test.before(() => {
  fs.mkdirSync(parent, { recursive: true }); root = fs.mkdtempSync(path.join(parent, 'fixture-'));
  for (const name of names) { fs.mkdirSync(path.dirname(file(name)), { recursive: true }); fs.copyFileSync(path.join(repo, name), file(name)); }
  fs.cpSync(path.join(repo, 'tools/typescript/node_modules/typescript'), file('tools/typescript/node_modules/typescript'), { recursive: true, errorOnExist: true });
  compiler = news.preflight(root); assert.equal(compiler.version, '5.9.3');
  console.log(JSON.stringify({ scope: 'real fixed dual compiler', fixture: path.relative(repo, root), compiler: compiler.version }));
});
test.beforeEach(() => {
  put('web/workspace/news.ts', valid); put('web/workspace/news.js', '// old news\n'); put('web/workspace/observation.js', fs.readFileSync(path.join(repo, 'web/workspace/observation.js')));
  for (const name of ['tools/typescript/news.manifest.json', 'tools/typescript/tsconfig.news.json']) fs.copyFileSync(path.join(repo, name), file(name));
  for (const name of ['rogue.js', 'other.d.ts']) if (fs.existsSync(file('web/workspace/' + name))) fs.unlinkSync(file('web/workspace/' + name));
});
test('fixed target enumeration and both ordinary-script outputs stay independent', () => {
  assert.throws(() => shared.fixedCompiler('../../arbitrary'), /fixed_asset_required/);
  const oldObservation = fs.readFileSync(file('web/workspace/observation.js'));
  assert.equal(observation.run(root, 'check').check_matches, true);
  assert.equal(news.run(root, 'typecheck').outputs.length, 0);
  const result = news.run(root, 'build'); assert.deepEqual(result.outputs.map(row => row.path), ['web/workspace/news.js']);
  const checked = news.run(root, 'check'); assert.equal(checked.check_matches, true);
  assert.ok(checked.hashes['tools/typescript/tsconfig.news.json']);
  assert.equal(Object.hasOwn(checked.hashes, 'tsconfig.news.json'), false);
  assert.deepEqual(fs.readFileSync(file('web/workspace/observation.js')), oldObservation);
  assert.equal(observation.run(root, 'check').check_matches, true);
});
for (const [label, source] of [['syntax', valid + '\nconst = ;'], ['semantic', valid + '\nconst invalid: string = 1;']]) test(`real news ${label} diagnostics preserve both delivered outputs`, () => {
  put('web/workspace/news.ts', source);
  unchanged(() => assert.throws(() => news.run(root, 'build'), error => error.code === 'compiler_diagnostics' && error.diagnostics.length > 0));
});
test('each target refuses the other manifest/config rather than expanding its closure', () => {
  put('tools/typescript/news.manifest.json', fs.readFileSync(file('tools/typescript/observation.manifest.json')));
  unchanged(() => assert.throws(() => news.run(root, 'build'), /manifest_drift/));
  fs.copyFileSync(path.join(repo, 'tools/typescript/news.manifest.json'), file('tools/typescript/news.manifest.json'));
  put('tools/typescript/tsconfig.news.json', fs.readFileSync(file('tools/typescript/tsconfig.observation.json')));
  unchanged(() => assert.throws(() => news.run(root, 'check'), /config_drift/));
});
test('news complete output, marker, byte check and writer reject extra or opposite assets', () => {
  const stage = fs.mkdtempSync(path.join(parent, 'stage-'));
  fs.writeFileSync(path.join(stage, 'news.js'), ''); news.completeOutputs(stage);
  fs.writeFileSync(path.join(stage, 'observation.js'), ''); assert.throws(() => news.completeOutputs(stage), /incomplete_emit_output/);
  assert.throws(() => news.stageWriter(stage)(path.join(stage, 'observation.js'), '', false), /unexpected_emit_output/);
  put('web/workspace/rogue.js', news.HEADER);
  unchanged(() => assert.throws(() => news.run(root, 'build'), /unexpected_generated_asset/));
  fs.unlinkSync(file('web/workspace/rogue.js')); news.run(root, 'build'); fs.appendFileSync(file('web/workspace/news.js'), '\n// stale');
  unchanged(() => assert.throws(() => news.run(root, 'check'), /generated_bytes_differ/));
});
test('news rejects maps and wrong Window exports; the shared helper adds no runtime global', () => {
  for (const suffix of ['//# sourceMappingURL=hidden.map', '/* sourcesContent */']) assert.throws(() => news.emittedBytes(compiler, root, Buffer.from(news.HEADER + valid + suffix)), /source_map_forbidden/);
  assert.throws(() => news.emittedBytes(compiler, root, Buffer.from(news.HEADER + valid.replace('WorkspaceNews', 'WorkspaceObservation'))), /unexpected_window_export/);
  assert.equal(fs.readFileSync(path.join(repo, 'web/workspace/observation.js')).includes('frontend_compile'), false);
});
test('real news CLI refuses dynamic flags without touching either output', () => {
  unchanged(() => { const result = spawnSync(process.execPath, [file('scripts/build_news.cjs'), 'check', '--manifest', 'other'], { cwd: root, encoding: 'utf8' }); assert.equal(result.status, 2); assert.match(result.stderr, /mode_required/); });
});

test('real news CLI resolves relocated config from another cwd without touching either output', () => {
  unchanged(() => {
    const result = spawnSync(process.execPath, [file('scripts/build_news.cjs'), 'typecheck'], { cwd: path.join(root, 'web/workspace'), encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    const receipt = JSON.parse(result.stdout);
    assert.ok(receipt.hashes['tools/typescript/tsconfig.news.json']);
    assert.deepEqual(receipt.outputs, []);
  });
});
test('real ambient consumers accept typed injection and reject unknown, nullable and DOM misuse', () => {
  const valid = `const deps: WorkspaceNewsTypes.Dependencies = { fetchJson: async () => ({}), postJson: async () => ({}) }; window.WorkspaceNews?.startWorkspace(deps);`;
  const cases = [['valid', valid], ['unknown', valid + ' const raw: unknown = {}; raw.items;'], ['nullable', 'const item = {} as WorkspaceNewsTypes.Item; const date: string = item.published_at;'], ['dom', 'document.createElement("p").value = "bad";'], ['write', 'const deps: WorkspaceNewsTypes.Dependencies = { fetchJson: async () => ({}) };']];
  const evidence = [];
  for (const [label, source] of cases) {
    const consumer = file('web/workspace/consumer.ts'); fs.writeFileSync(consumer, source);
    const program = compiler.createProgram([consumer, file('web/workspace/news.types.d.ts')], { strict: true, noEmit: true, types: [], target: compiler.ScriptTarget.ES2022 });
    const diagnostics = compiler.getPreEmitDiagnostics(program).map(row => row.code); evidence.push({ label, diagnostics });
    assert.equal(diagnostics.length === 0, label === 'valid', label);
  }
  console.log(JSON.stringify({ scope: 'compile-only consumers', evidence }));
});
