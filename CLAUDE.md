# Claude Code instructions — Agentic Moodle development workspace

This file is auto-loaded when Claude Code opens anywhere under
`/var/www/ai/`. It tells you (Claude) how to work in this repo.

## What this repo is

Six sibling tools that give an AI coding agent structured Moodle context:

- `agentic_devdocs` — Moodle docs index (SQLite + FTS5)
- `agentic_indexer` — Moodle source code index (symbols, edit-surface, tests)
- `agentic_sitemap` — Moodle site crawl (page types, workflow edges, affordances)
- `agentic_debug` — bounded Xdebug runner *(may be skipped if PHP < 8.4)*
- `agentic_orchestrator` — thin context-assembly layer over the four above
- `moodle_claude` — Claude Code harness with `./bin/*` commands for the active Moodle instance

Full architecture in [`README.md`](README.md). Setup script in
[`setup.py`](setup.py).

## Active instance — read this every session

The active Moodle instance, admin credentials, and tool paths are recorded
in [`_data/.setup_state.json`](_data/.setup_state.json). **Read it at the
start of any non-trivial task** to get:

- `instance` — the Moodle instance root (e.g. `/var/www/docker/aitest`)
- `moodle_dir` — Moodle source for direct edits
- `docker_dir` — moodle-docker checkout
- `site_url` — running Moodle URL for browser validation
- `admin_username` / `admin_password` — for browser login
- `skipped_php_tools` — list of tools (e.g. `agentic_debug`) that aren't installed; do not try to use them

Never hard-code paths or credentials in code or commits — read from this
file or from the relevant config (`moodle_claude/.claude.env`,
`agentic_orchestrator/config.local.toml`).

## Doing work — orchestrator-first for non-trivial tasks

Classify every task first:

