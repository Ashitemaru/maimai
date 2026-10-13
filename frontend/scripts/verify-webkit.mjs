import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { webkit } from 'playwright';

const root = path.resolve(import.meta.dirname, '../..');
const output = path.join(root, 'output/webkit-audit');
await fs.mkdir(output, { recursive: true });
const probe = await webkit.launch({ headless: true });
const version = probe.version();
await probe.close();
const suites = ['auth', 'score-sync', 'ap-buffer', 'venue-map', 'mobile-export'];
const results = [];
for (const suite of suites) {
  const result = spawnSync(
    process.execPath,
    [path.join(import.meta.dirname, `verify-${suite}.mjs`)],
    {
      stdio: 'inherit',
      env: {
        ...process.env,
        PLAYWRIGHT_BROWSER: 'webkit',
        AUTH_AUDIT_OUTPUT: path.join(output, 'auth'),
        SCORE_SYNC_AUDIT_OUTPUT: path.join(output, 'score-sync'),
        BUFFER_AUDIT_OUTPUT: path.join(output, 'ap-buffer'),
        VENUE_MAP_AUDIT_OUTPUT: path.join(output, 'venue-map'),
        MOBILE_EXPORT_AUDIT_OUTPUT: path.join(output, 'mobile-export'),
      },
    },
  );
  results.push({ suite, exitCode: result.status, error: result.error?.message || null });
  if (result.status !== 0) break;
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
if (results.some((result) => result.exitCode !== 0)) process.exitCode = 1;
