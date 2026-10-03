'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'nanodot npm package '));
const npm = process.env.npm_execpath || require.resolve('npm/bin/npm-cli.js');
const metadata = require('../package.json');

function run(command, args, cwd = temporary, env = process.env, expected = 0) {
  const result = spawnSync(command, args, { cwd, env, encoding: 'utf8', timeout: 180000 });
  assert.equal(result.status, expected, result.stdout + result.stderr);
  return result.stdout;
}

try {
  const pyproject = fs.readFileSync(path.join(root, 'pyproject.toml'), 'utf8');
  const version = fs.readFileSync(path.join(root, 'src/nanodot/__init__.py'), 'utf8');
  assert.equal(pyproject.match(/^version = "([^"]+)"/m)[1], metadata.version);
  assert.equal(version.match(/__version__ = "([^"]+)"/)[1], metadata.version);
  const packed = JSON.parse(run(process.execPath, [npm, 'pack', '--json', '--pack-destination', temporary], root))[0];
  const paths = packed.files.map(file => file.path);
  assert(paths.includes('bin/nanodot.cjs'));
  assert(paths.includes('bin/uv-manifest.json'));
  assert(paths.includes('src/nanodot/cli.py'));
  assert(paths.every(file =>
    ['package.json', 'README.md', 'LICENSE', 'bin/nanodot.cjs', 'bin/uv-manifest.json'].includes(file)
    || file.startsWith('src/nanodot/') && file.endsWith('.py')), paths);
  const archive = path.join(temporary, packed.filename);
  const prefix = path.join(temporary, 'global prefix');
  run(process.execPath, [npm, 'install', '--global', '--prefix', prefix, '--ignore-scripts',
    '--no-audit', '--no-fund', archive]);
  const env = { ...process.env, NANODOT_HOME: path.join(temporary, 'data'),
    XDG_CACHE_HOME: path.join(temporary, 'runtime-cache') };
  delete env.PYTHONPATH;
  const cli = path.join(prefix, 'bin', 'nanodot');
  fs.writeFileSync(path.join(temporary, 'nanodot.py'), 'raise RuntimeError("wrong source loaded")\n');
  assert.match(run(cli, ['--help'], temporary, env), /usage: nanodot/);
  assert.equal(run(cli, ['--version'], temporary, env).trim(), 'nanodot ' + metadata.version);
  // Once the MVP is integrated, check the daemon's own Python subprocess too.
  if (paths.includes('src/nanodot/native/runner_control.py')) {
    try {
      assert.match(run(cli, ['start'], temporary, env), /runner started/);
      assert.match(run(cli, ['status'], temporary, env), /runner is running/);
    } finally {
      run(cli, ['stop'], temporary, env);
    }
    assert.match(run(cli, ['status'], temporary, env, 1), /runner is not running/);
    console.log('PASS installed background runner inherits the packaged source outside the checkout');
  }
  const output = run(process.execPath, [npm, 'exec', '--yes', '--cache',
    path.join(temporary, 'npm-cache'), '--package', archive, '--', 'nanodot', '--version'], temporary, env);
  assert.equal(output.trim(), 'nanodot ' + metadata.version);
  console.log('PASS packed global install with --ignore-scripts, npx execution, payload and versions');
} finally {
  if (fs.existsSync(path.join(temporary, 'data', 'runner.pid'))) {
    console.error('Runner cleanup was not confirmed; preserved test data at ' + temporary);
  } else {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
