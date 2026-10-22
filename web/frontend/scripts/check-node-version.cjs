// Minimum patched versions from the 2026-10-21 Node.js security release.
const minimums = new Map([[22, [23, 4]], [24, [22, 1]], [26, [12, 1]]]);

function isSupportedNodeVersion(version) {
  if (!/^\d+\.\d+\.\d+$/.test(version)) return false;
  const [major, minor, patch] = version.split('.').map(Number);
  const minimum = minimums.get(major);
  return Boolean(minimum && (minor > minimum[0] || (minor === minimum[0] && patch >= minimum[1])));
}

if (require.main === module && !isSupportedNodeVersion(process.versions.node)) {
  console.error(`Node.js ${process.versions.node} is unsupported. Use Node.js 22.23.4+, 24.22.1+, or 26.12.1+ in those release lines (2026-10-21 security fixes).`);
  process.exitCode = 1;
}

module.exports = { isSupportedNodeVersion };
