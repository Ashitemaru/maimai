# Server deployment

Deployment is push-triggered and the server never contacts GitHub. On a push to main,
the last CI job (`deploy`) runs after the `release` job, which itself only runs after
every check succeeded. It downloads the published archive and connects over SSH as
`mai-deploy`, whose key is limited to the forced command `ci-deploy.sh`:

- `wheels` lists the dependency wheels the server has cached (by SHA-256). CI removes
  those from the archive, so an ordinary code change uploads only about 1 MB;
  cross-border uploads from GitHub to the server are slow.
- `deploy` reads the commit SHA and the reduced archive on stdin. `ci_receive.py`
  stores it as the single pending upload in `incoming/`, then the script starts
  `mai-journal-deploy.service` and prints its journal. The CI job fails when the
  deployment fails; rerun the job to retry.

The deployer restores omitted wheels from `wheels/`, then validates paths, file types,
resource bounds, `RELEASE_SHA` and every wheel hash before installing dependencies.
After activation `wheels/` keeps exactly the wheels of the new release. There is no
polling timer and no deploy configuration file.

The archive has no enclosing directory. It contains `backend/*.py` excluding tests,
the core `scripts/*.py` allowlist (`CORE_SCRIPTS` in `scripts/package_release.py`), `requirements.lock`,
`frontend/dist/**`, `RELEASE_SHA`, a required `dependencies/` wheel bundle, and
optionally `deploy/**` for manual bootstrap updates. It must contain no personal
runtime data, catalogs, pictures, credentials, symlinks or private records.
`dependencies/` contains only wheels and their public `manifest.json`.
Releases contain no deployment JSON. Requirements are exact version pins with no URLs or
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
   mode 0755. Copy `mai-journal.service` and `mai-journal-deploy.service` into `/etc/systemd/system`, root-owned 0644.
   Normal releases do not automatically update these root-owned bootstrap files.
4. Create `/etc/mai-journal/app.env` from the example, root-owned
   0600. Set the allowed HTTPS hostname. Generate
   `MAIMAI_MASTER_KEY_HASH` with `python -m backend.auth hash`, supplying the key
   through stdin. Keep the key in a password manager, never in shell arguments,
   code or releases. Production refuses to start without a valid hash. The app
   unit adds production paths and reads the release SHA from `current/release.env`.
5. Install the reviewed sudoers file at `/etc/sudoers.d/mai-journal`, root-owned
   0440, and validate with `visudo -cf`. It permits `maimai` only start/stop/restart of the
   exact application service, and `mai-deploy` only starting the deployment unit,
   reading its journal and running `ci_receive.py` as `maimai`. The application unit's `NoNewPrivileges` prevents
   it from using sudo; its filesystem sandbox permits writes only to catalog and
   runtime. The deployment unit can write release/current/backup paths.
6. Run `systemctl daemon-reload`, then `systemctl start mai-journal-deploy.service`.
   Inspect its journal and `/api/ready` through localhost. Enable
   `mai-journal.service` only after initial validation. If an older install enabled
   `mai-journal-deploy.timer`, run `systemctl disable --now mai-journal-deploy.timer`
   and remove its unit file.
7. CI deploy access: create the user `mai-deploy` with a normal shell and no password
   (`useradd -m -s /bin/sh mai-deploy; passwd -l mai-deploy`). Install
   `ci-deploy.sh` and `ci_receive.py` into `/opt/mai-journal/`, root-owned 0755. Generate a
   dedicated key pair (`ssh-keygen -t ed25519 -N "" -f mai-deploy`) and put the public
   key into `~mai-deploy/.ssh/authorized_keys` as in `authorized_keys.example`. In the
   GitHub repository secrets set `DEPLOY_HOST`, `DEPLOY_SSH_KEY` (private key),
   `DEPLOY_KNOWN_HOSTS` (output of `ssh-keyscan -t ed25519 <host>`), and optionally
   `DEPLOY_PORT` and `DEPLOY_USER`. Without `DEPLOY_HOST` the deploy job only warns.
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

Keep the template's `handle_errors` block when provisioning Caddy. A stopped or
unreachable app must produce an explicit, non-cacheable 503: JSON for API calls
and HTML for browser navigation. The HTML retries the same URL after five seconds.
This incident's browser downloaded the empty, untyped proxy error as a zero-byte
file; a typed HTML response removes that ambiguous navigation result. Application
responses, including API validation/conflict errors and intentional image downloads,
still pass through.
Run `python scripts/verify_proxy.py` on a host with Caddy to exercise the template
on isolated loopback ports, with a disconnected upstream and after recovery. It
does not stop or reconfigure the production proxy or application.

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

A filesystem flock serializes the complete deployment. Uploading the current SHA is
a no-op. A consumed or failed upload is deleted; a rerun of the CI job uploads again.

The deployer writes a recovery journal, stops the single application service
(60-second systemd budget; a running Diving-Fish sync gives up after at most 40 seconds), makes SQLite online-backup copies of personal and
adjudications databases (plus the session database when present), changes
`current` atomically and starts the new code.
It accepts `/api/ready` only when `ok` is true and `release_sha` matches. A failed
activation stops the new process before restoring the old pointer and restarting
it. Rollback never overwrites personal data. An interrupted activation is rolled
back from the journal on the next deployment before another deployment is tried.
After fixing an environment problem, rerun the CI deploy job.
Keep the previous release directory: recovery intentionally fails closed if it is
missing. A failed initial deployment has no previous release and remains stopped.

Each activation also makes SQLite online-backup copies in `backups/<UTC timestamp>-<random>/`
(the newest 10 are kept) and deletes every release except the active and previous one.
An absent personal database prevents activation. Daily encrypted off-host backups are
described in [backup.md](backup.md). Code rollback assumes
schema-compatible releases; destructive migrations need a separate reviewed plan.

Tests use synthetic archives, fake service management and temporary SQLite
databases. They do not prove the SSH path, server wheel install,
systemd sandbox or public proxy configuration. Validate those during bootstrap.

When introducing the bundled-release format, install the reviewed updated
`deploy/deploy_release.py` at `/opt/mai-journal/deploy_release.py` (root-owned 0755)
before publishing the first bundle. It remains one standalone Python file;
there is no new root helper or scheduled job. An older deployer intentionally
rejects `dependencies/`, keeping the existing application running. Normal releases
continue to leave this root-owned bootstrap file unchanged.

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