- **Trivial local edit** in already-identified files → just edit. Skip discovery.
- **Non-trivial Moodle task** (find files, follow Moodle conventions, touch a subsystem you don't already know) → query the orchestrator first.

### How to query the orchestrator

```bash
cd /var/www/ai/agentic_orchestrator
.venv/bin/agentic-orchestrator query "<short natural-language task description>" \
  --config ./config.local.toml --route-mode auto --json
```

Read back from the result:

- `exact_files_to_inspect` — start here
- `nearest_tests_to_mirror` — write/extend tests in these patterns
- `closest_matching_patterns` — copy these conventions, don't invent
- `recommended_implementation_path` — the canonical file you should likely edit
- `docs_results` / `code_results` / `site_results` — supporting evidence with provenance
- `result_thin: true` — your query was too vague; refine using `refine_query_hints`

### Verify before relying on results

If you haven't run a check this session:

```bash
cd /var/www/ai/agentic_orchestrator
.venv/bin/agentic-orchestrator verify --config ./config.local.toml --json
```

Trust only capabilities that report `usable_for.<cap>: true`:

- `docs_lookup`, `code_context`, `site_navigation`, `pattern_discovery` — the common four
- `debug_investigation` — only if `agentic_debug` is installed (check `skipped_php_tools` in state)

If a needed capability is `false`, say so and stop that path rather than
faking confidence.

## Daily Moodle development happens from the harness

For PHPCS, PHPUnit, Behat, Grunt builds, install/upgrade, Docker
operations, use the wrappers in [`moodle_claude/bin/`](moodle_claude/):

```bash
cd /var/www/ai/moodle_claude
./bin/preflight              # targeted PHPCS pass
./bin/preflight --changed-lines [--base-ref <ref>] [paths...]
./bin/phpcs <paths>          # whole-file PHPCS
./bin/phpunit <test>         # PHPUnit in container
./bin/behat <feature|--tags> # Behat in container
./bin/grunt amd --files=<src>   # narrow JS rebuild
./bin/upgrade                # Moodle CLI upgrade in container
./bin/up / ./bin/down / ./bin/ps / ./bin/logs
./bin/web <cmd>              # arbitrary command in webserver container
```

The harness has its own [`moodle_claude/CLAUDE.md`](moodle_claude/CLAUDE.md)
with the **non-negotiable Moodle development rules** — coding style,
external API access-control order, privacy provider requirement, PHPUnit
test requirements for web services and scheduled tasks, branch discipline,
self peer review, etc. **Read and follow that file** for any real Moodle
code change. Treat it as authoritative; this file is just the entry point.

Note that `moodle_claude/CLAUDE.md` was authored against Matt's path
layout (`~/projects/...`). The actual paths for this machine come from
`moodle_claude/.claude.env` (which setup.py wrote correctly). Don't be
confused by the example paths — use `MOODLE_DIR`, `MOODLE_DOCKER_DIR`,
etc. from the env file.

## Standard workflow for a Jira-driven Moodle task

1. **Read the Jira issue** (Atlassian MCP if connected, otherwise via web).
2. **Map target version → branch** using `moodle_claude/docs/moodle-branching.md`.
3. **Switch to the right development branch** in `MOODLE_DIR` (e.g. `MOODLE_502_STABLE_MDL-12345`). Never commit directly to `main` or `MOODLE_*_STABLE`.
4. **Query orchestrator** for files/patterns/tests.
5. **Edit** in `MOODLE_DIR` directly.
6. **Validate** — `./bin/preflight` → `./bin/phpunit` → `./bin/behat` (if UI/behaviour) → `./bin/upgrade` (if plugin metadata changed) → `purge_caches.php` (if templates/strings/SCSS changed).
7. **Browser-validate** via Chrome MCP for UI changes — login at `<site_url>/login/index.php` with `MOODLE_ADMIN_USERNAME` / `MOODLE_ADMIN_PASSWORD`, hit the relevant page, confirm it renders.
8. **Self peer review** (Y/N/- checklist, MUST FIX vs SHOULD FIX) for non-trivial changes. Apply MUST FIX, re-run only affected validation.
9. **Commit message** — Moodle style: `MDL-12345 component: concise imperative summary`.
10. **Don't push** without the user's explicit say-so.

## What NOT to do

- Do not invent Moodle APIs or file locations — query the orchestrator/indexer.
- Do not skip the privacy provider for new plugins — even no-data plugins need `null_provider`.
- Do not skip `validate_context()` in external `execute()` methods.
- Do not edit `amd/build/*.min.js` — edit `amd/src/` and run `./bin/grunt amd`.
- Do not commit to `main` or `MOODLE_*_STABLE` branches.
- Do not push to remotes without explicit instruction.
- Do not run `./bin/down`, `docker compose down -v`, or anything destructive without confirmation.
- Do not assume `agentic_debug` is available — check `skipped_php_tools` in state first.

## Useful direct tool calls when you don't need orchestrator routing

```bash
# docs lookup
/var/www/ai/agentic_devdocs/.venv/bin/agentic-docs query "<query>" \
  --db-path /var/www/ai/_data/devdocs.db --context-bundle --json-contract

# code lookup
/var/www/ai/agentic_indexer/.venv/bin/moodle-indexer find-definition \
  --db-path /var/www/ai/_data/moodle-index.sqlite \
  --symbol 'mod_assign\external\start_submission::execute' --json-contract

/var/www/ai/agentic_indexer/.venv/bin/moodle-indexer build-context-bundle \
  --db-path /var/www/ai/_data/moodle-index.sqlite \
  --query "<task description>" --json-contract

# site lookup against the most recent crawl
/var/www/ai/agentic_sitemap/.venv/bin/moodle-sitemap runtime-query \
  --run "$(cat /var/www/ai/_data/.setup_state.json | python3 -c 'import sys,json; print(json.load(sys.stdin)["sitemap_run"])')" \
  --lookup-mode page --query "<page url or page id>" --json-contract
```

These are useful when you already know which source you need and want to
skip orchestrator routing overhead.
