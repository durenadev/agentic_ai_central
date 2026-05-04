# Agentic Moodle development tooling

Six sibling tools that give an AI coding agent (Claude Code, Codex)
structured Moodle context — docs, code, sites, debug — so it can do real
Moodle LMS work instead of guessing conventions from training data.

## What's in here

| Directory | Purpose |
|---|---|
| [`agentic_devdocs/`](agentic_devdocs/) | SQLite docs index over the public Moodle developer docs |
| [`agentic_indexer/`](agentic_indexer/) | SQLite code index — symbols, edit-surface, tests, dependencies |
| [`agentic_sitemap/`](agentic_sitemap/) | Authenticated Moodle site crawl — page types, workflow edges, affordances |
| [`agentic_debug/`](agentic_debug/) | Bounded Xdebug runner for PHPUnit + CLI scripts *(needs PHP 8.4)* |
| [`agentic_orchestrator/`](agentic_orchestrator/) | Thin context-assembly layer over the four above |
| [`moodle_claude/`](moodle_claude/) | Claude Code harness with `./bin/*` commands for Moodle dev |

Each sub-repo has its own README with the deep details.

## Quick start

```bash
git clone <this-repo> /var/www/ai
cd /var/www/ai
python3 setup.py
```

Answer the prompts (instance path, admin creds, optional Jira/TinyMCE
keys). The script installs all dependencies, builds the docs and code
indexes, brings up Moodle, runs the sitemap crawl, and writes the wiring
configs. ~30 minutes total on first run.

When it's done, open the generated VSCode workspace:

```bash
code /var/www/ai/_data/<instance>.code-workspace
```

In the integrated terminal, run `claude` — `CLAUDE.md` auto-loads, the
permission allowlist in `.claude/settings.json` reduces prompts, and
you can start giving Moodle tasks.

## What setup.py does

A single idempotent bootstrap, ~30 min on first run. Re-run any time —
every step skips work that's already done.

- **Clones the 6 sibling tool repos** from `https://github.com/mattporritt/<name>.git` if they're not already present.
- **Installs dependencies** — a Python venv per tool with `pip install -e .[dev]`, `composer install` for `agentic_debug`, Playwright Chromium for the sitemap crawler. If a tool's deps fail (e.g. `agentic_debug` needs PHP 8.4) it offers to skip that tool and continue.
- **Builds the static SQLite resources** — clones the Moodle devdocs source and ingests it into `_data/devdocs.db`; runs the indexer over the Moodle source into `_data/moodle-index.sqlite`.
- **Manages the Moodle instance** — either clones Moodle + moodle-docker into a new `/var/www/docker/<instance>/`, or points at an existing checkout.
- **Brings up Moodle** *(if you say yes)* — copies `config.docker-template.php` into the Moodle source, runs `./bin/up` and `./bin/install` from the harness with the right `MOODLE_DOCKER_WWWROOT` env exported.
- **Runs the sitemap crawl** — logs into the running Moodle as admin, crawls 200 pages depth 4 into `_data/sitemap-runs/<instance>/`.
- **Writes per-machine configs** — `agentic_orchestrator/config.local.toml`, `moodle_claude/.claude.env` (with `export` on every line), `moodle_claude/.claude.identity`, and the VSCode workspace at `_data/<instance>.code-workspace`.
- **Verifies** — runs `agentic-orchestrator verify --json` and prints which capabilities (`docs_lookup`, `code_context`, `site_navigation`, `debug_investigation`) are usable.

Saves answers to `_data/.setup_state.json` so re-runs don't re-prompt.
What it deliberately does **not** do: `git init` the parent repo, push
code, modify your existing Moodle source if you target an existing
instance, or run any destructive operation.

## Layout

```
/var/www/ai/                     ← this repo (the 6 sub-repos as siblings)
├── agentic_*/                   ← the 5 agentic tools
├── moodle_claude/               ← the harness
├── _data/                       ← built resources (gitignored)
│   ├── devdocs.db                  docs SQLite
│   ├── moodle-index.sqlite         code SQLite
│   ├── sitemap-runs/<instance>/    crawl output
│   ├── <instance>.code-workspace   VSCode workspace
│   └── .setup_state.json           saved answers + progress
├── CLAUDE.md                    ← auto-loaded Claude Code instructions
├── .claude/settings.json        ← committed permission allowlist
├── setup.py                     ← bootstrap script
└── README.md

/var/www/docker/<instance>/      ← one Moodle instance per directory
├── moodle/                         Moodle source checkout
└── moodle-docker/                  moodle-docker checkout
```

## setup.py reference

```bash
python3 setup.py            # run the next pending step (idempotent)
python3 setup.py status     # show what's done so far
python3 setup.py creds      # add/update Jira + TinyMCE keys
python3 setup.py rebuild devdocs|indexer|sitemap|all
python3 setup.py reset      # wipe state file (keeps built DBs)
```

`python3 setup.py --help` for the full list.

## Day-to-day workflow

1. Open the workspace file in VSCode.
2. Start `claude` in the integrated terminal.
3. Give Claude a Moodle task — a Jira key (`"work on MDL-12345"`) or a
   short description.

Claude will: query the orchestrator for the right files and patterns,
edit in the Moodle source, run `./bin/preflight`, `./bin/phpunit`,
`./bin/behat` via the harness, browser-validate via Chrome MCP if
configured, and propose a commit. You review the diff and approve.

The committed [`CLAUDE.md`](CLAUDE.md) is the source of truth for how
Claude works in this repo. The harness's
[`moodle_claude/CLAUDE.md`](moodle_claude/CLAUDE.md) is the source of
truth for the non-negotiable Moodle development rules (PHPCS standard,
privacy provider, capability order, PHPUnit requirements, branching).

## Troubleshooting

- **`composer install` fails.** Usually `agentic_debug` requires PHP 8.4.
  Answer **yes** to the "skip" prompt — the orchestrator treats it as
  non-blocking, everything else still works.
- **`./bin/up` fails with `MOODLE_DOCKER_WWWROOT not set`.** Re-run
  `python3 setup.py` — phase 2 regenerates `.claude.env` with the right
  exports.
- **`Sitemap smoke failed`.** Moodle isn't reachable at the configured
  site URL. Check `./bin/ps` from `moodle_claude/`.
- **Verify reports `usable_for.<x>: False`.** Run
  `python3 setup.py rebuild <x>` to regenerate that resource.
- **Claude prompts for permission on a safe command.** Add the pattern
  to `.claude/settings.json` `allow` list and commit.

For sub-repo-specific issues, see each sub-repo's own README.

## License

Each sub-repo is licensed under the terms in its own `LICENSE`.
Most are Moodle Community License v1.3.
