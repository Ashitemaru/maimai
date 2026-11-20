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

`test:ci` runs the single-stylesheet check, TypeScript check and production build, then two browser suites against `dist/`:

- `verify:app` (`scripts/verify-app.mjs`) covers the core flows in named sections: `auth`, `list` (AP list browse, filters, search, paging), `detail` (pending record save/cancel and the 409 conflict reload), `completion`, `matrix` (default remaining display, drilldown), `preferences`, `score-sync`, `venues` (map focus, album, location editor, summary), `catalog` (update, review adjudication and revoke, Bilibili stale videos), `publishing` (QR login, order check, prepare, apply and retry) and `mobile` (390px layout and touch). Pass section names to run a subset: `node scripts/verify-app.mjs detail venues`.
- `verify:completion-export` checks the completion image export pixels and strip upload.

`verify:webkit` runs the `auth`, `detail`, `venues`, `publishing` and `mobile` sections in Playwright WebKit (`--webkit`). It does not certify physical iPhone Safari, the native share sheet or installed-app behaviour.

All browser sections share `scripts/mock-api.mjs`: one static server for `dist/`, one in-memory API with a small synthetic dataset (charts, venues, catalog review, Bilibili) and per-section route overrides via `app.api.on(...)`. Every external request is blocked (map tiles get a blank SVG), so tests never read `data/`, call personal services or write the user's database. `scripts/fixtures.mjs` provides the synthetic jackets and cover assets and substitutes the proprietary cover font with a system font. `scripts/browser.mjs` defaults to Chromium with software rendering; `PLAYWRIGHT_CHROMIUM_EXECUTABLE` can override the browser for local diagnosis.

## Formatting and lint

Run `npm run format` to apply Prettier to frontend sources, scripts, styles, configuration, and documentation. ESLint uses its recommended JavaScript and TypeScript rules, React Hooks dependency/rules checks, and the React Refresh component-export rule; CI treats warnings as failures. UI styling rules remain separately enforced by `check:styles`.

## Online home-screen app and iPhone delivery

The manifest declares a standalone application with generated 192/512px icons and a 180px Apple touch icon. `npm run generate:icons` renders the existing original `public/icon.svg`; no game artwork is bundled. The manifest and branded login shell are public; personal APIs, jackets, cover assets, and generated images require a valid server session.

This first deployment deliberately uses no Service Worker, offline cache, or queued writes. Opening the installed app requires a network connection and a Master Key login when its 30-day session has expired. Private API responses and generated images are not copied into Cache Storage or browser persistence. This avoids stale authenticated content surviving a session access change; server/cache headers remain a deployment responsibility.

On iPhone/iPad, image generation and saving are separate actions. The app keeps one complete PNG in memory, then offers a fresh-tap share button, a PNG download link, and a link to open the complete image for long-press saving. A dismissed share sheet leaves the image ready; other share errors retain the fallback links. Desktop browsers also keep these actions after starting the usual download. Leaving the view releases its Blob URL. Completion export cancellation aborts the in-flight requests; a later export discards any unfinished server job, and finishing returns the stitched PNG and deletes its parts. These changes preserve the full image, export dimensions, tile sizes, and cover composition.
