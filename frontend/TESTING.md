# Frontend checks

Use Node.js 22.13 or newer. Install the exact dependency tree and the browser builds pinned by Playwright:

```sh
npm ci
npx playwright install --with-deps chromium webkit
npm run lint
npm run format:check
npm run test:ci
npm run verify:webkit
```

`test:ci` runs the single-stylesheet check, TypeScript check, production build, UI regression, completion matrix, preference persistence UI, catalog review, completion image export, detail concurrency, mobile saving, and Master Key authentication tests. All gate suites create their own local server or intercept every request. They use generated catalogs, original synthetic SVG/PNG artwork, and system fonts. They do not read `data/`, call personal services, or write the user's database. Preferences/catalog writes and temporary image jobs are in-memory fixtures.

The cover UI check substitutes the proprietary cover font with a system font inside the test browser. It verifies rendering readiness, controls, and output dimensions, not the appearance of unpublished game assets. The completion export suite retains the exact per-pixel comparison between a 400-chart reference image and its concatenated strips, plus bounded-canvas checks through 5,600 charts. The PNG badge fixture is generated from RGBA pixels with Node’s built-in zlib so SVG rasterization caches cannot affect strip/reference comparisons. Browser scripts use `scripts/browser.mjs`, which defaults to Chromium with software rendering (`--disable-gpu`) for repeatable pixel comparisons. `verify:webkit` selects the actual Playwright WebKit engine for the mobile workflows listed below. Neither engine substitutes for physical iPhone acceptance.

`PLAYWRIGHT_CHROMIUM_EXECUTABLE` (or the older `CHROME_EXECUTABLE`) can override the browser for local diagnosis. Leave both unset in CI so the pinned Playwright browser is used. No Codex runtime or macOS Chrome installation is required. Ubuntu's Playwright dependencies include the Liberation Sans system font used by the cover fixture; macOS uses Arial.

Reports and screenshots are written under `../output/ui-audit`, `../output/completion-matrix-audit`, `../output/preferences-audit`, and `../output/catalog-review-batch` by default. The export suite prints its canvas/strip results. Use the output environment variables at the top of each script to isolate concurrent runs.

## Batch cover generation

`npm run render:covers -- [manifest.json] [--output DIR] [--origin URL]` runs the
current Canvas renderer for both approved compositions: v8 at 1920×1080 and the
retained 4:3 composition at 1440×1080. Each aspect is rendered independently;
neither requires a previous output directory. The default destination is
`../output/bilibili-covers-current`, so the final v8 delivery archive stays intact.

The default input is `../data/bilibili-regenerate/render-manifest.json`. It must
provide `videos` and `excluded` arrays; each video includes its BVID, title, chart,
and explicit achievement string. Optional `versionOverride` and
`mirror_horizontal` are honored. Scores are never inferred, and excluded videos
are not rendered. The copied input manifest and `report.json` record renderer and
input hashes, per-image hashes, dimensions, resource failures, and checks for both
layouts. Horizontal mirroring is checked pixel by pixel.

The tool starts and stops a local Vite server by default (`--port 5175`); the local
backend on port 8766 must already serve the private jackets and game assets. Use
`--origin` (or `COVER_BATCH_ORIGIN`) for an existing local Vite server. Rendering
uses the shared browser launcher with normal GPU rendering. It is a manual asset
check, not a CI gate: missing fonts, backgrounds, logos, or score sprites fail the
run instead of silently producing substitute covers. The public checkout does
not include those private assets. The old v3/v4/v6 checks and v7 wrapper were
removed because their geometry and central-crop assertions predate the accepted
independent 16:9 and 4:3 layouts.

## Formatting and lint

Run `npm run format` to apply Prettier to frontend sources, scripts, styles, configuration, and documentation. ESLint uses its recommended JavaScript and TypeScript rules, React Hooks dependency/rules checks, and the React Refresh component-export rule; CI treats warnings as failures. UI styling rules remain separately enforced by `check:styles`.

## Online home-screen app and iPhone delivery

