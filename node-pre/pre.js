const fs = require('fs');
const path = require('path');
const report = require('../lib/report');

const version = report('node-pre', 'pre', __dirname);
fs.appendFileSync(process.env.GITHUB_STATE, `pre_version=${version}\n`);
// A runtime artifact inside the action's own directory, like an npm install done by a pre step.
fs.writeFileSync(path.join(__dirname, '.pre-ran'), `${version}\n`);
