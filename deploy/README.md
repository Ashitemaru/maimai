# Server deployment

The server polls public GitHub main every two minutes. It activates only a completed,
successful `push` run of `ci.yml` in the same repository, plus the exact release tag
`app-<40 lowercase hex SHA>`. The tag must resolve to that commit. Required release
assets are `mai-journal-<SHA>.tar.gz` and `SHA256SUMS` (sha256sum format). Downloads
and redirects use HTTPS GitHub hosts only; the archive checksum, paths, file types,
resource bounds and `RELEASE_SHA` are validated before installing dependencies.

The archive has no enclosing directory. It contains `backend/*.py` excluding tests,
the core `scripts/*.py` allowlist in `deploy_release.py`, `requirements.lock`,
`frontend/dist/**`, `RELEASE_SHA`, a required `dependencies/` wheel bundle, and
optionally `deploy/**` for manual bootstrap updates. It must contain no personal
runtime data, catalogs, pictures, credentials, symlinks or private records.
`dependencies/` contains only wheels and their public `manifest.json`.
The only allowed deployment JSON is the public
`deploy/wahlap-runtime.lock.json` manifest; the external score runtime and its
binary files never enter a release. Requirements are exact version pins with no URLs or
pip options. CI must lint `deploy` and run `tests.test_deploy` alongside other tests.

## One-time root bootstrap

1. Install CPython 3.14, its venv package, CA certificates and sudo on Linux x86_64
   with glibc at least as new as the Ubuntu 24.04 wheel-builder runner (2.39).
   The manifest records the actual builder version and the deployer checks it.
   Create the locked service account
   `maimai` with no interactive login. Create `/srv/mai-journal` owned by `maimai`,
   mode 0700, and `releases`, `backups`, `shared/runtime`, `shared/catalog`,
   `shared/assets` beneath it. Existing private runtime/catalog files remain 0600;
   all application and deployment files run as the same uid, so no shared-user
   permission relaxation is needed.
2. Restore the consistent personal SQLite database, full catalog/current snapshot,
   adjudications database and referenced public asset pack into `shared`. The app
   rejects an uninitialized production database/catalog. Keep credentials in runtime
   or root-owned `app.env`, never in code/releases. Validate migrated data separately.
3. Install the reviewed deployer at `/opt/mai-journal/deploy_release.py`, root-owned,
   mode 0755. Copy the three unit files into `/etc/systemd/system`, root-owned 0644.
   Normal releases do not automatically update these root-owned bootstrap files.
4. Create `/etc/mai-journal/app.env` and `deploy.env` from the examples, root-owned
   0600. Set the actual public repository and allowed HTTPS hostname. Generate
   `MAIMAI_MASTER_KEY_HASH` with `python -m backend.auth hash`, supplying the key
   through stdin. Keep the key in a password manager, never in shell arguments,
   code or releases. Production refuses to start without a valid hash. The app
   unit adds production paths and reads the release SHA from `current/release.env`.
5. Install the reviewed sudoers file at `/etc/sudoers.d/mai-journal`, root-owned
   0440, and validate with `visudo -cf`. It permits only start/stop/restart of the
   exact application service. The application unit's `NoNewPrivileges` prevents
   it from using sudo; its filesystem sandbox permits writes only to catalog and
   runtime. The deployment unit can write release/current/backup paths.
6. Run `systemctl daemon-reload`, then `systemctl start mai-journal-deploy.service`.
   Inspect its journal and `/api/ready` through localhost. Enable
   `mai-journal.service` and `mai-journal-deploy.timer` only after initial validation.
   Configure Caddy from `Caddyfile.example`; never expose 8766 publicly. The app's
   login page uses one Master Key, without usernames. Caddy must overwrite the
   forwarded headers as shown so clients cannot forge the login rate-limit IP.
   Remove any old Basic Auth proxy gate only after this authenticated application
   is healthy. Do not roll back to a pre-authentication release afterward.

The login page and its static bundle/icons are public. Personal APIs, jackets,
cover assets and exported images require an authenticated session. A successful
login creates a Secure, HttpOnly, SameSite=Lax cookie valid for 30 days; only its
token digest is stored in `shared/runtime/auth.sqlite3`. Sessions survive restarts,
logout revokes the session and changing the configured key hash invalidates all
existing sessions. The Master Key is not stored in browser local storage.

Let the application own Cache-Control; do not add a blanket no-store header in
Caddy. The example enables gzip/zstd for HTML, JSON and scripts. Personal API
responses and exports remain no-store. Authenticated jacket previews use versioned
160px WebP URLs with a private immutable browser cache and conditional GET support;
the original PNG URLs are preserved for image export and revalidate when reused.
Unversioned cover artwork also revalidates; hashed frontend bundles can be cached.
Derived previews live in shared/runtime/jacket-thumbnails, capped at 64 MiB and
4,096 files. They are disposable and regenerated from catalog artwork after eviction.

## Activation and recovery

CI downloads the exact `requirements.lock` pins plus pinned pip into an isolated
wheelhouse on CPython 3.14 / Ubuntu 24.04 x86_64. It installs those wheels using the
same server installer with `--no-index --require-hashes --no-deps`, runs `pip check`
and imports the runtime/native modules before publishing. Missing wheels or
incomplete dependency pins fail CI. The immutable release contains this tested
bundle; server releases never download packages from PyPI.
To reproduce this build on the matching Linux host, run
`python -m scripts.build_release_dependencies` from the repository root, then
`python scripts/package_release.py`. The generated `release-dependencies/` and
`release/` directories remain untracked.

