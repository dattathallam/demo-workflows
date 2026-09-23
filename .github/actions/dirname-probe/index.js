// Does a shadowed env var reach a JS action's process, and is __dirname affected by it?
//
// __dirname is where the runner physically placed and executed this file. No env: key can
// change it. If the env below is shadowed but __dirname is not, then deriving the action
// cache root from __dirname is sound where deriving it from RUNNER_WORKSPACE is not.
const path = require('path');

const show = (k) => console.log(`  ${k.padEnd(22)} = ${process.env[k] ?? '<UNSET>'}`);

console.log('::group::JS action - env as received');
['RUNNER_WORKSPACE', 'GITHUB_REPOSITORY', 'GITHUB_WORKSPACE', 'GITHUB_ACTION_PATH',
 'PROBE_MARKER'].forEach(show);
console.log('::endgroup::');

console.log('::group::JS action - physical location');
console.log(`  __dirname                      = ${__dirname}`);
console.log(`  resolve(__dirname,'../../../..') = ${path.resolve(__dirname, '../../../..')}`);
console.log(`  env-derived _actions           = ${
  process.env.RUNNER_WORKSPACE
    ? path.resolve(process.env.RUNNER_WORKSPACE, '..', '_actions')
    : '<cannot derive>'}`);
console.log('::endgroup::');
