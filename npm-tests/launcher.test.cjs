'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');
const test = require('node:test');

const REPO = path.resolve(__dirname, '..');
const PLATFORMS = ['darwin-arm64', 'darwin-x64', 'linux-arm64', 'linux-x64'];
const UV_VERSION = '0.12.21';

function fakePython() {
  const fs = require('node:fs');
  const path = require('node:path');
  const config = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json')));
  const args = process.argv.slice(2);
  if (args[0] === '-I') {
    if (!MANAGED && !(config.workingPythons || []).includes(path.basename(__filename))) {
      process.exit(1);
    }
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
    fs.writeFileSync(temporary, fs.readFileSync(path.join(ROOT, 'managed-python-stub')), { mode: 0o755 });
    fs.renameSync(temporary, python);
    console.log('runtime setup output');
  } else {
    process.exit(2);
  }
}

function fakeCurl() {
  const fs = require('node:fs');
  const path = require('node:path');
  const config = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json')));
  const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'pkg', 'bin', 'uv-manifest.json')));
  const args = process.argv.slice(2);
  const url = `${manifest.baseUrl}/${manifest.assets['linux-x64'].name}`;
  if (args[0] !== '-q' || args.at(-1) !== url) {
    console.error('unexpected curl target: ' + args.join(' '));
    process.exit(3);
  }
  fs.appendFileSync(path.join(ROOT, 'downloads'), 'download\n');
  if (config.failDownload) process.exit(22);
  const bytes = config.failIntegrity || config.failExtract
    ? path.join(ROOT, 'garbage.bin') : path.join(ROOT, 'fixture.tar.gz');
  fs.copyFileSync(bytes, args[args.indexOf('--output') + 1]);
}

function fixture(t, options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nanodot npm test '));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const bin = path.join(root, 'bin');
  const caller = path.join(root, 'caller');
  fs.mkdirSync(bin);
  fs.mkdirSync(caller);
  // A real package copy: the launcher resolves its manifest and source
  // relative to its own location, so the fixture controls both.
  const pkg = path.join(root, 'pkg');
  fs.cpSync(path.join(REPO, 'bin'), path.join(pkg, 'bin'), { recursive: true });
  fs.cpSync(path.join(REPO, 'src'), path.join(pkg, 'src'), { recursive: true });
  const launcher = path.join(pkg, 'bin', 'nanodot.cjs');
  const source = path.join(pkg, 'src');

  const script = (fn, managed) => '#!' + process.execPath + '\nconst ROOT = ' + JSON.stringify(root)
    + ';\nconst MANAGED = ' + JSON.stringify(!!managed) + ';\n(' + fn.toString() + ')();\n';
  const write = (file, text) => fs.writeFileSync(file, text, { mode: 0o755 });
  write(path.join(root, 'python-stub'), script(fakePython));
  write(path.join(root, 'managed-python-stub'), script(fakePython, true));
  write(path.join(root, 'uv-stub'), script(fakeUv));
  write(path.join(bin, 'curl'), script(fakeCurl));
  // Python candidates always exist on PATH but fail the probe until warmed:
  // the stubs shadow any real interpreter the host provides, while tar and
  // friends still resolve from the host PATH.
  for (const name of ['python3', 'python3.12', 'python']) {
    write(path.join(bin, name), script(fakePython));
  }

  // The fixture archive publishes the uv stub; garbage.bin is digest-matched
  // when exercising a failing extraction, and digest-mismatched (against the
  // archive) when exercising the integrity check.
  const staged = path.join(root, 'staged');
  fs.mkdirSync(path.join(staged, 'uv-fixture'), { recursive: true });
  fs.copyFileSync(path.join(root, 'uv-stub'), path.join(staged, 'uv-fixture', 'uv'));
  fs.chmodSync(path.join(staged, 'uv-fixture', 'uv'), 0o755);
  const archive = path.join(root, 'fixture.tar.gz');
  const packed = spawnSync('tar', ['-czf', archive, '-C', staged, 'uv-fixture']);
  assert.equal(packed.status, 0, packed.stderr);
  const sha256 = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  fs.writeFileSync(path.join(root, 'garbage.bin'), 'not a tarball\n');
  const archiveDigest = sha256(archive);
  const repinArchive = () => {
    const shipped = path.join(pkg, 'bin', 'uv-manifest.json');
    const current = JSON.parse(fs.readFileSync(shipped, 'utf8'));
    for (const key of PLATFORMS) current.assets[key].sha256 = archiveDigest;
    fs.writeFileSync(shipped, JSON.stringify(current));
  };

  const manifest = {
    uvVersion: UV_VERSION,
    baseUrl: 'https://example.invalid/uv',
    assets: {},
  };
  for (const key of PLATFORMS) {
    manifest.assets[key] = {
      name: 'uv-fixture.tar.gz',
      sha256: options.failExtract ? sha256(path.join(root, 'garbage.bin')) : archiveDigest,
    };
  }
  fs.writeFileSync(path.join(pkg, 'bin', 'uv-manifest.json'), JSON.stringify(manifest));
  fs.writeFileSync(path.join(root, 'config.json'), JSON.stringify(options));

  const env = {
    ...process.env, PATH: bin + path.delimiter + process.env.PATH,
    XDG_CACHE_HOME: path.join(root, 'cache'),
    NANODOT_HOME: path.join(root, 'data'), PYTHONPATH: 'original-pythonpath',
  };
  const invoke = (...args) => spawnSync(process.execPath, [launcher, ...args], {
    cwd: caller, env, encoding: 'utf8', timeout: 15000,
  });
  const config = value => fs.writeFileSync(path.join(root, 'config.json'), JSON.stringify(value));
  const warm = name => {
    const current = JSON.parse(fs.readFileSync(path.join(root, 'config.json'), 'utf8'));
    current.workingPythons = [...(current.workingPythons || []), name || 'python3'];
    config(current);
  };
  const count = name => fs.existsSync(path.join(root, name))
    ? fs.readFileSync(path.join(root, name), 'utf8').trim().split('\n').length : 0;
  const cache = () => path.join(env.XDG_CACHE_HOME, 'nanodot', 'npm');
  return { root, pkg, source, env, invoke, warm, config, count, cache, repinArchive };
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
  assert.equal(call.args[2], f.source);
  assert.equal(call.cwd, fs.realpathSync(path.join(f.root, 'caller')));
  assert.equal(call.dataHome, f.env.NANODOT_HOME);
  assert.equal(call.pythonpath, f.source + path.delimiter + 'original-pythonpath');
  assert.equal(f.count('downloads'), 0);
});

