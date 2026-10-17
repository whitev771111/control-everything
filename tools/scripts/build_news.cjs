// Fixed development-time entry; targets and paths are not CLI options.
'use strict';
const path = require('node:path');
const { fixedCompiler, cli } = require('./frontend_compile.cjs');
const builder = fixedCompiler('news');
if (require.main === module) cli(builder, path.resolve(__dirname, '../..'));
module.exports = builder;
