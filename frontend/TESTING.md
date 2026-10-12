# Frontend checks

Use Node.js 22.13 or newer. Install the exact dependency tree and the Chromium build pinned by Playwright:

```sh
npm ci
npx playwright install --with-deps chromium
npm run lint
npm run format:check
npm run test:ci
```

`test:ci` runs the single-stylesheet check, TypeScript check, production build, UI regression, completion matrix, preference persistence UI, catalog review, completion image export, detail concurrency, mobile saving, and Master Key authentication tests. All gate suites create their own local server or intercept every request. They use generated catalogs, original synthetic SVG/PNG artwork, and system fonts. They do not read `data/`, call personal services, or write the user's database. Preferences/catalog writes and temporary image jobs are in-memory fixtures.

The cover UI check substitutes the proprietary cover font with a system font inside the test browser. It verifies rendering readiness, controls, and output dimensions, not the appearance of unpublished game assets. The completion export suite retains the exact per-pixel comparison between a 400-chart reference image and its concatenated strips, plus bounded-canvas checks through 5,600 charts. The PNG badge fixture is generated from RGBA pixels with Node’s built-in zlib so SVG rasterization caches cannot affect strip/reference comparisons. All browser test scripts use `scripts/browser.mjs` with software rendering (`--disable-gpu`) so GPU rasterization does not change the pixel comparison. Browser viewport tests include desktop and narrow layouts; these are Chromium checks, not iOS Safari or PWA certification.

`PLAYWRIGHT_CHROMIUM_EXECUTABLE` (or the older `CHROME_EXECUTABLE`) can override the browser for local diagnosis. Leave both unset in CI so the pinned Playwright browser is used. No Codex runtime or macOS Chrome installation is required. Ubuntu's Playwright dependencies include the Liberation Sans system font used by the cover fixture; macOS uses Arial.

Reports and screenshots are written under `../output/ui-audit`, `../output/completion-matrix-audit`, `../output/preferences-audit`, and `../output/catalog-review-batch` by default. The export suite prints its canvas/strip results. Use the output environment variables at the top of each script to isolate concurrent runs.

## Legacy manual checks

`verify-cover-v3.mjs`, `verify-cover-v4.mjs`, and `verify-cover-v6.mjs` require a private local `data/catalog/catalog.json`, unpublished game assets, and an existing Vite server (`COVER_QA_ORIGIN`, default `http://127.0.0.1:5174`). They preserve historical layout-specific assertions and are deliberately excluded from `test:ci`. `npm run verify:cover:legacy` selects v6. These scripts are not evidence that the current public checkout includes or validates those private assets.

`render-bilibili-covers*.mjs` are manual batch tools requiring local rendering manifests and assets. They also use the shared browser launcher, retain normal GPU rendering, and are not test gates.

## Formatting and lint

Run `npm run format` to apply Prettier to frontend sources, scripts, styles, configuration, and documentation. ESLint uses its recommended JavaScript and TypeScript rules, React Hooks dependency/rules checks, and the React Refresh component-export rule; CI treats warnings as failures. UI styling rules remain separately enforced by `check:styles`.

## Online home-screen app and iPhone delivery

The manifest declares a standalone application with generated 192/512px icons and a 180px Apple touch icon. `npm run generate:icons` renders the existing original `public/icon.svg`; no game artwork is bundled. The manifest and branded login shell are public; personal APIs, jackets, cover assets, and generated images require a valid server session.

This first deployment deliberately uses no Service Worker, offline cache, or queued writes. Opening the installed app requires a network connection and a Master Key login when its 30-day session has expired. Private API responses and generated images are not copied into Cache Storage or browser persistence. This avoids stale authenticated content surviving a session access change; server/cache headers remain a deployment responsibility.

On iPhone/iPad, image generation and saving are separate actions. The app keeps one complete PNG in memory, then offers a fresh-tap share button, a PNG download link, and a link to open the complete image for long-press saving. A dismissed share sheet leaves the image ready; other share errors retain the fallback links. Desktop browsers also keep these actions after starting the usual download. Leaving the view releases its Blob URL. Completion export cancellation, generation failures, and finished-image fetch failures delete the temporary server job; a successfully fetched PNG also releases its server job. These changes preserve the full image, export dimensions, tile sizes, and cover composition.

`verify:detail-concurrency` checks out-of-order detail responses and revision conflicts using isolated in-memory records. `verify:mobile-export` checks touch viewports, gesture-bound single-file sharing, cancellation/fallback, manifest/icons, and asset recovery with injected browser APIs. Chromium emulation does not establish real iPhone Safari, iOS share-sheet, or installed-app session behavior; device acceptance remains a separate deployment check.

## Master Key entry

`verify:auth` runs against an isolated cookie-backed fixture server. It verifies that the application never mounts or requests personal data before authentication, invalid and rate-limited key messages, show/hide and keyboard submission, loading states, cookie persistence through refresh, logout, expired-session 401 handling, and failed-session-request recovery. The key is kept only in form memory and never written to localStorage, sessionStorage, or a JavaScript-readable cookie. Desktop, 320px, and 390px screenshots and a report are written under `../output/auth-ui-audit`. These UI fixtures do not establish backend cookie security, which is verified by the backend authentication suite.

## Venue atlas

`verify:venue-map` covers SVG jacket clicks, paginated AP details, cancel without
writes, explicit place search, failed-save recovery, persistence through reload,
address-only creation, and map selection at a 390px viewport. Every external map
tile is replaced by a synthetic SVG during tests; no map-provider prefetching is
performed. The AP buffer regression also verifies that dismissing a new venue's
location form preserves its already-saved pending record. Reports are saved under
`../output/venue-map-audit`.
