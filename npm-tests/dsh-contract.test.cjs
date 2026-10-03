'use strict';

// CLI contract test for the DSH tools integration: unlike dsh-plugin.test.cjs
// (fake binary, runner logic), this drives the REAL nanodot CLI through
// runTool, offline (dead proxies) and in an isolated data home. If the CLI's
// argument surface drifts away from the tool table, this file breaks — the
// fake-binary tests cannot see that.
//
// The CLI runs from the repo source via a python shim (the project is
// stdlib-only, no install needed). Requires python >= 3.11 on PATH; the test
// skips loudly when no supported interpreter exists.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const REPO = path.resolve(__dirname, '..');
const { NanodotToolError, runNanodot, runTool } = require('../integrations/dsh/nanodot-tools.cjs');

function findPython() {
  for (const candidate of ['python3.13', 'python3.12', 'python3.11', 'python3', 'python']) {
    const probe = spawnSync(candidate, ['-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)']);
    if (probe.status === 0) return candidate;
  }
  return null;
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nanodot dsh contract '));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const python = findPython();
  if (!python) return null;
  // A "nanodot binary" that runs the real CLI from the repo source — exactly
  // what bin/nanodot.cjs does, minus the runtime management.
  const shim = path.join(root, 'nanodot');
  const quote = (value) => "'" + value.replaceAll("'", "'\\''") + "'";
  fs.writeFileSync(shim, [
    '#!/bin/sh',
    `exec ${quote(python)} -c ${quote(
      'import sys; sys.path.insert(0, sys.argv.pop(1)); '
      + 'from nanodot.cli import main; raise SystemExit(main())',
    )} ${quote(path.join(REPO, 'src'))} "$@"`,
  ].join('\n'), { mode: 0o755 });
  const env = {
    NANODOT_HOME: path.join(root, 'home'),
    HTTP_PROXY: 'http://127.0.0.1:9', HTTPS_PROXY: 'http://127.0.0.1:9',
    ALL_PROXY: 'http://127.0.0.1:9', NO_PROXY: '', no_proxy: '',
    http_proxy: 'http://127.0.0.1:9', https_proxy: 'http://127.0.0.1:9',
    all_proxy: 'http://127.0.0.1:9',
  };
  const options = { binary: shim, env };
  return { root, options };
}

test('every tool speaks the real CLI: full offline watch lifecycle', async t => {
  const f = fixture(t);
  if (!f) { t.skip('no python >= 3.11 on PATH; CLI contract not exercised'); return; }

  // Credential-free, offline configuration first. Resolution is the
  // contract: config set prints nothing on success.
  await runNanodot(['config', 'set', 'github-auth-mode', 'anonymous'], f.options);

  // watch_add: the exact argument mapping the tool table promises.
  const add = await runTool('nanodot_watch_add',
    { target: 'thinkflowlab/nanodot#1', cadence: 1800, purpose: 'contract check' }, f.options);
  assert.match(add, /thinkflowlab\/nanodot#1/);

  const list = await runTool('nanodot_watch_list', {}, f.options);
  assert.match(list, /active/);
  const taskId = (list.match(/^([0-9a-f]+)\s/m) || [])[1];
  assert.ok(taskId, `task id not found in: ${list}`);

  assert.match(await runTool('nanodot_watch_show', { task_id: taskId }, f.options),
    new RegExp(taskId));
  // A never-ticked watch has an empty timeline — the log starts at the
  // first check-observed entry, not at creation.
  assert.match(await runTool('nanodot_activity', { task_id: taskId }, f.options),
    /no activity yet/);

  // watch_control: pause → resume → cancel, state visible through list.
  await runTool('nanodot_watch_control', { task_id: taskId, action: 'pause' }, f.options);
  assert.match(await runTool('nanodot_watch_list', {}, f.options), /paused/);
  await runTool('nanodot_watch_control', { task_id: taskId, action: 'resume' }, f.options);
  assert.match(await runTool('nanodot_watch_list', {}, f.options), /active/);
  await runTool('nanodot_watch_control', { task_id: taskId, action: 'cancel' }, f.options);
  assert.match(await runTool('nanodot_watch_list', {}, f.options), /cancelled|canceled/);

  // tick with nothing active runs zero tasks and exits cleanly.
  assert.match(await runTool('nanodot_tick', {}, f.options), /ran 0 task\(s\)/);

  assert.match(await runTool('nanodot_inbox', {}, f.options), /inbox is empty/);

  // status with no runner is a typed nonzero exit — the contract is the
  // error code, not prose matching.
  await assert.rejects(
    runTool('nanodot_status', {}, f.options),
    (error) => error instanceof NanodotToolError && error.code === 'NANODOT_EXIT_1',
  );
});

test('real CLI rejects what the tools would never send', async t => {
  const f = fixture(t);
  if (!f) { t.skip('no python >= 3.11 on PATH; CLI contract not exercised'); return; }
  await runNanodot(['config', 'set', 'github-auth-mode', 'anonymous'], f.options);

  // A malformed target must fail typed at the CLI boundary, proving the
  // error path crosses the seam as NANODOT_EXIT_<code>, not as prose.
  await assert.rejects(
    runTool('nanodot_watch_add', { target: 'not-a-pr-target' }, f.options),
    (error) => error instanceof NanodotToolError
      && /^NANODOT_EXIT_/.test(error.code),
  );
});
