import { chromium } from 'playwright';

// Use Playwright's pinned Chromium by default, including in CI. An explicit
// executable is useful for local diagnosis but is not the reproducible gate.
export function launchBrowser({ softwareRendering = true } = {}) {
  const executablePath =
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || process.env.CHROME_EXECUTABLE;
  return chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
    // Canvas strip/reference comparisons must use the same software renderer.
    args: softwareRendering ? ['--disable-gpu'] : [],
  });
}
