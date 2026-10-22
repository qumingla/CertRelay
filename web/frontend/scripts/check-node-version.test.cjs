const assert = require('node:assert/strict');
const test = require('node:test');
const { isSupportedNodeVersion } = require('./check-node-version.cjs');

test('rejects vulnerable and unsupported Node.js versions', () => {
  for (const version of ['20.20.0', '22.23.3', '22.22.9', '24.8.0', '24.22.0', '26.12.0', '25.9.0', '27.0.0', '24.22.1-rc.1', 'bad']) {
    assert.equal(isSupportedNodeVersion(version), false, version);
  }
});

test('accepts patched releases within supported lines', () => {
  for (const version of ['22.23.4', '22.23.5', '22.24.0', '24.22.1', '24.23.0', '26.12.1', '26.13.0']) {
    assert.equal(isSupportedNodeVersion(version), true, version);
  }
});
