// Fixed development-time compiler for the observation asset. Never installs tools.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { isDeepStrictEqual } = require('node:util');
const PACKAGE = { name: 'mianban-typescript-tools', private: true, version: '1.0.0', engines: { node: '24.x' }, devDependencies: { typescript: '5.9.3' } };
const INPUTS = ['web/workspace/observation.types.d.ts', 'web/workspace/observation.ts'];
const OUTPUT = 'web/workspace/observation.js';
const MANIFEST = { schema: 1, compiler: { name: 'typescript', version: '5.9.3' }, inputs: INPUTS, outputs: [{ emitted: 'observation.js', path: OUTPUT, url: '/observation.js' }] };
const CONFIG = { compilerOptions: { target: 'ES2022', module: 'none', moduleDetection: 'legacy', lib: ['ES2022', 'DOM', 'DOM.Iterable'], types: [], strict: true,
  noUncheckedIndexedAccess: true, exactOptionalPropertyTypes: true, skipLibCheck: false, allowJs: false, checkJs: false, noEmitOnError: true,
  declaration: false, declarationMap: false, sourceMap: false, inlineSourceMap: false, inlineSources: false, importHelpers: false, removeComments: false,
  emitBOM: false, newLine: 'lf', forceConsistentCasingInFileNames: true, rootDir: 'web/workspace', outDir: 'work/frontend-observation/manual-emit' }, files: INPUTS };