test('an unsuitable Python does not hide an available supported interpreter', t => {
  const f = fixture(t);
  f.warm('python3.12');
  const result = f.invoke('--help');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(f.count('downloads'), 0);
});

test('stdin reaches the CLI for noninteractive secret entry', t => {
  const f = fixture(t, { readInput: true });
  f.warm();
  const result = spawnSync(process.execPath,
    [path.join(f.pkg, 'bin', 'nanodot.cjs'), 'config', 'set', 'github-token', '-'], {
      cwd: path.join(f.root, 'caller'), env: f.env,
      input: 'example-test-token\n', encoding: 'utf8', timeout: 15000,
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
  assert.deepEqual(fs.readdirSync(f.cache()).sort(), ['python', `uv-${UV_VERSION}`]);
});

test('a digest mismatch fails closed and publishes nothing', t => {
  const f = fixture(t, { failIntegrity: true });
  const result = f.invoke('--help');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /integrity check failed/);
  assert.equal(f.count('python-installs'), 0);
  assert.deepEqual(fs.readdirSync(f.cache()), [], 'a failed check must not publish uv');
  f.config({});
  const retry = f.invoke('--help');
  assert.equal(retry.status, 0, retry.stderr);
});

test('a failing extraction fails closed and publishes nothing', t => {
  const f = fixture(t, { failExtract: true });
  const result = f.invoke('--help');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /tar exited with status/);
  assert.equal(f.count('python-installs'), 0);
  assert.deepEqual(fs.readdirSync(f.cache()), []);
  f.config({});
  f.repinArchive();  // the manifest was pinned to the garbage digest
  const retry = f.invoke('--help');
  assert.equal(retry.status, 0, retry.stderr);
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

test('the shipped manifest pins a digest for every supported platform', () => {
  const manifest = require(path.join(REPO, 'bin', 'uv-manifest.json'));
  assert.match(manifest.baseUrl, /^https:\/\/github\.com\/astral-sh\/uv\/releases\/download\//);
  for (const key of PLATFORMS) {
    const asset = manifest.assets[key];
    assert.ok(asset, key);
    assert.match(asset.sha256, /^[0-9a-f]{64}$/, key);
    assert.equal(asset.name, asset.name.replace(/\.tar\.gz$/, '') + '.tar.gz');
  }
});

test('SIGTERM reaches the CLI and the launcher waits for cooperative completion', async t => {
  const f = fixture(t, { waitForSignal: true });
  f.warm();
  const child = spawn(process.execPath, [path.join(f.pkg, 'bin', 'nanodot.cjs'), 'runner'], {
    cwd: path.join(f.root, 'caller'), env: f.env, stdio: ['ignore', 'pipe', 'pipe'],
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
    const child = spawn(process.execPath, [path.join(f.pkg, 'bin', 'nanodot.cjs'), '--help'],
      { cwd: path.join(f.root, 'caller'), env: f.env });
    let error = '';
    child.stderr.on('data', chunk => { error += chunk; });
    child.on('error', reject);
    child.on('close', code => resolve({ code, error }));
  });
  const results = await Promise.all([invoke(), invoke()]);
  for (const result of results) assert.equal(result.code, 0, result.error);
  assert.deepEqual(fs.readdirSync(f.cache()).sort(), ['python', `uv-${UV_VERSION}`]);
});
