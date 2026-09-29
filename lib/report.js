const fs = require('fs');
const path = require('path');

// Prints one CMP-RESULT line for the action in actionDir and returns the VERSION it holds.
module.exports = function report(action, stage, actionDir) {
  const version = fs.readFileSync(path.join(actionDir, 'VERSION'), 'utf8').trim();
  const state = process.env.STATE_pre_version || '<none>';
  console.log(`CMP-RESULT action=${action} stage=${stage} version=${version} state_from_pre=${state} dir=${actionDir}`);
  return version;
};
