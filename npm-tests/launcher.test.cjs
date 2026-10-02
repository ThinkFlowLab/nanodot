'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');
const test = require('node:test');

const LAUNCHER = path.resolve(__dirname, '..', 'bin', 'nanodot.cjs');
const SOURCE = path.resolve(__dirname, '..', 'src');

function fakePython() {
  const fs = require('node:fs');
  const path = require('node:path');
  const config = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json')));
  const args = process.argv.slice(2);
  if (args[0] === '-I') {
    if (config.oldPython && path.basename(__filename) === 'python3') process.exit(1);
    console.log(__filename);
    process.exit(0);
  }
  fs.appendFileSync(path.join(ROOT, 'app.jsonl'), JSON.stringify({
    args, cwd: process.cwd(), pythonpath: process.env.PYTHONPATH,
    dataHome: process.env.NANODOT_HOME,
    input: config.readInput ? fs.readFileSync(0, 'utf8') : null,
  }) + '\n');
  if (config.waitForSignal) {
    process.on('SIGTERM', () => {
      fs.writeFileSync(path.join(ROOT, 'terminated'), 'yes');
      process.exit(0);
    });
    console.log('READY');
    setTimeout(() => process.exit(99), 10000);
  } else {
    console.log('fake nanodot');
    process.exit(config.exitCode || 0);
  }
}

function fakeUv() {
  const fs = require('node:fs');
  const path = require('node:path');
  const args = process.argv.slice(2);
  const python = path.join(process.env.UV_PYTHON_INSTALL_DIR, 'python');
  if (args[1] === 'find') {
    if (!fs.existsSync(python)) process.exit(1);
    console.log(python);
  } else if (args[1] === 'install') {
    if (!args.includes('--no-bin')) process.exit(2);
    fs.appendFileSync(path.join(ROOT, 'python-installs'), 'install\n');
    fs.mkdirSync(path.dirname(python), { recursive: true });
    const temporary = python + '.' + process.pid;
    fs.writeFileSync(temporary, fs.readFileSync(path.join(ROOT, 'python-stub')), { mode: 0o755 });
    fs.renameSync(temporary, python);
    console.log('runtime setup output');
  } else {
    process.exit(2);
  }
}

function fakeBootstrap() {
  const fs = require('node:fs');
  const path = require('node:path');
  const config = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json')));
  if (process.env.UV_NO_MODIFY_PATH !== '1') process.exit(3);
  if (config.failSetup) process.exit(9);
  fs.mkdirSync(process.env.UV_UNMANAGED_INSTALL, { recursive: true });
  fs.copyFileSync(path.join(ROOT, 'uv-stub'), path.join(process.env.UV_UNMANAGED_INSTALL, 'uv'));
  console.log('installer output');
}

function fakeCurl() {
  const fs = require('node:fs');
  const path = require('node:path');
  const config = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json')));
  const args = process.argv.slice(2);
  if (args[0] !== '-q' || args.at(-1) !== 'https://astral.sh/uv/0.12.21/install.sh') process.exit(3);
  fs.appendFileSync(path.join(ROOT, 'downloads'), 'download\n');
  if (config.failDownload) process.exit(22);
  fs.copyFileSync(path.join(ROOT, 'installer.sh'), args[args.indexOf('--output') + 1]);
}

function fixture(t, options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nanodot npm test '));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const bin = path.join(root, 'bin');
  const caller = path.join(root, 'caller');
  fs.mkdirSync(bin);
  fs.mkdirSync(caller);
  const script = fn => '#!' + process.execPath + '\nconst ROOT = ' + JSON.stringify(root)
    + ';\n(' + fn.toString() + ')();\n';
  const write = (file, text) => fs.writeFileSync(file, text, { mode: 0o755 });
  write(path.join(root, 'python-stub'), script(fakePython));
  write(path.join(root, 'uv-stub'), script(fakeUv));
  write(path.join(root, 'bootstrap.cjs'), script(fakeBootstrap));
  write(path.join(bin, 'curl'), script(fakeCurl));
  const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
  write(path.join(root, 'installer.sh'), 'exec ' + quote(process.execPath) + ' '
    + quote(path.join(root, 'bootstrap.cjs')) + '\n');
  fs.writeFileSync(path.join(root, 'config.json'), JSON.stringify(options));
  const env = {
    ...process.env, PATH: bin, XDG_CACHE_HOME: path.join(root, 'cache'),
    NANODOT_HOME: path.join(root, 'data'), PYTHONPATH: 'original-pythonpath',
  };
  const invoke = (...args) => spawnSync(process.execPath, [LAUNCHER, ...args], {
    cwd: caller, env, encoding: 'utf8', timeout: 15000,
  });
  const warm = name => write(path.join(bin, name || 'python3'), script(fakePython));
  const config = value => fs.writeFileSync(path.join(root, 'config.json'), JSON.stringify(value));
  const count = name => fs.existsSync(path.join(root, name))
    ? fs.readFileSync(path.join(root, name), 'utf8').trim().split('\n').length : 0;
  return { root, caller, env, invoke, warm, config, count };
}