The manifest declares a standalone application with generated 192/512px icons and a 180px Apple touch icon. `npm run generate:icons` renders the existing original `public/icon.svg`; no game artwork is bundled. The manifest and branded login shell are public; personal APIs, jackets, cover assets, and generated images require a valid server session.

This first deployment deliberately uses no Service Worker, offline cache, or queued writes. Opening the installed app requires a network connection and a Master Key login when its 30-day session has expired. Private API responses and generated images are not copied into Cache Storage or browser persistence. This avoids stale authenticated content surviving a session access change; server/cache headers remain a deployment responsibility.

On iPhone/iPad, image generation and saving are separate actions. The app keeps one complete PNG in memory, then offers a fresh-tap share button, a PNG download link, and a link to open the complete image for long-press saving. A dismissed share sheet leaves the image ready; other share errors retain the fallback links. Desktop browsers also keep these actions after starting the usual download. Leaving the view releases its Blob URL. Completion export cancellation, generation failures, and finished-image fetch failures delete the temporary server job; a successfully fetched PNG also releases its server job. These changes preserve the full image, export dimensions, tile sizes, and cover composition.

`verify:detail-concurrency` checks out-of-order detail responses and revision conflicts using isolated in-memory records. `verify:mobile-export` checks touch viewports, gesture-bound single-file sharing, cancellation/fallback, manifest/icons, and asset recovery with injected browser APIs. Chromium emulation does not establish real iPhone Safari, iOS share-sheet, or installed-app session behavior; device acceptance remains a separate deployment check.

`verify:webkit` runs the authentication, score sync, AP buffer, venue map, and mobile export suites in WebKit. It checks cookie session restoration after refresh, actual `jsQR` screenshot decoding, resuming a server-side sync job after reload, venue selection and record locking, touch input on the map, and downloading the complete PNG with an exact content hash. The cover checks include prefilled achievements and AP/AP+ status, no inferred AP from a high score, missing-score handling, and editable historical values. The share API remains an injected test double, so these tests do not certify the native iOS share sheet, Photos saving, multi-finger gestures, or installed-app lifecycle. Those physical-device checks are deferred by the user. The engine version, limitations, suite results, and screenshots are recorded under `../output/webkit-audit`.

## Master Key entry

`verify:auth` runs against an isolated cookie-backed fixture server. It verifies that the application never mounts or requests personal data before authentication, invalid and rate-limited key messages, show/hide and keyboard submission, loading states, cookie persistence through refresh, logout, expired-session 401 handling, and failed-session-request recovery. The key is kept only in form memory and never written to localStorage, sessionStorage, or a JavaScript-readable cookie. Desktop, 320px, and 390px screenshots and a report are written under `../output/auth-ui-audit`. These UI fixtures do not establish backend cookie security, which is verified by the backend authentication suite.

## Venue atlas

`verify:venue-map` covers SVG jacket clicks, paginated AP details, cancel without
writes, explicit place search, failed-save recovery, persistence through reload,
search-result creation with a separate display name, minimal location payloads, and map selection at a 390px viewport. Every external map
tile is replaced by a synthetic SVG during tests; no map-provider prefetching is
performed. The AP buffer regression verifies empty initial venue selection, choosing only existing venues, and locking confirmed venue records. Reports are saved under
`../output/venue-map-audit`.

## Game score sync

`verify:score-sync` decodes the original synthetic QR fixture with `jsQR` while
`BarcodeDetector` is unavailable. It checks that no image is uploaded, only QR text
is sent to the score endpoint, and the text is cleared after submission without
browser persistence. It covers unavailable and unverified states, asynchronous
job polling across page navigation, AP-buffer refresh, expired codes, failed jobs,
and unreadable images. Uploading to DivingFish requires a saved Token and a prior
Wahlap snapshot; it is a separate explicit action, reports skipped records, and
never calls the old score-pull endpoint. The fixture makes no requests to game
servers and does not prove that the live worker can read a real account or close
a game session. Reports and 390px/desktop screenshots are saved under
`../output/score-sync-audit`.
