import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { webkit } from 'playwright';

const root = path.resolve(import.meta.dirname, '../..');
const output = path.resolve(
  process.env.WEBKIT_AUDIT_OUTPUT || path.join(root, 'output/webkit-audit'),
);
await fs.mkdir(output, { recursive: true });
const suites = ['auth', 'score-sync', 'ap-buffer', 'venue-map', 'mobile-export', 'publishing'];
const results = [];
let version = null;
let activeSuite = 'browser-startup';

function reportFailure(result, diagnostics) {
  const tail = diagnostics.trim().split('\n').slice(-100).join('\n').slice(-12000);
  const message = [
    `WebKit ${result.suite} failed (exit ${result.exitCode ?? 'none'}, signal ${result.signal || 'none'}).`,
    tail || result.error || 'The child process exited without diagnostic output.',
  ].join('\n');
  console.error(message);
  if (process.env.GITHUB_ACTIONS) {
    const escaped = message.replaceAll('%', '%25').replaceAll('\r', '%0D').replaceAll('\n', '%0A');
    console.error(`::error title=WebKit ${result.suite} verification::${escaped}`);
  }
}

async function saveLogs(suite, stdout, stderr, combined) {
  const logs = {
    stdout: `${suite}.stdout.log`,
    stderr: `${suite}.stderr.log`,
    combined: `${suite}.log`,
  };
  await Promise.all(
    Object.entries({ stdout, stderr, combined }).map(([stream, data]) =>
      fs.writeFile(path.join(output, logs[stream]), data),
    ),
  );
  return logs;
}

async function runSuite(suite) {
  console.log(`WebKit: running ${suite}`);
  const stdout = [];
  const stderr = [];
  const combined = [];
  let error = null;
  const child = spawn(process.execPath, [path.join(import.meta.dirname, `verify-${suite}.mjs`)], {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      ...process.env,
      PLAYWRIGHT_BROWSER: 'webkit',
      AUTH_AUDIT_OUTPUT: path.join(output, 'auth'),
      SCORE_SYNC_AUDIT_OUTPUT: path.join(output, 'score-sync'),
      BUFFER_AUDIT_OUTPUT: path.join(output, 'ap-buffer'),
      VENUE_MAP_AUDIT_OUTPUT: path.join(output, 'venue-map'),
      MOBILE_EXPORT_AUDIT_OUTPUT: path.join(output, 'mobile-export'),
      PUBLISHING_AUDIT_OUTPUT: path.join(output, 'publishing'),
    },
  });
  child.stdout.on('data', (chunk) => {
    stdout.push(chunk);
    combined.push(chunk);
    process.stdout.write(chunk);
  });
  child.stderr.on('data', (chunk) => {
    stderr.push(chunk);
    combined.push(chunk);
    process.stderr.write(chunk);
  });
  child.on('error', (failure) => {
    error = failure.stack || String(failure);
  });
  const outcome = await new Promise((resolve) => {
    child.on('close', (exitCode, signal) => resolve({ exitCode, signal }));
  });
  if (error) {
    const chunk = Buffer.from(`${error}\n`);
    stderr.push(chunk);
    combined.push(chunk);
    process.stderr.write(chunk);
  }
  const logs = await saveLogs(
    suite,
    Buffer.concat(stdout),
    Buffer.concat(stderr),
    Buffer.concat(combined),
  );
  const result = { suite, ...outcome, error, logs };
  results.push(result);
  if (result.exitCode !== 0 || error) {
    reportFailure(result, Buffer.concat(combined).toString('utf8'));
    return false;
  }
  return true;
}

try {
  const probe = await webkit.launch({ headless: true });
  version = probe.version();
  await probe.close();
  for (const suite of suites) {
    activeSuite = suite;
    if (!(await runSuite(suite))) break;
  }
} catch (failure) {
  const error = failure.stack || String(failure);
  const logs = await saveLogs(activeSuite, '', error, error);
  const result = { suite: activeSuite, exitCode: 1, signal: null, error, logs };
  results.push(result);
  reportFailure(result, error);
}
await fs.writeFile(
  path.join(output, 'report.json'),
  JSON.stringify(
    {
      engine: 'webkit',
      version,
      physicalDevice: false,
      nativeShareSheet: false,
      network: 'isolated fixtures',
      results,
    },
    null,
    2,
  ),
);
if (results.some((result) => result.exitCode !== 0 || result.error)) process.exitCode = 1;
