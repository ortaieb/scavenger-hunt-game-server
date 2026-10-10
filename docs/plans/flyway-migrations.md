# Plan: versioned database migrations with Flyway

| | |
|---|---|
| Status | In progress: issues #98–#105. M1 is #99 |
| Written | 2026-10-10, against `main` at `6484f23` |
| Repos affected | **game-server** only. The web app has no database and doesn't refer to the game server's schema, so it needs no change |
| Purpose | Agreed design, plus a breakdown into issues ([below](#issue-breakdown)) |

## Decisions (2026-10-10), which supersede parts of this plan

Answers to the [open questions](#open-questions), and what they change. The issues
(#98–#105) carry the details.

- **Production's data isn't kept on the switch.** We're still in development, so production is
  **reset once onto V1** (a one-off `clean` + `migrate`, #103) instead of being baselined: M4's
  backup, comparison and `baseline` are dropped. From then on, data is kept and every change is
  an incremental migration.
- **Production's schema matches `schema.sql` on `main`** (open question 2): moot, given the reset.
- **The health check is config as code** (open question 3): every deploy applies `railway.toml`'s
  `healthcheckPath` and `healthcheckTimeout` through the Railway API and checks them (#98).
- **PostgreSQL is private** (open question 4): no public endpoint, reachable only from services
  on Railway's private network (#105). That rules out option A (Flyway on the GitHub runner,
  over the public proxy). Option B's Java in the distroless app image stays rejected. Instead,
  migrations run from **a separate migrations image** (pinned Flyway plus `db/`, #101) deployed
  as **its own Railway service on the private network**, which the deploy workflow runs, and
  waits for, before the app (#102). The sections below that describe option A's deploy step
  are kept for history.

## Why

Today every schema change means dropping and recreating every table
([`schema.sql`](../../src/game_server/schema.sql) through `make db-reset`), and that deletes all
the data, published hunts included. We now have data worth keeping, so schema and content
changes should be applied **incrementally**: as small, ordered, versioned migrations that run
once per database and are recorded there. [Flyway](https://documentation.red-gate.com/flyway)
does this. It applies `V<n>__<name>.sql` files in order and records each one in a
`flyway_schema_history` table. It refuses to run if a migration that was already applied has
since been edited.

## Where we are today

Findings from the code at `6484f23`:

- **The schema** is one file, `src/game_server/schema.sql`. It runs in one transaction:
  `DROP VIEW/TABLE IF EXISTS … CASCADE`, then `CREATE` for 11 tables, their indexes and the
  `ruled_submissions` view. It sits inside the Python package, so it ships in the wheel and the
  image.
- **`game_server.db_reset`** (`python -m game_server.db_reset --yes`, `make db-reset`) runs it.
  Four things depend on it:
  - **Developers**, through `make db-reset`.
  - **The test suite.** `tests/conftest.py`'s session `db` fixture calls `reset_schema()` once,
    then `TRUNCATE`s an explicit list of tables before every test. `tests/test_db_reset.py`
    tests the reset itself.
  - **PR CI's Docker smoke test** (`pr-build.yml`, job `docker`). It runs the image's own
    `db_reset` against a TLS PostgreSQL container before it starts the server.
  - **Production.** The README says to run `make db-reset` once from a laptop against the
    database's **public** URL (`DATABASE_PUBLIC_URL`).
- **The deploy** ([`railway-deploy.yml`](../../.github/workflows/railway-deploy.yml)) is one job.
  It builds the image and pushes it to GHCR (tags `latest` and the short SHA). It then calls
  the Railway GraphQL API to point the service at that tag (`serviceInstanceUpdate`) and deploy
  it (`serviceInstanceDeployV2`), and polls until the deploy succeeds or fails. The concurrency
  group `railway-deploy` (with `cancel-in-progress: false`) means only one deploy runs at a
  time. A manual run with a `tag` is a rollback: it skips the build and redeploys an existing
  image.
- **The runtime image** is `gcr.io/distroless/cc-debian12:nonroot`. It has no shell, no package
  manager and no Java runtime. `.dockerignore` only lets `pyproject.toml`, `uv.lock`,
  `.python-version` and `src/` into the build.
- **`GET /health`** is Railway's readiness check. It runs `SELECT 1 FROM submissions`, so a deploy
  without the tables never takes traffic. It doesn't check *which* schema version the database
  is at.
- **The server never changes the schema itself**, and that stays true under this plan.

## Decision: a separate step, or an init container?

**Railway has no init containers.** A Railway service runs one container. The closest thing is
the service's **pre-deploy command**. Railway runs it in a separate container between the build
and the deploy, inside the private network and with the service's variables. If it fails, the
deploy doesn't go ahead. No volume is mounted, and the command's dependencies must already be in
the **application image** ([Railway docs](https://docs.railway.com/guides/pre-deploy-command)).

| | **A. Separate step in `railway-deploy.yml`** (recommended) | **B. Railway pre-deploy command ("init container")** |
|---|---|---|
| Where Flyway runs | On the GitHub runner, from the official `flyway/flyway` image, using the migrations in the commit being deployed | Inside the game-server image, on Railway |
| What changes in the runtime image | Nothing: `db/` isn't even copied in | A Java runtime and the Flyway CLI (plus a shell for its launcher script, or a hand-written `java` call) added to the distroless image. That's a large addition, and it undoes the no-shell hardening |
| How it reaches the database | Through Railway PostgreSQL's public TCP proxy, the same public URL the README already uses for `db-reset`. The credentials are GitHub secrets | Through the private network: no public endpoint needed |
| Fit with the current script | One new step between "Build and push image" and "Deploy image to Railway". The existing concurrency group already runs migrations one at a time | `preDeployCommand` would have to be set through the API or the dashboard. `railway.toml` is read from a repo, but this service is deployed from a GHCR image, and a Railway community thread suggests config-as-code isn't re-read for image deploys (to check: see [open questions](#open-questions)) |
| Rollbacks (manual run with `tag`) | No build means no migration. The database stays ahead, which is safe if migrations are backward compatible (see [conventions](#conventions-for-writing-migrations)) | Runs the older image's migrations. Flyway ignores migrations the database has but the files don't, so it does nothing. Also safe |
| A deploy started outside the workflow (Railway dashboard) | Doesn't migrate. The [schema-version health gate](#health-gate-schema-version) keeps such a deploy from taking traffic | Migrates on every deploy |
| On failure | The workflow fails before Railway is touched, the old deploy keeps serving, and the logs are in GitHub Actions | The deploy fails on Railway, the old deploy keeps serving, and the logs are in Railway |

**Recommendation: A, a separate step in the existing deploy job.** It leaves the slim distroless
runtime alone, slots into the script as it stands, gives each migration a visible place in the
GitHub run, and keeps the rollback path doing no DDL. Its one real cost is that the database
stays reachable from the internet, and it already is. The health gate closes the gap B would
otherwise have over A.

**Revisit** if the public endpoint must be closed. Then either put Flyway in the app image and
use the pre-deploy command, or run a separate migrations image (`FROM flyway/flyway` plus the
migrations) on Railway. Both are more work than we need today.

## Target design

### Layout

```
db/
  flyway.toml                     # shared Flyway settings (no credentials)
  migrations/
    V1__baseline.sql              # today's schema.sql, minus the DROPs and BEGIN/COMMIT
    V2__<what_it_does>.sql        # …the first real incremental change, and so on
```

- `db/` lives at the repo root, outside the Python package. The server doesn't read migrations
  at runtime, and `.dockerignore` keeps them out of the image.
- `src/game_server/schema.sql` and `src/game_server/db_reset.py` (with its tests) are
  **removed**. There is one source of truth: the migrations.

### Flyway settings (`db/flyway.toml`)

A sketch. Check the exact keys against the docs for the Flyway version we pin.

```toml
[flyway]
locations = ["filesystem:/flyway/project/migrations"]   # db/ is mounted at /flyway/project
validateMigrationNaming = true   # a misnamed file is an error, not silently skipped
cleanDisabled = true             # `clean` (drop everything) is refused unless overridden locally
baselineOnMigrate = false        # baselining production is a deliberate one-off (M4)
placeholderReplacement = false   # SQL containing ${…} is left alone
outOfOrder = false               # versions must arrive in order
```

The URL, user and password are never in the file. They come from `FLYWAY_URL`, `FLYWAY_USER`
and `FLYWAY_PASSWORD`. The URL is JDBC: `jdbc:postgresql://host:port/db?sslmode=require`.

**Flyway version:** pin an **exact** `flyway/flyway` tag (13.x at the time of writing), never
`latest`. Define it once, in the Makefile, so local runs, CI and the deploy all use the same
version.

### One way to run Flyway: Makefile targets

Every caller (developer, PR CI, smoke test, deploy) runs Flyway through the same Docker
invocation. Only the network and the URL change. A sketch:

```make
FLYWAY_IMAGE   ?= flyway/flyway:<exact 13.x tag>
FLYWAY_NETWORK ?= container:$(DB_CONTAINER)   # localhost = the `make db-up` container (works on macOS and Linux)
FLYWAY_URL     ?= jdbc:postgresql://localhost:5432/game_server
FLYWAY_USER    ?= postgres
FLYWAY_PASSWORD ?= postgres
export FLYWAY_URL FLYWAY_USER FLYWAY_PASSWORD
FLYWAY = docker run --rm --network $(FLYWAY_NETWORK) -e FLYWAY_URL -e FLYWAY_USER -e FLYWAY_PASSWORD \
         -v "$(CURDIR)/db:/flyway/project:ro" $(FLYWAY_IMAGE) -configFiles=/flyway/project/flyway.toml

db-migrate:       ## Apply pending migrations (local database by default)
db-migrate-test:  ## The same, on game_server_test (what the tests use)
db-info:          ## Show applied and pending migrations
db-reset:         ## DESTRUCTIVE, local only: clean + migrate, with the URL forced to the local container
db-new-migration: ## NAME=add_x → db/migrations/V<next>__add_x.sql from a template (optional)
```

Credentials are passed as `-e NAME`, without the value, so they never appear on a command line
or in a log. `db-reset` hard-codes the local URL and passes `-cleanDisabled=false` only for
itself. Whatever `FLYWAY_URL` the shell holds, it can't clean any other database.

### Baseline (V1) and the existing production database

- `V1__baseline.sql` is today's `schema.sql` **with only these removed**: the destructive
  header notes, `BEGIN;`/`COMMIT;` (Flyway already runs each PostgreSQL migration in its own
  transaction), and the `DROP VIEW`/`DROP TABLE` lines. Every `CREATE` stays exactly as it is,
  so the constraint names Postgres generates (`hunt_drafts_status_check` and so on) match
  production.
- **Production already has these tables**, created by `schema.sql`, so V1 must not run there.
  Production gets a one-off `flyway baseline -baselineVersion=1` instead (M4). That records V1
  as "already applied" without running it, and from then on every `V2+` applies normally.
  Before baselining, a schema-only dump of production is compared with a fresh database
  migrated to V1. They must match.
- Local and CI databases have no tables to keep, so they just `migrate` from empty.
- **The alternative**, if production's data turns out not to be worth keeping: one last reset,
  then `flyway migrate` builds it from V1. This plan assumes we keep the data.

### Local development and tests

- `make db-up` is unchanged (it starts PostgreSQL in Docker and creates `game_server` and
  `game_server_test`). It's followed by `make db-migrate db-migrate-test`. `make test` (and
  `make check`) runs `db-migrate-test` first.
- `tests/conftest.py`: the `db` fixture **stops creating the schema**. Instead it checks the test
  database has been migrated to the newest version. If not, it stops with a clear message
  ("run `make db-migrate-test`"), the way it already does when PostgreSQL is down. The
  per-test `TRUNCATE` list stays explicit, so `flyway_schema_history` is never emptied.
- `tests/test_db_reset.py` is replaced by `tests/test_migrations.py`:
  - the migrated database has exactly the expected tables (today's 11, plus `flyway_schema_history`);
  - migration file names are valid, with unique, contiguous integer versions;
  - `SCHEMA_VERSION` equals the newest migration (see the [health gate](#health-gate-schema-version)).

### PR CI (`pr-build.yml`)

- **validate:** before pytest, run `make db-migrate` against the service container
  (`FLYWAY_NETWORK=host`, URL `…localhost:5432/game_server_test`). Then run it a **second time**,
  which must apply nothing, as a basic idempotency check.
- **docker (smoke test):** replace the image's `db_reset` with Flyway on the `smoke` network,
  over TLS (`…db:5432/postgres?sslmode=require`). That also proves the JDBC driver accepts a
  self-signed certificate the way Railway's PostgreSQL presents one. Then start the server
  and wait for `/health` as today.
- **migration guard-rails** (M2), a cheap job that compares the PR with `origin/main`:
  - migration files already on `main` are **never modified, renamed or deleted** (`git diff
    --name-status origin/main...HEAD -- db/migrations` may only show `A`);
  - every new version is **greater than the highest version on `main`**, so two PRs that both
    add `V5` can't both merge. The second must renumber after rebasing.

### Deploy (`railway-deploy.yml`)

A new step in the existing job, after "Build and push image" and before "Deploy image to
Railway":

```yaml
      - name: Migrate the database (Flyway)
        # Same conditions as the deploy; a rollback (manual run with a tag) skips it.
        if: steps.meta.outputs.build == 'true' && vars.RAILWAY_SERVICE_ID != ''
        env:
          FLYWAY_URL: ${{ secrets.FLYWAY_URL }}         # jdbc:postgresql://<x>.proxy.rlwy.net:<port>/railway?sslmode=require
          FLYWAY_USER: ${{ secrets.FLYWAY_USER }}
          FLYWAY_PASSWORD: ${{ secrets.FLYWAY_PASSWORD }}
        run: |
          set -euo pipefail
          test -n "$FLYWAY_URL" || { echo "FLYWAY_URL is not set in the production environment" >&2; exit 1; }
          make db-migrate FLYWAY_NETWORK=host
          { echo '### Database migrations'; echo '```'; make -s db-info FLYWAY_NETWORK=host; echo '```'; } >> "$GITHUB_STEP_SUMMARY"
```

- New **secrets** in the `production` environment: `FLYWAY_URL`, `FLYWAY_USER` and
  `FLYWAY_PASSWORD`, taken from the PostgreSQL service's public connection details on Railway.
  The workflow header comment lists them alongside the existing ones.
- If the migration fails, the job stops and Railway isn't touched. The image is already pushed
  (and `latest` already moved), as happens today when a deploy fails.
- While Railway brings up the new deploy, **the old one keeps serving** on the migrated
  schema. That is why migrations must be backward compatible (see
  [conventions](#conventions-for-writing-migrations)).
- Flyway holds a PostgreSQL advisory lock while it migrates, and the workflow's concurrency
  group already runs one deploy at a time.

### Health gate: schema version

This closes the gap where an image runs against a database that wasn't migrated for it (a
manual redeploy of a newer tag, a dashboard deploy, a migration step that was skipped):

- `src/game_server/schema_version.py` holds `SCHEMA_VERSION = <newest migration>`. A test fails
  if it differs from the highest `V<n>` in `db/migrations`, so every migration PR bumps it.
- `SubmissionStore.ping()` (used by `GET /health`) reads the highest successfully applied
  version from `flyway_schema_history`. If that's **below** `SCHEMA_VERSION`, it raises, so
  `/health` answers 503 and Railway keeps traffic on the old deploy. It logs both numbers,
  which contain no secrets. A database that's *ahead* of the code is fine: that's a rollback.
- The response body doesn't change (`{"status": "unavailable"}`). It still reveals nothing about
  internals.

### Conventions for writing migrations

These go into `CLAUDE.md` (for agents picking up issues) and the README:

1. **Never edit a migration that's on `main`.** Fix it with a new one. (Flyway checksums the
   files, and CI enforces this.)
2. **One change per file:** `V<next integer>__<what_it_does>.sql`, lower-case snake case. Bump
   `SCHEMA_VERSION` in the same PR.
3. **Backward compatible with the running version (expand, then contract).** The old deploy
   serves on the new schema for a while, and a rollback runs old code on it indefinitely. So:
   - add columns as nullable, or with a default;
   - a rename or type change is add new → backfill → switch the code → drop old, across
     separate releases;
   - drop a column or table only once no deployed code reads it.
4. **Mind the live game.** DDL takes locks that queue every query behind it. Start migrations
   that alter busy tables (`submissions`, `arrivals`, `participants`, `referee_traces`) with
   `SET lock_timeout = '5s';` so they fail fast rather than freeze the game. Don't ship schema
   changes during a live hunt.
5. **The `ruled_submissions` view uses `s.*`,** which Postgres expands once, when the view is
   created. A migration that changes `submissions` or `rulings` must drop and recreate the view
   in the same file, or new columns won't appear in it (and Postgres refuses to drop or alter
   a column the view uses).
6. **Name new constraints and indexes explicitly.** V1's constraints keep Postgres's generated
   names; use those names when altering them.
7. **Content changes are migrations too:** backfills, fixes to existing rows, and reference data
   go in `V<n>` files with the same rules. Data the app owns at runtime (published hunts,
   drafts, rulings) is only touched to reshape it for a schema change.
8. **Test against data, not just an empty database.** CI migrates an empty database, which won't
   catch, say, `ADD COLUMN … NOT NULL` without a default on a populated table. When a
   migration reshapes existing rows, run it locally against a restored copy of production
   (`pg_dump` → `pg_restore` → `make db-migrate`) before merging.
9. **The server never migrates itself.** Migrations run only through Flyway: the Makefile
   locally, the workflow in CI and production.

## Rollout order

1. **M1 + M2** land together or one after the other: migrations directory, Makefile, tests and
   CI on Flyway, `db_reset` and `schema.sql` gone, guard-rails and rules in place. Production
   is untouched: the deploy workflow doesn't migrate yet. **Hold any new schema change until
   M5 is in**, because until then nothing would apply it to production.
2. **M3**, the health gate with `SCHEMA_VERSION = 1`. ⚠️ Don't deploy M3 before production is
   baselined (M4), or `/health` answers 503 and the deploy never goes live (safe, but blocked).
   The simplest order is M4 first, then merge M3.
3. **M4**, a one-off operation on production: back up, compare, baseline. After it, `flyway info`
   on production shows V1 as *Baseline*.
4. **M5**, the migrate step in the deploy. Its first run applies nothing ("Schema is up to date").
5. **The first real `V2`** (whatever schema change comes next) is the end-to-end test. Watch that
   run's step summary.

## Issue breakdown

All issues are in **game-server**. The web app needs none. Sizes are rough: S is under half a
day, M about a day.

### M1: Move `schema.sql` to Flyway migrations, and run local dev, tests and PR CI on them (M)

- Add `db/flyway.toml` and `db/migrations/V1__baseline.sql`, derived from `schema.sql` as
  described in [Baseline](#baseline-v1-and-the-existing-production-database).
- Add the Makefile targets: `db-migrate`, `db-migrate-test`, `db-info`, a local-only `db-reset`,
  and optionally `db-new-migration`. `make test`/`make check` run `db-migrate-test` first. Pin
  an exact Flyway image tag once, in the Makefile.
- `tests/conftest.py`: no schema creation. Check the database is migrated, and stop with a clear
  message if not.
- Replace `tests/test_db_reset.py` with `tests/test_migrations.py` (expected tables, file naming
  and version sequence).
- `pr-build.yml`: `validate` migrates the service database (twice, the second time a no-op)
  before pytest. `docker` migrates the smoke database over TLS with Flyway instead of
  `db_reset`.
- Remove `src/game_server/schema.sql`, `src/game_server/db_reset.py` and their uses.
- Update every reference: README ("Creating the tables" → "Database migrations", the
  Development table, the CI description, the Docker and Railway sections), `docs/project-layout.md`,
  `docs/api.md` (links at the tables and the `ruled_submissions` view),
  `docs/sessions-file.md` (the reset warning), `.env.example`, and the docstrings in
  `submissions.py`, `referee_traces.py`, `review_queue.py` and `rulings.py` that say "defined
  in `schema.sql`".

**Done when:** a fresh clone runs `make db-up db-migrate db-migrate-test check` green; PR CI is
green on both jobs; `grep -r "schema.sql\|db_reset"` finds nothing outside this plan and V1's
header comment; the image builds without `schema.sql` in it.

### M2: Migration guard-rails in CI, and the rules in CLAUDE.md (S). Depends on M1

- A PR job (or a step in `validate`) that fetches `origin/main` and fails if a migration file on
  `main` was modified, renamed or deleted, or if a new version isn't greater than the highest
  on `main`.
- An escape hatch for one case: a migration that is on `main` but **failed in production**. On
  PostgreSQL a failed migration rolls back whole and isn't recorded, so the right fix is to
  correct that same file. A PR label (for example `fix-unapplied-migration`) lets the check
  pass for it.
- A "Database migrations" section in `CLAUDE.md`, and the matching README text, with the
  [conventions](#conventions-for-writing-migrations).

**Done when:** test PRs that edit `V1`, or that add a version ≤ `main`'s highest, fail with a
message that says what to do.

### M3: Gate `/health` on the schema version (S). Depends on M1. Merge after M4

- `src/game_server/schema_version.py` with `SCHEMA_VERSION`. A test checks it matches the
  newest migration file.
- `SubmissionStore.ping()` checks `flyway_schema_history` (the highest successful version ≥
  `SCHEMA_VERSION`). On a mismatch it logs `database schema at V<n>, this build needs V<m>`.
- Tests: a database behind the code makes `/health` 503; equal or ahead is 200; a missing
  history table is 503.

**Done when:** the tests above pass and the README's health-check paragraph describes the gate.

### M4: Baseline the production database (S, ops). Depends on M1

A runbook, to be done from a laptop with Docker against the public URL:

1. Make sure no hunt is live.
2. Back up: `pg_dump --format=custom "$DATABASE_PUBLIC_URL" > game-server-<date>.dump`. Keep it
   off the repo.
3. Compare: `pg_dump --schema-only --no-owner --no-privileges` of production, against the same
   dump of a local database after `make db-reset`. Any difference has to be understood and
   settled (fix V1 or production) before going on.
4. Baseline: with `FLYWAY_URL`/`USER`/`PASSWORD` set for production, run `make db-info` (no
   history table yet). Then run `baseline -baselineVersion=1 -baselineDescription="schema.sql
   at <sha>"` through the same Docker invocation: a one-off `make db-baseline` target, or the
   `docker run` line by hand. Run `make db-info` again: V1 now shows as *Baseline*.
5. Add the README's runbook section for setting up a **new** environment (`make db-migrate`
   against an empty database, no baseline).

**Done when:** production's `flyway_schema_history` has the baseline row, and `make db-migrate`
against production reports nothing to do.

### M5: Run Flyway in the deploy, before Railway deploys the image (S). Depends on M1 and M4 (and M3 to close the gap)

- Add the `FLYWAY_URL`, `FLYWAY_USER` and `FLYWAY_PASSWORD` secrets to the `production`
  environment, and list them in the workflow's header comment.
- Add the "Migrate the database (Flyway)" step [above](#deploy-railway-deployyml). It's skipped
  on rollbacks (manual runs with a `tag`), and writes `flyway info` to the step summary.
- README, "Deploying on Railway": replace "create the tables once with `make db-reset`" with how
  migrations reach production, how rollbacks behave, and what to do when a migration fails.
  On PostgreSQL it rolls back whole and the old deploy keeps serving. Correct the failed file
  in a PR labelled for M2's escape hatch, and merge it to retry. `flyway repair` is only for a
  history row left in a failed state.

**Done when:** a merge to `main` with no new migration deploys as before, with a no-op migrate
step in the log. A deliberately failing migration (tried on a throwaway branch against a
scratch database) stops the job before the Railway steps.

### Optional follow-ups (not needed for the switch)

- **Move `ruled_submissions` to a repeatable migration** (`R__ruled_submissions.sql`, drop and
  recreate). Flyway re-applies it whenever it changes, after all versioned migrations, which
  takes the burden of convention 5 off each migration.
- **Upgrade test on a populated database in CI.** Migrate to `main`'s newest version, load
  fixture rows, then migrate to the PR's head, to catch convention 8 automatically.
- **Separate database roles.** A migrator role that owns the schema (the one Flyway uses), and
  an app role with data access only, for the server.
- **Dev-only seed data** in a location only the local `db-migrate` includes (for example
  `db/seed/dev/R__seed.sql`), never in production's locations.
- **Close the public database endpoint** by moving migrations onto Railway (see
  [Revisit](#decision-a-separate-step-or-an-init-container)).

## Open questions

1. **Keep production's data?** The plan assumes yes (baseline). If not, M4 becomes "reset once,
   then `migrate`".
2. **Does production's schema match `schema.sql` on `main`?** M4 step 3 answers this.
3. **Is the Railway health check actually applied?** The service is deployed from a GHCR image,
   and `railway.toml` may not be read for image deploys. Check in the dashboard that the service
   has `/health` as its health check path. M3's gate only protects us if Railway calls it.
4. **Keep the PostgreSQL public TCP proxy on?** Option A needs it. If it's turned off, revisit
   the decision.
