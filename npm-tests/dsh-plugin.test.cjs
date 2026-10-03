'use strict';

// Offline tests for the DSH tools integration (integrations/dsh/):
// the tool table and the CLI runner run against a fake nanodot binary.
// The Cordis registration shim (index.ts) is not exercised here — see the
// integration README for its verification status.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { TOOLS, NanodotToolError, runTool } = require('../integrations/dsh/nanodot-tools.cjs');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nanodot dsh test '));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const bin = path.join(root, 'nanodot');
  fs.writeFileSync(bin, [
    '#!' + process.execPath,
    `const fs = require('node:fs');`,
    `const argv = process.argv.slice(2);`,
    `fs.appendFileSync(${JSON.stringify(path.join(root, 'calls.jsonl'))},`,
    `  JSON.stringify({ argv, home: process.env.NANODOT_HOME }) + '\\n');`,
    `if (argv.includes('--fail')) { console.error('boom from stderr'); process.exit(7); }`,
    `else if (argv.includes('--hang')) { setInterval(() => {}, 1000); }`,
    `else if (argv.includes('--stubborn')) {`,
    `  process.on('SIGTERM', () => {}); setInterval(() => {}, 1000);`,
    `}`,
    `else if (argv.includes('--chatty')) { console.log('x'.repeat(20000)); }`,
    `else { console.log('fake nanodot output'); }`,
  ].join('\n'), { mode: 0o755 });
  const calls = () => fs.readFileSync(path.join(root, 'calls.jsonl'), 'utf8')
    .trim().split('\n').map((line) => JSON.parse(line));
  return { root, bin, calls };
}

test('the tool table is a stable, complete surface', () => {
  assert.deepEqual(TOOLS.map((tool) => tool.name), [
    'nanodot_watch_add', 'nanodot_watch_list', 'nanodot_watch_show',
    'nanodot_watch_control', 'nanodot_status', 'nanodot_inbox',
    'nanodot_activity', 'nanodot_tick',
  ]);
  for (const tool of TOOLS) {
    assert.equal(typeof tool.argv, 'function', tool.name);
    assert.ok(tool.description.length > 40, `${tool.name} needs a model-facing description`);
  }
});

test('watch_add maps arguments exactly, injection strings stay one argv entry', async t => {
  const f = fixture(t);
  const output = await runTool('nanodot_watch_add',
    { target: 'owner/repo#1; echo unsafe', cadence: 120, purpose: 'a "quoted" purpose' },
    { binary: f.bin, env: { NANODOT_HOME: '/tmp/home-x' } });
  assert.equal(output, 'fake nanodot output');
  const [call] = f.calls();
  assert.deepEqual(call.argv, [
    'watch', 'add', 'owner/repo#1; echo unsafe', '--yes',
    '--cadence', '120', '--purpose', 'a "quoted" purpose',
  ]);
  assert.equal(call.home, '/tmp/home-x');
});

test('tools without parameters take no arguments they did not ask for', async t => {
  const f = fixture(t);
  await runTool('nanodot_tick', {}, { binary: f.bin });
  await runTool('nanodot_inbox', {}, { binary: f.bin });
  await runTool('nanodot_status', {}, { binary: f.bin });
  await runTool('nanodot_watch_list', {}, { binary: f.bin });
  assert.deepEqual(f.calls().map((call) => call.argv), [
    ['runner', '--once'], ['inbox'], ['status'], ['watch', 'list'],
  ]);
});

test('watch_control accepts only its enum and maps to the subcommand', async t => {
  const f = fixture(t);
  await runTool('nanodot_watch_control', { task_id: 't9', action: 'pause' }, { binary: f.bin });
  assert.deepEqual(f.calls()[0].argv, ['watch', 'pause', 't9']);
  await assert.rejects(
    runTool('nanodot_watch_control', { task_id: 't9', action: 'destroy' }, { binary: f.bin }),
    (error) => error instanceof NanodotToolError && error.code === 'NANODOT_BAD_PARAMETER',
  );
});

test('missing, unknown, and mistyped parameters fail before any process starts', async t => {
  const f = fixture(t);
  await assert.rejects(
    runTool('nanodot_watch_show', {}, { binary: f.bin }),
    (error) => error.code === 'NANODOT_MISSING_PARAMETER',
  );
  await assert.rejects(
    runTool('nanodot_watch_list', { extra: 1 }, { binary: f.bin }),
    (error) => error.code === 'NANODOT_BAD_PARAMETER',
  );
  await assert.rejects(
    runTool('nanodot_watch_add', { target: 'x', cadence: 'soon' }, { binary: f.bin }),
    (error) => error.code === 'NANODOT_BAD_PARAMETER',
  );
  await assert.rejects(
    runTool('nanodot_nope', {}, { binary: f.bin }),
    (error) => error.code === 'NANODOT_UNKNOWN_TOOL',
  );
  assert.equal(fs.existsSync(path.join(f.root, 'calls.jsonl')), false);
});

test('a nonzero exit is a typed error carrying bounded stderr as data', async t => {
  const f = fixture(t);
  // --fail path: the target carries the marker into argv
  await assert.rejects(
    runTool('nanodot_watch_add', { target: '--fail' }, { binary: f.bin }),
    (error) => error instanceof NanodotToolError
      && error.code === 'NANODOT_EXIT_7'
      && error.message.includes('boom from stderr'),
  );
});

test('a hung call is terminated at the timeout and typed', async t => {
  const f = fixture(t);
  await assert.rejects(
    runTool('nanodot_watch_add', { target: '--hang' }, { binary: f.bin, timeoutMs: 300 }),
    (error) => error.code === 'NANODOT_TERMINATED',
  );
});

test('aborting the DSH signal terminates the child and types the error', async t => {
  const f = fixture(t);
  const controller = new AbortController();
  const pending = runTool('nanodot_watch_add', { target: '--hang' },
    { binary: f.bin, signal: controller.signal });
  setTimeout(() => controller.abort(), 200);
  await assert.rejects(pending, (error) => error.code === 'NANODOT_TERMINATED');
});

test('a missing binary is a spawn failure, not a hang', async t => {
  await assert.rejects(
    runTool('nanodot_status', {}, { binary: '/nonexistent/nanodot', timeoutMs: 2000 }),
    (error) => error.code === 'NANODOT_SPAWN_FAILED',
  );
});

test('successful output is capped — a huge timeline cannot flood the model', async t => {
  const f = fixture(t);
  const output = await runTool('nanodot_watch_add', { target: '--chatty' }, { binary: f.bin });
  assert.ok(output.startsWith('…'), 'truncated output is marked');
  assert.ok(output.length <= 8001, `output was ${output.length} chars`);
  assert.ok(output.endsWith('xxxx'));
});

test('a SIGTERM-ignoring child is SIGKILLed after the grace period', async t => {
  const f = fixture(t);
  const started = Date.now();
  await assert.rejects(
    runTool('nanodot_watch_add', { target: '--stubborn' },
      { binary: f.bin, timeoutMs: 150, killGraceMs: 300 }),
    (error) => error.code === 'NANODOT_TERMINATED',
  );
  const elapsed = Date.now() - started;
  assert.ok(elapsed < 5000, `escalation took ${elapsed}ms`);
});