const MARKER = '// Generated from web/workspace/observation.ts by scripts/build_observation.cjs.';
const HEADER = MARKER + ' Do not edit.\n';
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
class BuildError extends Error {
  constructor(code, exit = 1, diagnostics = []) { super(code); this.code = code; this.exit = exit; this.diagnostics = diagnostics; }
}
const fail = (code, exit = 1) => { throw new BuildError(code, exit); };
function inside(root, target) {
  const relative = path.relative(root, path.resolve(target));
  if (!relative || relative.startsWith('..' + path.sep) || relative === '..' || path.isAbsolute(relative)) fail('path_outside_root');
  return relative.split(path.sep).join('/');
}
function noLinks(root, target, stat = fs.lstatSync) {
  const relative = inside(root, target);
  let current = root;
  for (const part of relative.split('/')) {
    current = path.join(current, part);
    try { if (stat(current).isSymbolicLink()) fail('linked_path'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}
function json(root, name) {
  const target = path.join(root, name); noLinks(root, target);
  try { return JSON.parse(fs.readFileSync(target, 'utf8')); }
  catch (error) { if (error instanceof BuildError) throw error; fail('missing_or_invalid_' + name.replaceAll('/', '_'), 2); }
}
function preflight(root, nodeVersion = process.versions.node) {
  if (nodeVersion.split('.')[0] !== '24') fail('node24_required', 2);
  if (!isDeepStrictEqual(json(root, 'tools/typescript/package.json'), PACKAGE)) fail('package_drift', 2);
  if (!isDeepStrictEqual(json(root, 'tools/typescript/observation.manifest.json'), MANIFEST)) fail('manifest_drift', 2);
  if (!isDeepStrictEqual(json(root, 'tsconfig.observation.json'), CONFIG)) fail('config_drift', 2);
  const lock = json(root, 'tools/typescript/package-lock.json'), packages = lock?.packages;
  if (lock?.lockfileVersion !== 3 || lock.name !== PACKAGE.name || lock.version !== PACKAGE.version || !packages ||
      !isDeepStrictEqual(Object.keys(packages).sort(), ['', 'node_modules/typescript']) ||
      packages['']?.name !== PACKAGE.name || packages['']?.version !== PACKAGE.version || packages[''].dependencies !== undefined ||
      !isDeepStrictEqual(packages[''].devDependencies, PACKAGE.devDependencies) || !isDeepStrictEqual(packages[''].engines, PACKAGE.engines)) fail('lock_drift', 2);
  const tool = packages['node_modules/typescript'];
  if (tool?.version !== '5.9.3' || tool.resolved !== 'https://registry.npmjs.org/typescript/-/typescript-5.9.3.tgz' ||
      typeof tool.integrity !== 'string' || !/^sha512-[A-Za-z0-9+/]+={0,2}$/.test(tool.integrity) || Buffer.from(tool.integrity.slice(7), 'base64').length !== 64 || tool.link === true) fail('lock_integrity_missing', 2);
  const installed = json(root, 'tools/typescript/node_modules/typescript/package.json');
  if (installed.name !== 'typescript' || installed.version !== '5.9.3') fail('compiler_version_mismatch', 2);
  const compilerPath = path.join(root, 'tools/typescript/node_modules/typescript/lib/typescript.js');
  noLinks(root, compilerPath);
  let ts;
  try { ts = require(compilerPath); } catch { fail('compiler_missing', 2); }
  if (ts.version !== '5.9.3') fail('compiler_version_mismatch', 2);
  return ts;
}
function diagnostics(ts, root, rows) {
  return rows.map(row => {
    const position = row.file && row.start !== undefined ? row.file.getLineAndCharacterOfPosition(row.start) : null;
    return { code: row.code, file: row.file ? inside(root, row.file.fileName) : null,
      line: position ? position.line + 1 : null, column: position ? position.character + 1 : null };
  });
}
function requireNoDiagnostics(ts, root, rows) {
  if (rows.length) throw new BuildError('compiler_diagnostics', 1, diagnostics(ts, root, rows));
}
function requireCompletedEmit(ts, root, emitted) {
  requireNoDiagnostics(ts, root, emitted.diagnostics);
  if (emitted.emitSkipped) fail('emit_skipped');
}
function assertScript(ts, source, requireIife = false) {
  if (ts.isExternalModule(source)) fail('module_output_forbidden');
  let exports = 0;
  function visit(node) {
    if (ts.isIdentifier(node) && ['require', 'module', 'exports', 'define'].includes(node.text)) fail('module_output_forbidden');
    if (ts.isImportDeclaration(node) || ts.isImportEqualsDeclaration(node) || ts.isExportDeclaration(node) || ts.isExportAssignment(node) ||
        node.modifiers?.some(item => item.kind === ts.SyntaxKind.ExportKeyword || item.kind === ts.SyntaxKind.DefaultKeyword) ||
        ts.isModuleDeclaration(node) && !source.isDeclarationFile) fail('module_output_forbidden');
    if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword ||
        ts.isIdentifier(node.expression) && ['require', 'define'].includes(node.expression.text))) fail('module_output_forbidden');
    if (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression) && ['module', 'exports'].includes(node.expression.text)) fail('module_output_forbidden');
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      const left = node.left;
      if ((ts.isPropertyAccessExpression(left) || ts.isElementAccessExpression(left)) && ts.isIdentifier(left.expression) && ['window', 'globalThis'].includes(left.expression.text)) {
        if (!ts.isPropertyAccessExpression(left) || left.expression.text !== 'window' || left.name.text !== 'WorkspaceObservation') fail('unexpected_window_export');
        exports++;
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  if (requireIife) {
    if (exports !== 1) fail('unexpected_window_export');
    const statements = source.statements.filter(node => !(ts.isExpressionStatement(node) && ts.isStringLiteral(node.expression) && node.expression.text === 'use strict'));
    if (statements.length !== 1 || !ts.isExpressionStatement(statements[0]) || !ts.isCallExpression(statements[0].expression)) fail('ordinary_iife_required');
    let expression = statements[0].expression.expression;
    while (ts.isParenthesizedExpression(expression)) expression = expression.expression;
    if (!ts.isFunctionExpression(expression) && !ts.isArrowFunction(expression)) fail('ordinary_iife_required');
  }
}
function inputClosure(ts, root, program) {
  const lib = path.join(root, 'tools/typescript/node_modules/typescript/lib');
  const seen = new Set();
  for (const source of program.getSourceFiles()) {
    noLinks(root, source.fileName);
    const name = inside(root, source.fileName);
    if (INPUTS.includes(name)) { seen.add(name); assertScript(ts, source); }
    else if (path.resolve(path.dirname(source.fileName)) !== path.resolve(lib) || !/^lib(?:\.[a-z0-9]+)*\.d\.ts$/.test(path.basename(source.fileName)) || !source.isDeclarationFile) fail('unexpected_program_input');
  }
  if (seen.size !== INPUTS.length) fail('missing_observation_inputs', 2);
}
function stageWriter(stage) {
  const written = new Set();
  return (file, data, bom, onError) => {
    const relative = inside(stage, file); noLinks(stage, file);
    if (relative !== 'observation.js' || written.has(relative) || bom) fail('unexpected_emit_output');
    written.add(relative);
    fs.writeFileSync(file, data, { encoding: 'utf8', flag: 'wx' });
  };
}
function completeOutputs(stage) {
  const actual = [];
  for (const entry of fs.readdirSync(stage, { withFileTypes: true })) {
    const file = path.join(stage, entry.name); noLinks(stage, file);
    if (!entry.isFile()) fail('unexpected_emit_output');
    actual.push(entry.name);
  }
  if (!isDeepStrictEqual(actual.sort(), ['observation.js'])) fail('incomplete_emit_output');
}
function emittedBytes(ts, root, bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) fail('invalid_output_encoding');
  if (/sourceMappingURL|sourceURL|sourcesContent/.test(text)) fail('source_map_forbidden');
  // Match the repository's path classes; full candidate/secret scanning remains separate.
  if (/(?<![\w])[a-z]:[\\/]+(?!temporary(?:[\\/]|$)|temp(?:[\\/]|$)|example(?:[\\/]|$))[^\s'"<>]+/i.test(text) ||
      /\/(?:home|Users)\/(?!example(?:\/|$)|synthetic(?:\/|$))[^/\s'"<>]+(?:\/|$)/.test(text) ||
      text.includes(root) || text.includes(root.replaceAll('\\', '\\\\'))) fail('source_path_forbidden');
  const source = ts.createSourceFile(path.join(root, OUTPUT), text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS);
  requireNoDiagnostics(ts, root, source.parseDiagnostics);
  assertScript(ts, source, true);
}
function markedOutputs(root, initialBuild) {
  const actual = [];
  function visit(directory) {
    noLinks(root, directory);
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name); noLinks(root, file);
      if (entry.isDirectory()) visit(file);
      else if (entry.isFile() && entry.name.endsWith('.js') && fs.readFileSync(file, 'utf8').startsWith(MARKER)) actual.push(inside(root, file));
    }
  }
  visit(path.join(root, 'web/workspace'));
  if (initialBuild && !actual.includes(OUTPUT)) actual.push(OUTPUT);
  if (!isDeepStrictEqual(actual.sort(), [OUTPUT])) fail('unexpected_generated_asset');
}
function atomicReplace(target, bytes, stage, rename = fs.renameSync) {
  const temporary = path.join(stage, 'publish.js');
  fs.writeFileSync(temporary, bytes, { flag: 'wx' });
  rename(temporary, target);
}
function run(root, mode) {
  root = path.resolve(root);
  if (!['typecheck', 'build', 'check'].includes(mode)) fail('mode_required', 2);
  const receipt = { mode, exit: 1, compiler: null, hashes: {}, outputs: [], diagnostics: [] };
  let failure;
  try {
    const ts = preflight(root); receipt.compiler = ts.version;
    for (const name of [...INPUTS, 'tsconfig.observation.json', 'tools/typescript/package.json', 'tools/typescript/package-lock.json', 'tools/typescript/observation.manifest.json', 'scripts/build_observation.cjs']) {
      const file = path.join(root, name); noLinks(root, file);
      if (!fs.existsSync(file)) fail('missing_observation_inputs', 2);
      receipt.hashes[name] = hash(fs.readFileSync(file));
    }
    // The Compiler API adds defaults to the raw object. Keep the fixed contract immutable.
    const parsed = ts.parseJsonConfigFileContent(json(root, 'tsconfig.observation.json'), ts.sys, root);
    requireNoDiagnostics(ts, root, parsed.errors);
    let stage;
    const options = { ...parsed.options, noEmit: mode === 'typecheck' };
    if (mode !== 'typecheck') {
      const parent = path.join(root, 'work/frontend-observation'); noLinks(root, parent);
      fs.mkdirSync(parent, { recursive: true }); stage = fs.mkdtempSync(path.join(parent, 'emit-')); options.outDir = stage;
    }
    const program = ts.createProgram(parsed.fileNames, options);
    inputClosure(ts, root, program);
    requireNoDiagnostics(ts, root, ts.getPreEmitDiagnostics(program));
    if (mode !== 'typecheck') {
      const emitted = program.emit(undefined, stageWriter(stage));
      requireCompletedEmit(ts, root, emitted);
      completeOutputs(stage);
      const bytes = Buffer.concat([Buffer.from(HEADER, 'utf8'), fs.readFileSync(path.join(stage, 'observation.js'))]);
      emittedBytes(ts, root, bytes); markedOutputs(root, mode === 'build');
      const target = path.join(root, OUTPUT); noLinks(root, target);
      if (mode === 'check') {
        if (!fs.existsSync(target) || !fs.readFileSync(target).equals(bytes)) fail('generated_bytes_differ');
        receipt.check_matches = true;
      } else atomicReplace(target, bytes, stage);
      receipt.outputs = [{ path: OUTPUT, sha256: hash(bytes) }]; receipt.stage = inside(root, stage);
    }
    receipt.exit = 0;
  } catch (error) {
    failure = error instanceof BuildError ? error : new BuildError('build_io_failed');
    receipt.exit = failure.exit; receipt.reason = failure.code; receipt.diagnostics = failure.diagnostics;
  }
  try {
    const directory = path.join(root, 'work/frontend-observation/receipts'); noLinks(root, directory);
    fs.mkdirSync(directory, { recursive: true });
    receipt.receipt = inside(root, path.join(directory, `${mode}-${crypto.randomUUID()}.json`));
    fs.writeFileSync(path.join(root, receipt.receipt), JSON.stringify(receipt, null, 2), { flag: 'wx' });
  } catch { throw new BuildError('receipt_write_failed'); }
  if (failure) { failure.receipt = receipt; throw failure; }
  return receipt;
}
if (require.main === module) {
  try {
    if (process.argv.length !== 3) fail('mode_required', 2);
    console.log(JSON.stringify(run(path.resolve(__dirname, '..'), process.argv[2])));
  } catch (error) {
    console.error(JSON.stringify(error instanceof BuildError ? { reason: error.code, exit: error.exit, diagnostics: error.diagnostics, receipt: error.receipt?.receipt,
      ...(error.exit === 2 ? { preparation: 'Use the documented explicit project tool preparation; never a global compiler.' } : {}) } : { reason: 'build_io_failed', exit: 1 }));
    process.exitCode = error instanceof BuildError ? error.exit : 1;
  }
}
module.exports = { run, preflight, inside, noLinks, stageWriter, completeOutputs, requireCompletedEmit, emittedBytes, markedOutputs, atomicReplace, BuildError, HEADER };
