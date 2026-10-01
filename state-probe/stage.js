const fs = require('fs');
const path = require('path');

// Prints what this stage received, then appends `saves` to this stage's GITHUB_STATE file.
module.exports = function stage(name, saves) {
  const received = Object.keys(process.env).filter((k) => k.startsWith('STATE_')).sort();
  console.log(`STATE-PROBE stage=${name} step=${process.env.GITHUB_ACTION} instance=${process.env.PROBE_INSTANCE || '-'}`);
  console.log(`STATE-PROBE stage=${name} received ${received.length} STATE_* variable(s):`);
  for (const k of received) console.log(`STATE-PROBE stage=${name}   ${k}=${process.env[k]}`);

  const stateFile = process.env.GITHUB_STATE || '';
  console.log(`STATE-PROBE stage=${name} GITHUB_STATE=${stateFile}`);
  const dir = path.dirname(stateFile);
  if (stateFile && fs.existsSync(dir)) {
    for (const f of fs.readdirSync(dir).sort()) {
      const content = fs.readFileSync(path.join(dir, f), 'utf8');
      console.log(`STATE-PROBE stage=${name}   file ${f} (${content.length} bytes)${content ? ': ' + JSON.stringify(content.slice(0, 200)) : ''}`);
    }
  }

  for (const [k, v] of Object.entries(saves)) fs.appendFileSync(stateFile, `${k}=${v}\n`);
  console.log(`STATE-PROBE stage=${name} saved: ${Object.keys(saves).join(', ') || '(nothing)'}`);
};