test('existing Python preserves literal arguments, data home, cwd and CLI exit status', t => {
  const f = fixture(t, { exitCode: 23 });
  f.warm();
  const args = ['watch', 'add', 'owner/repo#1; echo unsafe', '--purpose', 'a "quoted" purpose'];
  const result = f.invoke(...args);
  assert.equal(result.status, 23, result.stderr);
  assert.equal(result.stderr, '');
  const call = JSON.parse(fs.readFileSync(path.join(f.root, 'app.jsonl')));
  assert.deepEqual(call.args.slice(3), args);
  assert.equal(call.args[2], SOURCE);
  assert.equal(call.cwd, f.caller);
  assert.equal(call.dataHome, f.env.NANODOT_HOME);
  assert.equal(call.pythonpath, SOURCE + path.delimiter + 'original-pythonpath');
  assert.equal(f.count('downloads'), 0);
});

test('an unsuitable Python does not hide an available supported interpreter', t => {
  const f = fixture(t, { oldPython: true });
  f.warm();
  f.warm('python3.12');
  const result = f.invoke('--help');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(f.count('downloads'), 0);
});

test('stdin reaches the CLI for noninteractive secret entry', t => {
  const f = fixture(t, { readInput: true });
  f.warm();
  const result = spawnSync(process.execPath, [LAUNCHER, 'config', 'set', 'github-token', '-'], {
    cwd: f.caller, env: f.env, input: 'example-test-token\n', encoding: 'utf8', timeout: 15000,
  });
  assert.equal(result.status, 0, result.stderr);
  const call = JSON.parse(fs.readFileSync(path.join(f.root, 'app.jsonl')));
  assert.equal(call.input, 'example-test-token\n');
});

test('first run prepares a private runtime and later runs reuse it quietly', t => {
  const f = fixture(t);
  const first = f.invoke('--version');
  assert.equal(first.status, 0, first.stderr);
  assert.equal(first.stdout, 'fake nanodot\n');
  assert.match(first.stderr, /Setting up Python/);
  assert.match(first.stderr, /runtime setup output/);
  const second = f.invoke('--version');
  assert.equal(second.status, 0, second.stderr);
  assert.equal(second.stderr, '');
  assert.equal(f.count('downloads'), 1);
  assert.equal(f.count('python-installs'), 1);
  const cache = path.join(f.env.XDG_CACHE_HOME, 'nanodot', 'npm');
  assert.deepEqual(fs.readdirSync(cache).sort(), ['python', 'uv-0.12.21']);
});

test('failed download reports failure and a subsequent command can retry', t => {
  const f = fixture(t, { failDownload: true });
  const first = f.invoke('--help');
  assert.equal(first.status, 22);
  assert.match(first.stderr, /Install Python 3.11\+ or retry/);
  assert.equal(f.count('python-installs'), 0);
  f.config({});
  assert.equal(f.invoke('--help').status, 0);
  assert.equal(f.count('downloads'), 2);
});

test('a failed installer does not publish an incomplete cached executable', t => {
  const f = fixture(t, { failSetup: true });
  assert.equal(f.invoke('--help').status, 9);
  const cache = path.join(f.env.XDG_CACHE_HOME, 'nanodot', 'npm');
  assert.deepEqual(fs.readdirSync(cache), []);
  f.config({});
  const result = f.invoke('--help');
  assert.equal(result.status, 0, result.stderr);
});

test('SIGTERM reaches the CLI and the launcher waits for cooperative completion', async t => {
  const f = fixture(t, { waitForSignal: true });
  f.warm();
  const child = spawn(process.execPath, [LAUNCHER, 'runner'], {
    cwd: f.caller, env: f.env, stdio: ['ignore', 'pipe', 'pipe'],
  });
  t.after(() => { if (child.exitCode === null) child.kill('SIGTERM'); });
  let output = '';
  await new Promise((resolve, reject) => {
    child.on('error', reject);
    child.stdout.on('data', chunk => {
      output += chunk;
      if (output.includes('READY')) resolve();
    });
    child.once('exit', code => { if (!output.includes('READY')) reject(new Error('early exit ' + code)); });
  });
  const done = once(child, 'close');
  child.kill('SIGTERM');
  const [code] = await done;
  assert.equal(code, 0);
  assert.equal(fs.readFileSync(path.join(f.root, 'terminated'), 'utf8'), 'yes');
});

test('simultaneous first runs both receive a complete cached runtime', async t => {
  const f = fixture(t);
  const invoke = () => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [LAUNCHER, '--help'], { cwd: f.caller, env: f.env });
    let error = '';
    child.stderr.on('data', chunk => { error += chunk; });
    child.on('error', reject);
    child.on('close', code => resolve({ code, error }));
  });
  const results = await Promise.all([invoke(), invoke()]);
  for (const result of results) assert.equal(result.code, 0, result.error);
  const cache = path.join(f.env.XDG_CACHE_HOME, 'nanodot', 'npm');
  assert.deepEqual(fs.readdirSync(cache).sort(), ['python', 'uv-0.12.21']);
});
