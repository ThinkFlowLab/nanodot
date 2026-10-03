#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const UV_VERSION = '0.12.21';
const INSTALLER_URL = `https://astral.sh/uv/${UV_VERSION}/install.sh`;
const SOURCE = path.resolve(__dirname, '..', 'src');
const PYTHON_PROBE = 'import sys\nif sys.version_info < (3, 11): raise SystemExit(1)\nprint(sys.executable)';

function probePython(command) {
  const result = spawnSync(command, ['-I', '-c', PYTHON_PROBE], {
    encoding: 'utf8', timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'],
  });
  return result.status === 0 ? result.stdout.trim() : null;
}

function run(command, args, env = process.env, check = true) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { env, stdio: ['inherit', check ? 2 : 'inherit', 'inherit'] });
    const interrupt = () => child.kill('SIGINT');
    const terminate = () => child.kill('SIGTERM');
    process.on('SIGINT', interrupt);
    process.on('SIGTERM', terminate);
    const cleanup = () => {
      process.removeListener('SIGINT', interrupt);
      process.removeListener('SIGTERM', terminate);
    };
    child.on('error', error => { cleanup(); reject(error); });
    child.on('close', (code, signal) => {
      cleanup();
      const status = code ?? 128 + os.constants.signals[signal];
      if (check && status !== 0) {
        const error = new Error(`${path.basename(command)} exited with status ${status}`);
        error.exitCode = status;
        reject(error);
      } else {
        resolve(status);
      }
    });
  });
}

function managedPython(uv, env) {
  const result = spawnSync(uv, ['python', 'find', '--managed-python', '--no-python-downloads', '3.12'], {
    env, encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'ignore'],
  });
  return result.status === 0 ? probePython(result.stdout.trim()) : null;
}

async function python() {
  for (const candidate of ['python3', 'python3.12', 'python']) {
    const found = probePython(candidate);
    if (found) return found;
  }

  const base = process.env.XDG_CACHE_HOME || (process.platform === 'darwin'
    ? path.join(os.homedir(), 'Library', 'Caches') : path.join(os.homedir(), '.cache'));
  const cache = path.join(base, 'nanodot', 'npm');
  const uvDirectory = path.join(cache, `uv-${UV_VERSION}`);
  const uv = path.join(uvDirectory, 'uv');
  const env = {
    ...process.env,
    UV_PYTHON_INSTALL_DIR: path.join(cache, 'python'),
    UV_CACHE_DIR: path.join(cache, 'downloads'),
  };
  if (fs.existsSync(uv)) {
    const found = managedPython(uv, env);
    if (found) return found;
  }

  console.error('nanodot: Setting up Python for the first run. This requires internet access.');
  fs.mkdirSync(cache, { recursive: true, mode: 0o700 });
  if (!fs.existsSync(uv)) {
    const temporary = fs.mkdtempSync(path.join(cache, 'setup-'));
    const installer = path.join(temporary, 'install.sh');
    const installDirectory = path.join(temporary, 'uv');
    try {
      await run('curl', ['-q', '--fail', '--location', '--silent', '--show-error',
        '--connect-timeout', '15', '--max-time', '120', '--output', installer, INSTALLER_URL]);
      await run('/bin/sh', [installer], {
        ...process.env, UV_UNMANAGED_INSTALL: installDirectory, UV_NO_MODIFY_PATH: '1',
      });
      // Publish a complete installation; simultaneous first runs can use the winner.
      try {
        fs.renameSync(installDirectory, uvDirectory);
      } catch (error) {
        if (!['EEXIST', 'ENOTEMPTY'].includes(error.code)) throw error;
      }
    } finally {
      fs.rmSync(temporary, { recursive: true, force: true });
    }
  }
  await run(uv, ['python', 'install', '--no-bin', '3.12'], env);
  const found = managedPython(uv, env);
  if (!found) throw new Error('Python setup did not produce a usable interpreter');
  return found;
}

async function main() {
  const executable = await python();
  const env = {
    ...process.env,
    PYTHONPATH: SOURCE + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : ''),
    PYTHONSAFEPATH: '1',
  };
  // Insert the bundled source before the caller's working directory as well.
  const entry = 'import sys; sys.path.insert(0, sys.argv.pop(1)); from nanodot.cli import main; raise SystemExit(main())';
  process.exitCode = await run(executable, ['-c', entry, SOURCE, ...process.argv.slice(2)], env, false);
}

main().catch(error => {
  console.error(`nanodot: ${error.message}. Install Python 3.11+ or retry setup with internet access.`);
  process.exitCode = error.exitCode || 1;
});
