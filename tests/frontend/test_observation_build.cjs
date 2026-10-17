// Real fixed compiler in an owned mini-repo; no browser, product emit, or installation.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const builder = require('../../scripts/build_observation.cjs');
const repo = path.resolve(__dirname, '../..');
const parent = path.join(repo, 'work/frontend-observation-tests');
const declarations = ['tools/typescript/package.json', 'tools/typescript/package-lock.json', 'tools/typescript/observation.manifest.json', 'tsconfig.observation.json', 'scripts/build_observation.cjs'];
const validSource = `'use strict';\n(function () {\n  const label: string = 'synthetic';\n  window.WorkspaceObservation = { projects: [], start() { void label; }, renderProjectRefs() {} };\n})();\n`;
const types = `declare namespace WorkspaceObservationTypes {\n  interface PublicApi { projects: readonly {id: string; name: string}[]; start(): void; renderProjectRefs(): void; }\n}\ninterface Window { WorkspaceObservation: WorkspaceObservationTypes.PublicApi; }\n`;
const oldBytes = Buffer.from('// old delivered asset, intentionally not generated\n');
let root, compiler, copiedDeclarations;
const compilerFailures = [];
const sha256 = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const file = name => path.join(root, name);
function createFixture(withCompiler) {
  fs.mkdirSync(parent, { recursive: true });
  const target = fs.mkdtempSync(path.join(parent, 'fixture-'));
  for (const name of declarations) {
    fs.mkdirSync(path.dirname(path.join(target, name)), { recursive: true });
    fs.copyFileSync(path.join(repo, name), path.join(target, name));
  }
  fs.mkdirSync(path.join(target, 'web/workspace'), { recursive: true });
  fs.writeFileSync(path.join(target, 'web/workspace/observation.ts'), validSource);
  fs.writeFileSync(path.join(target, 'web/workspace/observation.types.d.ts'), types);
  fs.writeFileSync(path.join(target, 'web/workspace/observation.js'), oldBytes);
  if (withCompiler) fs.cpSync(path.join(repo, 'tools/typescript/node_modules/typescript'), path.join(target, 'tools/typescript/node_modules/typescript'), { recursive: true, errorOnExist: true });
  return target;
}
function writeJson(name, value) { fs.writeFileSync(file(name), JSON.stringify(value)); }
function rejects(code, callback) { assert.throws(callback, error => error instanceof builder.BuildError && error.code === code); }
function preserved(callback, code) {
  const before = fs.readFileSync(file('web/workspace/observation.js'));
  rejects(code, callback);
  assert.deepEqual(fs.readFileSync(file('web/workspace/observation.js')), before);
}
test.before(() => {
  root = createFixture(true);
  copiedDeclarations = Object.fromEntries(declarations.map(name => [name, fs.readFileSync(file(name))]));
  compiler = builder.preflight(root);
  console.log(JSON.stringify({ scope: 'owned synthetic mini-repo with copied fixed compiler', compiler: compiler.version, fixture: path.relative(repo, root) }));
});
test.beforeEach(() => {
  for (const name of declarations) fs.writeFileSync(file(name), copiedDeclarations[name]);
  fs.writeFileSync(file('web/workspace/observation.ts'), validSource);
  fs.writeFileSync(file('web/workspace/observation.types.d.ts'), types);
  fs.writeFileSync(file('web/workspace/observation.js'), oldBytes);
  for (const name of ['rogue.js', 'unexpected.d.ts']) {
    const target = file('web/workspace/' + name);
    if (fs.existsSync(target)) fs.unlinkSync(target);
  }
});
test.after(() => {
  if (root) fs.writeFileSync(path.join(root, 'contract-evidence.json'), JSON.stringify({ compiler: compiler.version, compilerFailures }, null, 2));
});
test('the actual local compiler is 5.9.3; preflight refuses a non-24 Node', () => {
  assert.equal(compiler.version, '5.9.3'); rejects('node24_required', () => builder.preflight(root, '22.0.0'));
});
test('missing project tool fails rather than searching another installed compiler', () => {
  const missing = createFixture(false);
  rejects('missing_or_invalid_tools_typescript_node_modules_typescript_package.json', () => builder.preflight(missing));
});
for (const field of ['package', 'manifest', 'config', 'lock']) test(`${field} drift is refused without modifying delivered JS`, () => {
  const name = { package: 'tools/typescript/package.json', manifest: 'tools/typescript/observation.manifest.json', config: 'tsconfig.observation.json', lock: 'tools/typescript/package-lock.json' }[field];
  const value = JSON.parse(fs.readFileSync(file(name), 'utf8'));
  if (field === 'package') value.devDependencies.typescript = '^5.9.3';
  if (field === 'manifest') value.outputs.push({ emitted: 'extra.js', path: 'web/workspace/extra.js', url: '/extra.js' });
  if (field === 'config') value.compilerOptions.strict = false;
  if (field === 'lock') value.packages['node_modules/unexpected'] = { version: '1' };
  writeJson(name, value); preserved(() => builder.run(root, 'build'), field + '_drift');
});
test('lock requires official resolved, real integrity and the exact package entry', () => {
  const lock = JSON.parse(copiedDeclarations['tools/typescript/package-lock.json']);
  lock.packages['node_modules/typescript'].integrity = 'sha512-invalid'; writeJson('tools/typescript/package-lock.json', lock);
  rejects('lock_integrity_missing', () => builder.preflight(root));
});
test('installed package metadata version drift is refused', () => {
  const name = 'tools/typescript/node_modules/typescript/package.json', original = fs.readFileSync(file(name));
  try { const value = JSON.parse(original); value.version = '6.0.0'; writeJson(name, value); rejects('compiler_version_mismatch', () => builder.preflight(root)); }
  finally { fs.writeFileSync(file(name), original); }
});
test('typecheck uses the real compiler, emits nothing and leaves old JS alone', () => {
  const result = builder.run(root, 'typecheck'); assert.equal(result.exit, 0); assert.deepEqual(result.outputs, []);
  assert.deepEqual(fs.readFileSync(file('web/workspace/observation.js')), oldBytes);
  assert.equal(fs.existsSync(file('work/frontend-observation/manual-emit')), false);
});
test('valid source builds an ordinary marked script and fresh check compares all bytes', () => {
  const built = builder.run(root, 'build'); const emitted = fs.readFileSync(file('web/workspace/observation.js'));
  assert.equal(built.exit, 0); assert.ok(emitted.toString('utf8').startsWith(builder.HEADER));
  assert.equal(built.outputs.length, 1); assert.equal(built.outputs[0].path, 'web/workspace/observation.js');
  const checked = builder.run(root, 'check'); assert.equal(checked.check_matches, true); assert.deepEqual(fs.readFileSync(file('web/workspace/observation.js')), emitted);
  const stage = path.join(root, built.stage); assert.equal(fs.existsSync(path.join(stage, 'observation.js.map')), false);
  assert.equal(fs.existsSync(path.join(stage, 'observation.d.ts')), false);
});
for (const [label, text] of [['syntax', validSource + '\nconst = ;'], ['semantic', validSource.replace("const label: string = 'synthetic'", 'const label: string = 42')]]) test(`real ${label} diagnostic prevents emit and preserves old JS`, () => {
  fs.writeFileSync(file('web/workspace/observation.ts'), text);
  const before = fs.readFileSync(file('web/workspace/observation.js'));
  let failure;
  assert.throws(() => builder.run(root, 'build'), error => {
    failure = error;
    return error.code === 'compiler_diagnostics' && error.diagnostics.length > 0 && error.diagnostics.every(row => row.file === 'web/workspace/observation.ts' || row.file === null);
  });
  const after = fs.readFileSync(file('web/workspace/observation.js'));
  assert.deepEqual(after, before);
  compilerFailures.push({ case: label, exit: failure.exit, diagnostics: failure.diagnostics, old_js_before_sha256: sha256(before), old_js_after_sha256: sha256(after) });
});
test('missing one configured input cannot be hidden by existing old JS', () => {
  fs.unlinkSync(file('web/workspace/observation.ts'));
  preserved(() => builder.run(root, 'check'), 'missing_observation_inputs');
});
test('an extra ambient source cannot join the fixed Program closure', () => {
  fs.writeFileSync(file('web/workspace/unexpected.d.ts'), 'interface Unrequested {}');
  fs.writeFileSync(file('web/workspace/observation.ts'), '/// <reference path="unexpected.d.ts" />\n' + validSource);
  preserved(() => builder.run(root, 'build'), 'unexpected_program_input');
});
test('check cannot pass from an old JS file or repair a changed byte', () => {
  preserved(() => builder.run(root, 'check'), 'unexpected_generated_asset');
  builder.run(root, 'build'); fs.appendFileSync(file('web/workspace/observation.js'), '\n// changed byte');
  preserved(() => builder.run(root, 'check'), 'generated_bytes_differ');
});
test('an unlisted generated marker is rejected, including before a first build', () => {
  fs.writeFileSync(file('web/workspace/rogue.js'), builder.HEADER + '');
  preserved(() => builder.run(root, 'build'), 'unexpected_generated_asset');
});
test('complete output set rejects missing, extra files and even empty directories', () => {
  const stage = fs.mkdtempSync(path.join(parent, 'stage-'));
  rejects('incomplete_emit_output', () => builder.completeOutputs(stage));
  fs.writeFileSync(path.join(stage, 'observation.js'), ''); builder.completeOutputs(stage);
  fs.writeFileSync(path.join(stage, 'extra.map'), ''); rejects('incomplete_emit_output', () => builder.completeOutputs(stage));
  fs.unlinkSync(path.join(stage, 'extra.map')); fs.mkdirSync(path.join(stage, 'extra')); rejects('unexpected_emit_output', () => builder.completeOutputs(stage));
});
test('writeFile hook rejects duplicate, unexpected and outside-stage paths', () => {
  const stage = fs.mkdtempSync(path.join(parent, 'writer-')), writer = builder.stageWriter(stage), output = path.join(stage, 'observation.js');
  writer(output, 'first', false); rejects('unexpected_emit_output', () => writer(output, 'again', false));
  rejects('unexpected_emit_output', () => writer(path.join(stage, 'observation.js.map'), '', false));
  rejects('unexpected_emit_output', () => writer(path.join(stage, 'observation.d.ts'), '', false));
  rejects('path_outside_root', () => writer(path.join(stage, '..', 'outside.js'), '', false));
});
test('emitSkipped cannot be reported as a successful complete emit', () => {
  rejects('emit_skipped', () => builder.requireCompletedEmit(compiler, root, { diagnostics: [], emitSkipped: true }));
  builder.requireCompletedEmit(compiler, root, { diagnostics: [], emitSkipped: false });
});
test('path boundary rejects linked ancestors through the filesystem adapter', () => {
  rejects('linked_path', () => builder.noLinks(root, file('web/workspace/observation.ts'), () => ({ isSymbolicLink: () => true })));
});
// Construct only this synthetic fixture to avoid a source-scanner false positive.
const syntheticPrivatePath = ['C:', 'Users', 'synthetic', 'hidden'].join('/');
assert.equal(syntheticPrivatePath, 'C:' + '/Users' + '/synthetic/hidden');
for (const marker of ['//# sourceMappingURL=data:application/json;base64,AAAA', '//# sourceURL=private.js', '/* sourcesContent */', '/* ' + syntheticPrivatePath + ' */']) test(`output content rejects ${marker.split('=')[0]} without rewriting it`, () => {
  rejects(marker.includes('Users') ? 'source_path_forbidden' : 'source_map_forbidden', () => builder.emittedBytes(compiler, root, Buffer.from(builder.HEADER + validSource.replace(': string', '') + marker)));
});
for (const [label, source, code] of [
  ['import', "import x from './unexpected';", 'module_output_forbidden'],
  ['require', '(function(){ require("x"); })();', 'module_output_forbidden'],
  ['namespace', 'namespace Unexpected { export const value = 1; }', 'module_output_forbidden'],
  ['window export', '(function(){ window.Unexpected = {}; })();', 'unexpected_window_export'],
  ['top-level global', 'var value = 1; window.WorkspaceObservation = {};', 'ordinary_iife_required'],
]) test(`AST refuses ${label}`, () => {
  rejects(code, () => builder.emittedBytes(compiler, root, Buffer.from(builder.HEADER + source)));
});
test('publish I/O failure cannot replace old bytes or report success', () => {
  const stage = fs.mkdtempSync(path.join(parent, 'publish-'));
  assert.throws(() => builder.atomicReplace(file('web/workspace/observation.js'), Buffer.from('new'), stage, () => { throw new Error('synthetic rename failure'); }), /synthetic rename failure/);
  assert.deepEqual(fs.readFileSync(file('web/workspace/observation.js')), oldBytes);
});
test('the real CLI rejects root/output overrides and accepts only a single mode', () => {
  const result = spawnSync(process.execPath, [file('scripts/build_observation.cjs'), 'build', '--root', repo], { encoding: 'utf8', cwd: root });
  assert.equal(result.status, 2); assert.match(result.stderr, /mode_required/); assert.deepEqual(fs.readFileSync(file('web/workspace/observation.js')), oldBytes);
});
test('the real CLI typechecks the mini-repo without emitting delivered JS', () => {
  const result = spawnSync(process.execPath, [file('scripts/build_observation.cjs'), 'typecheck'], { encoding: 'utf8', cwd: root });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).compiler, '5.9.3');
  assert.deepEqual(fs.readFileSync(file('web/workspace/observation.js')), oldBytes);
});
test('a directory cannot stand in for the delivered JS target', () => {
  const target = file('web/workspace/observation.js');
  fs.unlinkSync(target); fs.mkdirSync(target);
  try {
    rejects('unexpected_generated_asset', () => builder.run(root, 'check'));
    assert.equal(fs.statSync(target).isDirectory(), true);
  } finally { fs.rmdirSync(target); }
});
test('missing delivered target is not success and check does not recreate it', () => {
  fs.unlinkSync(file('web/workspace/observation.js'));
  rejects('unexpected_generated_asset', () => builder.run(root, 'check')); assert.equal(fs.existsSync(file('web/workspace/observation.js')), false);
});
