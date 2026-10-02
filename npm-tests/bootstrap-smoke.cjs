'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'nanodot npm bootstrap '));
const launcher = path.resolve(__dirname, '..', 'bin', 'nanodot.cjs');
const version = require('../package.json').version;
const preload = path.join(temporary, 'force-bootstrap.cjs');
// Hide PATH Python probes only; all downloads and the managed interpreter are real.
fs.writeFileSync(preload, [
  "const cp = require('node:child_process');",
  'const original = cp.spawnSync;',
  'cp.spawnSync = function(command, args, options) {',
  "  if (['python3', 'python3.12', 'python'].includes(command) && args[0] === '-I')",
  "    return { status: 1, stdout: '' };",
  '  return original.call(this, command, args, options);',
  '};',
].join('\n'));
const env = {
  ...process.env, XDG_CACHE_HOME: path.join(temporary, 'cache'),
  NANODOT_HOME: path.join(temporary, 'data'),
};

function invoke(command, args, environment = env) {
  const result = spawnSync(command, args, {
    cwd: temporary, env: environment, encoding: 'utf8', timeout: 180000,
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  return result;
}

try {
  const first = invoke(process.execPath, ['--require', preload, launcher, '--version']);
  assert.equal(first.stdout.trim(), 'nanodot ' + version);
  assert.match(first.stderr, /Setting up Python/);
  const second = invoke(process.execPath, ['--require', preload, launcher, '--version']);
  assert.equal(second.stdout.trim(), 'nanodot ' + version);
  assert.equal(second.stderr, '');
  const cache = path.join(env.XDG_CACHE_HOME, 'nanodot', 'npm');
  const uvDirectory = fs.readdirSync(cache).find(name => name.startsWith('uv-'));
  const found = invoke(path.join(cache, uvDirectory, 'uv'),
    ['python', 'find', '--managed-python', '--no-python-downloads', '3.12'],
    { ...env, UV_PYTHON_INSTALL_DIR: path.join(cache, 'python') });
  const python = found.stdout.trim();
  // Verify TLS trust in the downloaded runtime, needed for the public PR watcher.
  invoke(python, ['-c', [
    'from urllib.error import HTTPError',
    'from urllib.request import Request, urlopen',
    'request = Request("https://api.github.com/repos/ThinkFlowLab/nanodot",',
    '                  headers={"User-Agent": "nanodot-npm-smoke", "Accept": "application/vnd.github+json"})',
    'try:',
    '    with urlopen(request, timeout=30) as response: assert response.status == 200',
    'except HTTPError:',
    '    # An HTTP response verifies TLS even when anonymous API quota is exhausted.',
    '    # Certificate and connection failures are URLError, and still fail this check.',
    '    pass',
  ].join('\n')]);
  console.log('PASS real runtime download, cached restart, clean stdout and public GitHub TLS');
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
}
