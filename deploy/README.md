# Server deployment

The server polls public GitHub main every two minutes. It activates only a completed,
successful `push` run of `ci.yml` in the same repository, plus the exact release tag
`app-<40 lowercase hex SHA>`. The tag must resolve to that commit. Required release
assets are `mai-journal-<SHA>.tar.gz` and `SHA256SUMS` (sha256sum format). Downloads
and redirects use HTTPS GitHub hosts only; the archive checksum, paths, file types,
resource bounds and `RELEASE_SHA` are validated before installing dependencies.

The archive has no enclosing directory. It contains `backend/*.py` excluding tests,
the core `scripts/*.py` allowlist in `deploy_release.py`, `requirements.lock`,
`frontend/dist/**`, `RELEASE_SHA`, and optionally `deploy/**` for manual bootstrap
updates. It must contain no runtime, catalogs, pictures, archives, credentials,
symlinks or private records. Requirements are exact version pins with no URLs or
pip options. CI must lint `deploy` and run `tests.test_deploy` alongside other tests.

## One-time root bootstrap

1. Install the OS Python 3.12+ interpreter, its venv package, CA certificates and sudo.
   Use an app interpreter version covered by CI. Create the locked service account
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
   0600. Set the actual public repository and allowed HTTPS hostname. The app unit
   adds production paths and reads the release SHA from `current/release.env`.
5. Install the reviewed sudoers file at `/etc/sudoers.d/mai-journal`, root-owned
   0440, and validate with `visudo -cf`. It permits only start/stop/restart of the
   exact application service. The application unit's `NoNewPrivileges` prevents
   it from using sudo; its filesystem sandbox permits writes only to catalog and
   runtime. The deployment unit can write release/current/backup paths.
6. Run `systemctl daemon-reload`, then `systemctl start mai-journal-deploy.service`.
   Inspect its journal and `/api/ready` through localhost. Enable
   `mai-journal.service` and `mai-journal-deploy.timer` only after initial validation.
   Configure the HTTPS/authentication proxy separately; never expose 8766 publicly.

## Activation and recovery

Dependencies are installed into the final candidate release directory before the
old app is stopped; venvs are never moved after installation. pip is pinned and run
in isolated mode against PyPI with binary wheels only. Main is rechecked after
preparation. A filesystem flock serializes the complete deployment. The current
SHA is a no-op, so ordinary timer ticks make just one unauthenticated GitHub API
request. GitHub errors/rate limits keep the running version and are retried on a
later timer run; a release in progress is not an error.

The deployer writes a recovery journal, stops the single application service
(90-second systemd budget), makes SQLite online-backup copies of personal and
adjudications databases, changes `current` atomically and starts the new code.
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
not an off-host backup policy. Bootstrap operators must arrange retention and
encrypted off-host copies. Do not delete current, the previous release referenced
by `deployment-pending.json`, or backups needed for recovery. Code rollback assumes
schema-compatible releases; destructive migrations need a separate reviewed plan.

Tests use fake GitHub archives, fake service management and temporary SQLite
databases. They do not prove the first real asset download, server wheel install,
systemd sandbox or public proxy configuration. Validate those during bootstrap.