Before installation, the deployer validates every wheel's hash and package/version,
the exact bundle file set, the requirements digest, Python minor version, CPU
architecture, OS and minimum glibc version. Dependencies are installed into the
final candidate release directory before the old app is stopped; venvs are never
moved after installation. A missing, incompatible or corrupted bundle fails closed
without a network fallback or service stop. Installed older releases remain usable
for rollback, but a fresh deployment of a historical code-only archive requires
rebuilding it with the current packaging workflow under a new commit/tag.

Main is rechecked after preparation. A filesystem flock serializes the complete
deployment. The current
SHA is a no-op, so ordinary timer ticks make just one unauthenticated GitHub API
request. GitHub errors/rate limits keep the running version and are retried on a
later timer run; a release in progress is not an error.

The deployer writes a recovery journal, stops the single application service
(210-second systemd budget, allowing an active game sync to finish session cleanup), makes SQLite online-backup copies of personal and
adjudications databases (plus the session database when present), changes
`current` atomically and starts the new code.
It accepts `/api/ready` only when `ok` is true and `release_sha` matches. A failed
activation stops the new process before restoring the old pointer and restarting
it. Rollback never overwrites personal data. An interrupted activation is rolled
back from the journal on the next timer run before another deployment is tried.
An activation failure or interrupted activation records `failed-release.json`.
Timer ticks keep the recovered app running and skip that same SHA, avoiding a
two-minute cycle of outages. A different main SHA is eligible automatically.
After an operator fixes a transient environment problem, explicitly retry with
`sudo -u maimai /usr/bin/python3 /opt/mai-journal/deploy_release.py --repository OWNER/REPOSITORY --retry-failed`.
This still requires successful main CI, an exact release and all artifact checks.
Keep the previous release directory: recovery intentionally fails closed if it is
missing. A failed initial deployment has no previous release and remains stopped.

Backups live in `backups/<UTC timestamp>-<random>/` with a receipt and SQLite
integrity checks. An absent adjudications database is recorded as absent; an
absent personal database prevents activation. These are local recovery backups,
not an off-host backup policy. The separate [encrypted backup service](backup.md)
adds daily consistent snapshots, retention and a Mac offsite copy. Do not delete current, the previous release referenced
by `deployment-pending.json`, or backups needed for recovery. Code rollback assumes
schema-compatible releases; destructive migrations need a separate reviewed plan.

Tests use fake GitHub archives, fake service management and temporary SQLite
databases. They do not prove the first real asset download, server wheel install,
systemd sandbox or public proxy configuration. Validate those during bootstrap.

When introducing the bundled-release format, install the reviewed updated
`deploy/deploy_release.py` at `/opt/mai-journal/deploy_release.py` (root-owned 0755)
before publishing the first bundle. It remains one standalone Python file;
there is no new root helper or scheduled job. An older deployer intentionally
rejects `dependencies/`, keeping the existing application running. Normal releases
continue to leave this root-owned bootstrap file unchanged.

## Direct game-score synchronization

The Wahlap worker has separate, pinned dependencies and an explicitly provisioned
runtime. See [Wahlap runtime setup](wahlap-runtime.md). Provision the runtime and
`mai-journal-wahlap.socket` before setting `MAIMAI_WAHLAP_ENABLED=1` and
`MAIMAI_WAHLAP_SOCKET=/run/mai-journal-wahlap.sock` in the application environment.
Each private Unix socket connection starts a separate service using the current
release, which launches the sandboxed worker. The web service retains its existing
systemd hardening; only the broker disables `ProtectKernelTunables`, because its
masked host `/proc` otherwise prevents bubblewrap from mounting a private `/proc`.
The broker has no application EnvironmentFile, does not log the QR, and disables
core dumps. The website can continue running if this optional runtime
is unavailable. Only the worker's state directory is writable inside its Linux
filesystem sandbox. The personal database, catalog and application credentials
are not mounted there.

The root-owned deployer must include the exact `wahlap-runtime.lock.json` allowlist
entry before activating the first release that contains it. Normal CI releases
do not replace that bootstrap script or install Wine/system packages. Existing
personal scores and AP confirmations are migrated additively, without replacing
their evidence. A runtime probe validates local dependencies only; an authenticated
full score fetch and confirmed game logout are still required for end-to-end
verification. Do not simulate those using unrelated accounts.

For a cloud egress rejected by Wahlap, provision the [dedicated proxy namespace](../docs/WAHLAP_PROXY.md).
It routes both Python and Wine through one selected node, without changing the
web service's network. The manual network probe requests game configuration
without a QR or player login; the ordinary status poll remains an imports check.
Proxy credentials and binaries stay outside application releases.

## Venue map and place lookup

Venue locations live in the private `venue_locations` table in `personal.sqlite3`.
The migration only adds tables. Locations use WGS84 coordinates; converting a
GCJ-02 source is an import responsibility, never an inferred API behavior. Location
edits do not rename personal venues or change confirmed/pending AP records.
Initial personal location seeds are imported privately, never bundled in releases.

The map requests only visible OpenStreetMap tiles, with the browser cache and a
visible attribution. Configure Caddy `Referrer-Policy strict-origin-when-cross-origin`
so the tile provider receives the site origin. Do not add offline tile prefetching.
See https://operations.osmfoundation.org/policies/tiles/ for provider requirements.

User-triggered place search uses Photon's public endpoint, with a shared one-request
per second limit and a bounded seven-day SQLite cache. No autocomplete or background
geocoding is performed. Set `MAIMAI_GEOCODER_URL` to another Photon-compatible endpoint
without a code release; set it to an empty string to disable remote search. The public
endpoint is best-effort (https://github.com/komoot/photon#demo-server); manual map
selection and address entry remain available when search is unavailable. Only the
search text is sent to Photon; AP records and saved venue lists stay in the app.
