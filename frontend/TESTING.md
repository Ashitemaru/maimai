# Frontend checks

Use Node.js 22.13 or newer. Install the exact dependency tree and the Chromium build pinned by Playwright:

```sh
npm ci
npx playwright install --with-deps chromium
npm run lint
npm run format:check
npm run test:ci
```

`test:ci` runs the single-stylesheet check, TypeScript check, production build, UI regression, completion matrix, preference persistence UI, catalog review, and completion image export tests. All gate suites create their own local server or intercept every request. They use generated catalogs, original synthetic SVG/PNG artwork, and system fonts. They do not read `data/`, call personal services, or write the user's database. Preferences/catalog writes and temporary image jobs are in-memory fixtures.

The cover UI check substitutes the proprietary cover font with a system font inside the test browser. It verifies rendering readiness, controls, and output dimensions, not the appearance of unpublished game assets. The completion export suite retains the exact per-pixel comparison between a 400-chart reference image and its concatenated strips, plus bounded-canvas checks through 5,600 charts. The PNG badge fixture is generated from RGBA pixels with Node’s built-in zlib so SVG rasterization caches cannot affect strip/reference comparisons. All browser test scripts use `scripts/browser.mjs` with software rendering (`--disable-gpu`) so GPU rasterization does not change the pixel comparison. Browser viewport tests include desktop and narrow layouts; these are Chromium checks, not iOS Safari or PWA certification.

`PLAYWRIGHT_CHROMIUM_EXECUTABLE` (or the older `CHROME_EXECUTABLE`) can override the browser for local diagnosis. Leave both unset in CI so the pinned Playwright browser is used. No Codex runtime or macOS Chrome installation is required. Ubuntu's Playwright dependencies include the Liberation Sans system font used by the cover fixture; macOS uses Arial.

Reports and screenshots are written under `../output/ui-audit`, `../output/completion-matrix-audit`, `../output/preferences-audit`, and `../output/catalog-review-batch` by default. The export suite prints its canvas/strip results. Use the output environment variables at the top of each script to isolate concurrent runs.

## Legacy manual checks

`verify-cover-v3.mjs`, `verify-cover-v4.mjs`, and `verify-cover-v6.mjs` require a private local `data/catalog/catalog.json`, unpublished game assets, and an existing Vite server (`COVER_QA_ORIGIN`, default `http://127.0.0.1:5174`). They preserve historical layout-specific assertions and are deliberately excluded from `test:ci`. `npm run verify:cover:legacy` selects v6. These scripts are not evidence that the current public checkout includes or validates those private assets.

`render-bilibili-covers*.mjs` are manual batch tools requiring local rendering manifests and assets. They also use the shared browser launcher, retain normal GPU rendering, and are not test gates.

## Formatting and lint

Run `npm run format` to apply Prettier to frontend sources, scripts, styles, configuration, and documentation. ESLint uses its recommended JavaScript and TypeScript rules, React Hooks dependency/rules checks, and the React Refresh component-export rule; CI treats warnings as failures. UI styling rules remain separately enforced by `check:styles`.
