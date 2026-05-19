#!/usr/bin/env python3
"""
setup.py — bootstrap the agentic Moodle tooling around one primary instance.

Layout assumed:
  /var/www/ai/                  ← this directory (the 6 tool repos live here)
    agentic_devdocs/  agentic_indexer/  agentic_sitemap/
    agentic_orchestrator/  agentic_debug/  moodle_claude/
    _data/                      ← built resources (gitignored)
    setup.py                    ← this script

  /var/www/docker/<instance>/   ← one Moodle instance per directory
    moodle/                     ← Moodle source checkout
    moodle-docker/              ← moodle-docker checkout (per-instance, not shared)

The script is idempotent. Re-run it any time. State lives in
_data/.setup_state.json so it picks up where it left off.

Usage:
  python3 setup.py            run the next pending phase
  python3 setup.py status     show current state
  python3 setup.py reset      wipe state (asks for confirmation)
  python3 setup.py rebuild devdocs|indexer|sitemap|all
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parent
DATA_DIR = AI_ROOT / "_data"
STATE_FILE = DATA_DIR / ".setup_state.json"

PY_TOOLS = ["agentic_devdocs", "agentic_indexer", "agentic_sitemap", "agentic_orchestrator"]
PHP_TOOLS = ["agentic_debug"]
HARNESS = "moodle_claude"
ALL_TOOL_REPOS = PY_TOOLS + PHP_TOOLS + [HARNESS]

# Used only when a tool repo is missing locally and we need to bootstrap it.
# Override per-repo by editing the dict below if you keep your own forks.
TOOL_REPO_BASE_URL = "https://git.in.moodle.com/matt.porritt"
TOOL_REPO_OVERRIDES: dict[str, str] = {
    # "agentic_orchestrator": "https://github.com/yourname/agentic_orchestrator.git",
}

DEVDOCS_REPO_URL = "https://github.com/moodle/devdocs.git"
DEFAULT_MOODLE_REPO_URL = "https://github.com/moodle/moodle.git"
DEFAULT_MOODLE_DOCKER_REPO_URL = "https://github.com/moodlehq/moodle-docker.git"

DEFAULT_INSTANCE_ROOT = "/var/www/docker"
DEFAULT_MOODLE_BRANCH = "main"
DEFAULT_DOCKER_BRANCH = "main"
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "test"
DEFAULT_ADMIN_EMAIL = "admin@example.com"
DEFAULT_SITE_URL = "http://localhost:8000"


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def banner(text: str) -> None:
    print()
    print("─" * 72)
    print(f"  {text}")
    print("─" * 72)


def info(text: str) -> None:
    print(f"  {text}")


def ok(text: str) -> None:
    print(f"  ✓ {text}")


def warn(text: str) -> None:
    print(f"  ! {text}")


def fail(text: str) -> None:
    print(f"  ✗ {text}", file=sys.stderr)


def run(cmd, cwd=None, env=None, check=True, capture=False, quiet=False):
    pretty = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else cmd
    if not quiet:
        print(f"    $ {pretty}", flush=True)
    if capture:
        return subprocess.run(cmd, cwd=cwd, env=env, check=check,
                              capture_output=True, text=True)
    return subprocess.run(cmd, cwd=cwd, env=env, check=check)


def prompt(question: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            ans = getpass.getpass(f"  {question}{suffix}: ")
        else:
            ans = input(f"  {question}{suffix}: ").strip()
        if ans:
            return ans
        if default is not None:
            return default
        print("    please answer.")


def prompt_optional(question: str, default: str = "", secret: bool = False) -> str:
    """Like prompt() but allows empty input as 'skip'."""
    if secret:
        ans = getpass.getpass(f"  {question} (Enter to skip): ")
    else:
        suffix = f" [{default}]" if default else " (Enter to skip)"
        ans = input(f"  {question}{suffix}: ").strip()
    return ans or default


def yes_no(question: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        ans = input(f"  {question}{suffix}: ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# system checks
# ──────────────────────────────────────────────────────────────────────────────

def check_system_tools(need_docker: bool) -> None:
    banner("System prerequisites")
    required = [
        ("python3", ["python3", "--version"]),
        ("php",     ["php", "--version"]),
        ("composer",["composer", "--version"]),
        ("git",     ["git", "--version"]),
    ]
    if need_docker:
        required.append(("docker",         ["docker", "--version"]))
        required.append(("docker compose", ["docker", "compose", "version"]))

    missing = []
    for name, cmd in required:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            line = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else "present"
            ok(f"{name}: {line}")
        except FileNotFoundError:
            missing.append(name)
            fail(f"{name}: not found")

    if missing:
        print()
        fail(f"Install the missing tools and re-run: {', '.join(missing)}")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# tool repo presence
# ──────────────────────────────────────────────────────────────────────────────

def ensure_tool_repos_present() -> None:
    """Make sure the 6 tool repos exist as siblings of this script.

    Three paths handled:
      1. all present → do nothing
      2. .gitmodules exists in AI_ROOT → run `git submodule update --init`
      3. missing dirs and no submodules → offer to git-clone each one
    """
    banner("Checking tool repos")
    missing = [r for r in ALL_TOOL_REPOS if not (AI_ROOT / r).is_dir()]
    if not missing:
        ok(f"All {len(ALL_TOOL_REPOS)} tool repos present")
        return

    info(f"Missing repos: {', '.join(missing)}")

    # Submodule path — the parent is a git repo with .gitmodules listing them.
    if (AI_ROOT / ".gitmodules").exists() and (AI_ROOT / ".git").exists():
        info("Detected .gitmodules — initialising submodules instead of cloning.")
        run(["git", "submodule", "update", "--init", "--recursive"], cwd=AI_ROOT)
        still_missing = [r for r in missing if not (AI_ROOT / r).is_dir()]
        if still_missing:
            fail(f"Submodule init didn't produce: {', '.join(still_missing)}")
            fail("Fix .gitmodules or clone these manually, then re-run.")
            sys.exit(1)
        ok("Submodules initialised")
        return

    # Fresh-clone path.
    print()
    info(f"These repos can be cloned from {TOOL_REPO_BASE_URL}/<name>.git")
    info("Edit TOOL_REPO_OVERRIDES at the top of setup.py if you want different URLs.")
    if not yes_no(f"Clone the {len(missing)} missing repos now?", default=True):
        fail("Cannot continue without the tool repos. Aborting.")
        sys.exit(1)

    for name in missing:
        url = TOOL_REPO_OVERRIDES.get(name, f"{TOOL_REPO_BASE_URL}/{name}.git")
        info(f"Cloning {name} from {url}")
        run(["git", "clone", url, str(AI_ROOT / name)])
    ok(f"Cloned {len(missing)} repos")


# ──────────────────────────────────────────────────────────────────────────────
# instance management
# ──────────────────────────────────────────────────────────────────────────────

def configure_instance(state: dict) -> dict:
    """Prompt for the target Moodle instance, optionally creating it."""
    banner("Moodle instance")

    if state.get("instance"):
        info(f"Using saved instance: {state['instance']}")
        if not yes_no("Keep this instance?", default=True):
            state.pop("instance", None)

    if not state.get("instance"):
        instance_name = prompt("Instance name (used as dir name)", default="main")
        instance_path = Path(prompt(
            "Instance path",
            default=f"{DEFAULT_INSTANCE_ROOT}/{instance_name}",
        )).resolve()

        moodle_dir = instance_path / "moodle"
        docker_dir = instance_path / "moodle-docker"

        if instance_path.exists() and (moodle_dir.exists() or docker_dir.exists()):
            info(f"Found existing files at {instance_path}.")
            mode = prompt(
                "Use [e]xisting checkout or re-create from scratch [c]?",
                default="e",
            ).lower()
        else:
            mode = "c" if yes_no(
                f"{instance_path} is empty or missing. Create it now?", default=True
            ) else "e"

        if mode.startswith("c"):
            create_instance(instance_path)
        else:
            verify_existing_instance(instance_path)

        state["instance"]   = str(instance_path)
        state["moodle_dir"] = str(moodle_dir)
        state["docker_dir"] = str(docker_dir)
        save_state(state)

    return state


def create_instance(instance_path: Path) -> None:
    moodle_dir = instance_path / "moodle"
    docker_dir = instance_path / "moodle-docker"

    moodle_repo_url   = prompt("Moodle repo URL (your fork or upstream)",
                               default=DEFAULT_MOODLE_REPO_URL)
    moodle_branch     = prompt("Moodle branch to clone", default=DEFAULT_MOODLE_BRANCH)
    docker_repo_url   = prompt("moodle-docker repo URL",
                               default=DEFAULT_MOODLE_DOCKER_REPO_URL)
    docker_branch     = prompt("moodle-docker branch", default=DEFAULT_DOCKER_BRANCH)

    instance_path.mkdir(parents=True, exist_ok=True)

    if not moodle_dir.exists():
        info(f"Cloning Moodle ({moodle_branch}) from {moodle_repo_url} into {moodle_dir} ...")
        run(["git", "clone", "--branch", moodle_branch, "--depth", "1",
             moodle_repo_url, str(moodle_dir)])
    else:
        ok(f"Moodle source already at {moodle_dir}")

    if not docker_dir.exists():
        info(f"Cloning moodle-docker ({docker_branch}) from {docker_repo_url} into {docker_dir} ...")
        run(["git", "clone", "--branch", docker_branch, "--depth", "1",
             docker_repo_url, str(docker_dir)])
    else:
        ok(f"moodle-docker already at {docker_dir}")


def verify_existing_instance(instance_path: Path) -> None:
    moodle_dir = instance_path / "moodle"
    docker_dir = instance_path / "moodle-docker"
    missing = []
    if not (moodle_dir / "version.php").exists() and not (moodle_dir / "public" / "version.php").exists():
        missing.append(f"{moodle_dir}/version.php")
    if not (docker_dir / "bin" / "moodle-docker-compose").exists():
        missing.append(f"{docker_dir}/bin/moodle-docker-compose")
    if missing:
        fail("Existing instance is incomplete. Missing:")
        for m in missing:
            fail(f"  - {m}")
        sys.exit(1)
    ok(f"Existing instance verified at {instance_path}")


# ──────────────────────────────────────────────────────────────────────────────
# optional credentials
# ──────────────────────────────────────────────────────────────────────────────

def collect_optional_credentials(state: dict, force: bool = False) -> dict:
    """Prompt for optional API keys / tokens used by the harness.

    All are optional. Skipped values are remembered so re-runs don't re-ask.
    Use `setup.py creds` (or call with force=True) to update later.
    """
    if state.get("optional_creds_asked") and not force:
        return state

    banner("Optional credentials (Jira, TinyMCE)")
    info("These power Jira REST write-back and tiny_premium plugin work.")
    info("All are optional — press Enter to skip any field.")
    print()

    jira_url = prompt_optional(
        "Jira base URL (e.g. https://yoursite.atlassian.net)",
        default=state.get("jira_base_url", ""))
    if jira_url:
        state["jira_base_url"]   = jira_url
        state["jira_user_email"] = prompt_optional(
            "Jira user email", default=state.get("jira_user_email", ""))
        state["jira_api_token"]  = prompt_optional(
            "Jira API token", default=state.get("jira_api_token", ""), secret=True)
    else:
        # explicit empty so subsequent re-runs treat as 'asked but skipped'
        state.setdefault("jira_base_url", "")

    tiny = prompt_optional(
        "TinyMCE Premium API key (for tiny_premium plugin development)",
        default=state.get("tiny_premium_apikey", ""), secret=True)
    if tiny:
        state["tiny_premium_apikey"] = tiny
    else:
        state.setdefault("tiny_premium_apikey", "")

    state["optional_creds_asked"] = True
    save_state(state)
    return state


# ──────────────────────────────────────────────────────────────────────────────
# tool installation
# ──────────────────────────────────────────────────────────────────────────────

def install_python_tool(name: str) -> Path:
    """Create venv and pip install -e .[dev]. Returns venv path."""
    tool = AI_ROOT / name
    venv = tool / ".venv"
    if not venv.exists():
        info(f"Creating venv: {venv}")
        run(["python3", "-m", "venv", str(venv)])
    pip = venv / "bin" / "pip"
    info(f"Installing {name} (editable, with dev extras)")
    run([str(pip), "install", "--quiet", "--upgrade", "pip"])
    run([str(pip), "install", "--quiet", "-e", ".[dev]"], cwd=tool)
    ok(f"{name} installed")
    return venv


def install_php_tool(name: str, state: dict) -> None:
    """Install composer deps for a PHP tool.

    On failure (most often a PHP version mismatch — agentic_debug currently
    pins PHP 8.4) the user can choose to skip this tool. The skip is saved
    in state so re-runs don't keep hitting the same failure. The
    orchestrator treats a missing debug backend as non-blocking, so the
    other capabilities (docs, code, site) still work.
    """
    tool = AI_ROOT / name
    if (tool / "vendor").exists():
        ok(f"{name}: composer dependencies already installed")
        return

    skipped = state.setdefault("skipped_php_tools", [])
    if name in skipped:
        warn(f"{name}: previously skipped (re-add via `setup.py rebuild` after fixing PHP)")
        return

    info(f"Running composer install in {name}")
    r = subprocess.run(["composer", "install", "--no-interaction"],
                       cwd=tool, check=False)
    if r.returncode == 0:
        ok(f"{name} installed")
        return

    # Failed — most often PHP version mismatch. Offer to skip and continue.
    php = subprocess.run(["php", "--version"], capture_output=True, text=True, check=False)
    php_line = php.stdout.splitlines()[0] if php.stdout else "unknown"
    print()
    fail(f"composer install failed in {name}")
    info(f"Local PHP: {php_line}")
    info(f"This is usually a PHP version mismatch — {name} may require a newer PHP.")
    info("Options:")
    info(f"  1) install a newer PHP (e.g. PHP 8.4 via ondrej/php PPA on Ubuntu) and re-run setup.py")
    info(f"  2) skip {name} for now — the orchestrator treats it as non-blocking")
    info(f"     (e.g. agentic_debug skip → usable_for.debug_investigation = False;")
    info(f"     docs / code / site lookups still work)")
    print()
    if yes_no(f"Skip {name} and continue?", default=True):
        skipped.append(name)
        state["skipped_php_tools"] = skipped
        save_state(state)
        warn(f"{name} skipped. Re-run setup.py after fixing PHP to enable it.")
        return
    fail("Aborted by user.")
    sys.exit(1)


def install_playwright_chromium() -> None:
    sitemap_venv = AI_ROOT / "agentic_sitemap" / ".venv"
    pw = sitemap_venv / "bin" / "playwright"
    if not pw.exists():
        warn("playwright binary not found in agentic_sitemap venv; skipping")
        return
    # crude check: skip if a chromium dir already exists in ~/.cache/ms-playwright
    cache = Path.home() / ".cache" / "ms-playwright"
    if cache.exists() and any(p.name.startswith("chromium-") for p in cache.iterdir()):
        ok("Playwright Chromium already installed")
        return
    info("Installing Playwright Chromium browser (one-time)")
    run([str(pw), "install", "chromium"])
    ok("Playwright Chromium installed")


def install_all_tools(state: dict) -> None:
    banner("Installing tool dependencies")
    for t in PY_TOOLS:
        install_python_tool(t)
    for t in PHP_TOOLS:
        install_php_tool(t, state)
    install_playwright_chromium()
    skipped = state.get("skipped_php_tools", [])
    if skipped:
        print()
        warn(f"Skipped: {', '.join(skipped)} — re-run setup.py after fixing PHP to enable.")


# ──────────────────────────────────────────────────────────────────────────────
# resource builds
# ──────────────────────────────────────────────────────────────────────────────

def build_devdocs_db(rebuild: bool = False) -> Path:
    banner("Building Moodle devdocs database")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    devdocs_repo = DATA_DIR / "devdocs"
    devdocs_db   = DATA_DIR / "devdocs.db"

    agentic_docs = AI_ROOT / "agentic_devdocs" / ".venv" / "bin" / "agentic-docs"

    if rebuild and devdocs_db.exists():
        info(f"Rebuild requested; removing {devdocs_db}")
        devdocs_db.unlink()

    info(f"Syncing Moodle devdocs into {devdocs_repo}")
    run([str(agentic_docs), "sync",
         "--repo-url", DEVDOCS_REPO_URL,
         "--local-path", str(devdocs_repo)])

    if devdocs_db.exists():
        ok(f"Devdocs DB exists at {devdocs_db}; skipping ingest. (Use --rebuild to force.)")
        return devdocs_db

    info(f"Ingesting devdocs into {devdocs_db} (this can take a few minutes)")
    run([str(agentic_docs), "ingest",
         "--source", str(devdocs_repo),
         "--db-path", str(devdocs_db),
         "--tokenizer", "openai",
         "--max-tokens", "400",
         "--overlap-tokens", "60"])

    ok(f"Devdocs DB built: {devdocs_db}")
    return devdocs_db


def build_indexer_db(moodle_dir: Path, rebuild: bool = False) -> Path:
    banner("Building Moodle code index")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_path = DATA_DIR / "moodle-index.sqlite"

    moodle_indexer = AI_ROOT / "agentic_indexer" / ".venv" / "bin" / "moodle-indexer"

    if rebuild and db_path.exists():
        info(f"Rebuild requested; removing {db_path}")
        db_path.unlink()

    if db_path.exists():
        ok(f"Indexer DB exists at {db_path}; skipping. (Use --rebuild to force.)")
        return db_path

    workers = max(2, (os.cpu_count() or 4) // 2)
    info(f"Indexing {moodle_dir} (workers={workers}); this can take 5-15 minutes")
    run([str(moodle_indexer), "index",
         "--moodle-path", str(moodle_dir),
         "--db-path", str(db_path),
         "--workers", str(workers)])

    ok(f"Indexer DB built: {db_path}")
    return db_path


def build_sitemap_run(state: dict, rebuild: bool = False) -> Path | None:
    banner("Building sitemap discovery run")
    instance_name = Path(state["instance"]).name
    runs_dir = DATA_DIR / "sitemap-runs" / instance_name
    runs_dir.mkdir(parents=True, exist_ok=True)

    moodle_sitemap = AI_ROOT / "agentic_sitemap" / ".venv" / "bin" / "moodle-sitemap"

    # if a run already exists and we're not rebuilding, reuse the most recent one
    if not rebuild:
        existing = sorted([p for p in runs_dir.iterdir() if p.is_dir()], reverse=True)
        if existing:
            ok(f"Reusing latest sitemap run: {existing[0]}")
            return existing[0]

    # generate a sitemap config from state
    config_path = DATA_DIR / f"sitemap-{instance_name}.toml"
    config_path.write_text(
        f'[site]\nurl = "{state["site_url"]}"\n\n'
        f'[auth]\nusername = "{state["admin_username"]}"\n'
        f'password = "{state["admin_password"]}"\n\n'
        '[browser]\nengine = "chromium"\nheadless = true\n\n'
        '[run]\nrole = "admin"\nsettle_strategy = "adaptive"\n'
    )
    ok(f"Sitemap config written: {config_path}")

    # smoke first to confirm the site responds and login works
    info("Running sitemap smoke (login + post-login capture)")
    smoke_dir = runs_dir / "_smoke"
    smoke_dir.mkdir(exist_ok=True)
    smoke_result = run(
        [str(moodle_sitemap), "smoke", "--config", str(config_path)],
        cwd=smoke_dir, check=False,
    )
    if smoke_result.returncode != 0:
        fail("Sitemap smoke failed. Is Moodle running and reachable at the configured URL?")
        return None

    info("Running sitemap discovery (max-pages=200, max-depth=4)")
    discover_result = run(
        [str(moodle_sitemap), "discover",
         "--config", str(config_path),
         "--max-pages", "200",
         "--max-depth", "4"],
        cwd=runs_dir, check=False,
    )
    if discover_result.returncode != 0:
        fail("Sitemap discovery failed.")
        return None

    # find the timestamped dir created under runs_dir/discovery-runs/
    discovery_root = runs_dir / "discovery-runs"
    runs = sorted([p for p in discovery_root.iterdir() if p.is_dir()], reverse=True) \
        if discovery_root.exists() else []
    if not runs:
        fail("Could not locate discovery run directory after crawl.")
        return None

    ok(f"Sitemap run: {runs[0]}")
    return runs[0]


# ──────────────────────────────────────────────────────────────────────────────
# config writers
# ──────────────────────────────────────────────────────────────────────────────

def write_orchestrator_config(devdocs_db: Path, indexer_db: Path,
                              sitemap_run: Path | None) -> Path:
    banner("Writing orchestrator config")
    cfg_path = AI_ROOT / "agentic_orchestrator" / "config.local.toml"

    devdocs_cmd  = AI_ROOT / "agentic_devdocs"     / ".venv" / "bin" / "agentic-docs"
    indexer_cmd  = AI_ROOT / "agentic_indexer"     / ".venv" / "bin" / "moodle-indexer"
    sitemap_cmd  = AI_ROOT / "agentic_sitemap"     / ".venv" / "bin" / "moodle-sitemap"
    debug_cmd    = AI_ROOT / "agentic_debug"       / "bin"   / "moodle-debug"

    sitemap_run_line = (
        f'sitemap_run_dir = "{sitemap_run}"' if sitemap_run
        else '# sitemap_run_dir = "<populated after phase 2>"'
    )

    contents = f"""# Generated by /var/www/ai/setup.py — re-run setup to regenerate.

[tools.devdocs]
command = "{devdocs_cmd}"
workdir = "{AI_ROOT / 'agentic_devdocs'}"

[tools.indexer]
command = "{indexer_cmd}"
workdir = "{AI_ROOT / 'agentic_indexer'}"

[tools.sitemap]
command = "{sitemap_cmd}"
workdir = "{AI_ROOT / 'agentic_sitemap'}"

[tools.debug]
command = "{debug_cmd}"
workdir = "{AI_ROOT / 'agentic_debug'}"

[resources]
devdocs_db_path = "{devdocs_db}"
indexer_db_path = "{indexer_db}"
{sitemap_run_line}
"""
    cfg_path.write_text(contents)
    ok(f"Orchestrator config written: {cfg_path}")
    return cfg_path


def write_vscode_workspace(state: dict) -> Path:
    """Generate a VSCode multi-root workspace so Claude Code sees both trees.

    /var/www/ai/ is listed first so it becomes the default cwd for the
    integrated terminal — meaning CLAUDE.md and .claude/settings.json
    auto-load when Claude Code starts.
    """
    instance_name = Path(state["instance"]).name
    ws_path = DATA_DIR / f"{instance_name}.code-workspace"
    ws = {
        "folders": [
            {"path": str(AI_ROOT),                "name": "Agentic tools (ai)"},
            {"path": state["moodle_dir"],         "name": f"Moodle source ({instance_name})"},
            {"path": state["docker_dir"],         "name": f"moodle-docker ({instance_name})"},
        ],
        "settings": {
            "terminal.integrated.cwd": str(AI_ROOT),
        },
    }
    ws_path.write_text(json.dumps(ws, indent=2))
    ok(f"Workspace file: {ws_path}")
    return ws_path


def write_moodle_claude_config(state: dict) -> None:
    banner("Writing moodle_claude harness config")
    harness = AI_ROOT / HARNESS

    optional_lines = []
    if state.get("jira_base_url"):
        optional_lines.append(f'export JIRA_BASE_URL="{state["jira_base_url"]}"')
        optional_lines.append(f'export JIRA_USER_EMAIL="{state.get("jira_user_email", "")}"')
        optional_lines.append(f'export JIRA_API_TOKEN="{state.get("jira_api_token", "")}"')
    if state.get("tiny_premium_apikey"):
        optional_lines.append(f'export TINY_PREMIUM_APIKEY="{state["tiny_premium_apikey"]}"')
    optional_block = ("\n# Optional credentials\n" + "\n".join(optional_lines) + "\n"
                      if optional_lines else "")

    env_path = harness / ".claude.env"
    # Always overwrite — vars must be exported (not just shell-local) so that
    # moodle-docker-compose, started as a child process, sees them.
    env_path.write_text(f"""# Generated by /var/www/ai/setup.py — edit by hand if you need to.
# These are exported so child processes (moodle-docker-compose) see them.

export MOODLE_DIR="{state['moodle_dir']}"
export MOODLE_DOCKER_DIR="{state['docker_dir']}"
export AGENTIC_ORCHESTRATOR_DIR="{AI_ROOT / 'agentic_orchestrator'}"

# moodle-docker reads MOODLE_DOCKER_WWWROOT (not MOODLE_DIR) — alias it.
export MOODLE_DOCKER_WWWROOT="{state['moodle_dir']}"

# Default DB for moodle-docker. Change to mariadb / mysqli / mssql / oci if needed.
export MOODLE_DOCKER_DB="{state.get('moodle_docker_db', 'pgsql')}"

export WEBSERVER_SERVICE="webserver"
export WEBSERVER_USER="www-data"

export MOODLE_ADMIN_USERNAME="{state['admin_username']}"
export MOODLE_ADMIN_PASSWORD="{state['admin_password']}"
export MOODLE_ADMIN_EMAIL="{state['admin_email']}"

export PHPCS_BIN="phpcs"
export PHPCBF_BIN="phpcbf"
export PHPCS_STANDARD="moodle"
{optional_block}""")
    ok(f"Wrote {env_path}")

    identity_path = harness / ".claude.identity"
    if identity_path.exists() and not yes_no(
            f"{identity_path} exists. Overwrite?", default=False):
        info("Keeping existing .claude.identity")
    else:
        identity_path.write_text(f"""# Generated by /var/www/ai/setup.py
AUTHOR_NAME="{state['author_name']}"
AUTHOR_EMAIL="{state['author_email']}"
COPYRIGHT_YEAR="{state['copyright_year']}"
""")
        ok(f"Wrote {identity_path}")


# ──────────────────────────────────────────────────────────────────────────────
# verify
# ──────────────────────────────────────────────────────────────────────────────

def run_verify() -> bool:
    banner("Running orchestrator verify")
    orch = AI_ROOT / "agentic_orchestrator" / ".venv" / "bin" / "agentic-orchestrator"
    cfg  = AI_ROOT / "agentic_orchestrator" / "config.local.toml"
    r = run([str(orch), "verify", "--config", str(cfg), "--json"],
            cwd=AI_ROOT / "agentic_orchestrator", check=False, capture=True)
    if r.returncode != 0 and not r.stdout:
        fail("verify failed without JSON output")
        if r.stderr:
            print(r.stderr)
        return False

    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        fail("verify output was not valid JSON")
        print(r.stdout[:1000])
        return False

    overall = payload.get("overall_status") or payload.get("status") or "UNKNOWN"
    usable  = payload.get("usable_for") or {}
    print()
    info(f"overall: {overall}")
    for cap in ("docs_lookup", "code_context", "site_navigation",
                "debug_investigation", "pattern_discovery"):
        mark = "✓" if usable.get(cap) else "·"
        info(f"  {mark} {cap}: {usable.get(cap)}")
    blocking = payload.get("blocking_issues") or []
    if blocking:
        warn("Blocking issues:")
        for b in blocking:
            warn(f"  - {b}")
    return overall in ("READY", "OK", "DEGRADED")


# ──────────────────────────────────────────────────────────────────────────────
# moodle bring-up
# ──────────────────────────────────────────────────────────────────────────────

def moodle_is_running(site_url: str) -> bool:
    try:
        with urllib.request.urlopen(site_url, timeout=4) as r:
            return r.status < 500
    except Exception:
        return False


def prepare_moodle_for_docker(state: dict) -> bool:
    """Copy moodle-docker's config template into the Moodle checkout.

    moodle-docker requires a config.php at MOODLE_DOCKER_WWWROOT before the
    first 'up'. With the new public/ layout the config still lives at the
    root (parent of public/), and moodle-docker handles the docroot
    redirection from there.
    """
    moodle_dir = Path(state["moodle_dir"])
    docker_dir = Path(state["docker_dir"])
    template   = docker_dir / "config.docker-template.php"
    config_php = moodle_dir / "config.php"

    if config_php.exists():
        ok(f"config.php already present at {config_php}")
        return True
    if not template.exists():
        fail(f"moodle-docker config template not found: {template}")
        return False
    info(f"Copying config template → {config_php}")
    shutil.copy(template, config_php)
    ok("config.php in place")
    return True


def docker_env(state: dict) -> dict:
    """Build an env dict with everything moodle-docker needs, exported."""
    return {
        **os.environ,
        "MOODLE_DIR":             state["moodle_dir"],
        "MOODLE_DOCKER_WWWROOT":  state["moodle_dir"],
        "MOODLE_DOCKER_DIR":      state["docker_dir"],
        "MOODLE_DOCKER_DB":       state.get("moodle_docker_db", "pgsql"),
    }


def bring_up_moodle(state: dict) -> bool:
    """Run ./bin/up and (if Moodle isn't installed yet) ./bin/install.

    Returns True on success.
    """
    banner("Bringing up Moodle")
    harness = AI_ROOT / HARNESS
    bin_up      = harness / "bin" / "up"
    bin_install = harness / "bin" / "install"

    if not bin_up.exists():
        fail(f"{bin_up} not found — moodle_claude harness missing or broken")
        return False

    if not prepare_moodle_for_docker(state):
        return False

    env = docker_env(state)

    info("Starting Docker stack (./bin/up)")
    if subprocess.run([str(bin_up)], cwd=harness, env=env, check=False).returncode != 0:
        fail("./bin/up failed — check Docker daemon and moodle-docker config")
        return False
    ok("Docker stack up")

    # Wait briefly for the webserver to start responding before deciding install.
    site_url = state["site_url"]
    info(f"Waiting for {site_url} to respond ...")
    import time
    for _ in range(20):  # up to ~40s
        if moodle_is_running(site_url):
            break
        time.sleep(2)

    # If Moodle already responds AND has been installed before, skip install.
    # We treat "responds with non-error" as installed; ./bin/install would
    # fail on an already-installed site, so this matters.
    if moodle_is_running(site_url):
        # Try to detect "needs install" by checking if it returns the install page
        try:
            with urllib.request.urlopen(site_url, timeout=4) as r:
                body = r.read(4096).decode("utf-8", errors="ignore").lower()
            if "install" in body and ("moodle" not in body or "installation" in body):
                pass  # looks like an install page
            else:
                ok(f"Moodle already installed at {site_url} (skipping ./bin/install)")
                return True
        except Exception:
            pass

    info("Installing Moodle (./bin/install) — typically 3-5 minutes")
    if subprocess.run([str(bin_install)], cwd=harness, env=env, check=False).returncode != 0:
        fail("./bin/install failed — check the harness output above")
        return False
    ok("Moodle installed")

    # Final check
    if not moodle_is_running(site_url):
        fail(f"Install reported success but {site_url} still not responding")
        return False
    ok(f"Moodle reachable at {site_url}")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# phases
# ──────────────────────────────────────────────────────────────────────────────

def phase1(state: dict, rebuild: set[str]) -> dict:
    """Step 1 of 2 — everything that doesn't require Moodle to be running."""
    banner("Step 1 of 2 — install tools and build static resources")
    info("This step does not need Moodle running. It installs the 5 tools,")
    info("builds the docs DB and code index, and writes config files.")
    info("Step 2 (sitemap crawl) runs after you bring up Moodle.")
    check_system_tools(need_docker=False)
    ensure_tool_repos_present()
    state = configure_instance(state)

    # collect remaining inputs once
    if "site_url" not in state:
        state["site_url"]       = prompt("Moodle site URL (after install)", default=DEFAULT_SITE_URL)
        state["admin_username"] = prompt("Moodle admin username",          default=DEFAULT_ADMIN_USER)
        state["admin_password"] = prompt("Moodle admin password",          default=DEFAULT_ADMIN_PASS, secret=True)
        state["admin_email"]    = prompt("Moodle admin email",             default=DEFAULT_ADMIN_EMAIL)

    if "author_name" not in state:
        state["author_name"]    = prompt("Author name (for new file headers)", default="Your Name")
        state["author_email"]   = prompt("Author email",                       default="you@example.com")
        state["copyright_year"] = prompt("Copyright year",                      default="2026")

    state = collect_optional_credentials(state)
    save_state(state)

    install_all_tools(state)

    devdocs_db = build_devdocs_db(rebuild=("devdocs" in rebuild or "all" in rebuild))
    indexer_db = build_indexer_db(Path(state["moodle_dir"]),
                                  rebuild=("indexer" in rebuild or "all" in rebuild))

    write_orchestrator_config(devdocs_db, indexer_db, sitemap_run=None)
    write_moodle_claude_config(state)
    write_vscode_workspace(state)

    state["phase1_complete"] = True
    state["devdocs_db"]      = str(devdocs_db)
    state["indexer_db"]      = str(indexer_db)
    save_state(state)

    print()
    banner("Step 1 of 2 complete")
    print(f"""
  Static resources built:
    docs DB:    {state.get('devdocs_db')}
    code index: {state.get('indexer_db')}

  Step 2 needs a running Moodle — the sitemap tool logs in with the admin
  user and crawls authenticated pages, which can only happen against a
  real running install.
""")
    if yes_no(
        "Bring up Moodle now (./bin/up + ./bin/install) and continue to step 2?",
        default=True,
    ):
        if bring_up_moodle(state):
            print()
            return phase2(state, rebuild)
        fail("Moodle bring-up failed. Fix the issue and re-run setup.py.")
        return state

    print(f"""
  Manual path — bring up Moodle yourself and re-run setup.py:

    cd {AI_ROOT / HARNESS}
    ./bin/up && ./bin/install
    # optional but useful for daily work:
    ./bin/phpunit-init && ./bin/behat-init && ./bin/smoke

    cd {AI_ROOT}
    python3 setup.py
""")
    return state


def phase2(state: dict, rebuild: set[str]) -> dict:
    """Step 2 of 2 — sitemap crawl + final verify. Requires Moodle running."""
    banner("Step 2 of 2 — crawl Moodle and verify")
    info("This step logs into your running Moodle, crawls authenticated")
    info("pages to build the sitemap, points the orchestrator at it, and")
    info("runs a final verify so you can see which capabilities are usable.")
    check_system_tools(need_docker=True)

    # Always regenerate harness config so changes to the template (e.g. new
    # exports needed by moodle-docker) propagate into existing setups too.
    write_moodle_claude_config(state)
    write_vscode_workspace(state)

    if not moodle_is_running(state["site_url"]):
        warn(f"Moodle is not running at {state['site_url']}.")
        if yes_no("Bring it up now (./bin/up + ./bin/install if needed)?",
                  default=True):
            if not bring_up_moodle(state):
                sys.exit(1)
        else:
            info(f"  cd {AI_ROOT / HARNESS} && ./bin/up && ./bin/install")
            sys.exit(1)
    else:
        ok(f"Moodle reachable at {state['site_url']}")

    sitemap_run = build_sitemap_run(state,
                                    rebuild=("sitemap" in rebuild or "all" in rebuild))
    if sitemap_run is None:
        fail("Sitemap step failed; phase 2 aborted.")
        sys.exit(1)

    write_orchestrator_config(Path(state["devdocs_db"]),
                              Path(state["indexer_db"]),
                              sitemap_run)

    state["sitemap_run"]     = str(sitemap_run)
    state["phase2_complete"] = True
    save_state(state)

    ready = run_verify()
    print()
    banner("Setup complete" if ready else "Setup finished with issues")
    print()
    print("  Quick smoke test:")
    print(f"    cd {AI_ROOT / 'agentic_orchestrator'}")
    print('    .venv/bin/agentic-orchestrator query "add admin settings to a plugin" \\')
    print('       --config ./config.local.toml --route-mode auto --json | head -80')
    print()
    print("  Start using it from Claude Code — open a session at:")
    print(f"    {AI_ROOT}")
    print(f"  …or {AI_ROOT / HARNESS} for harness-only sessions.")
    print()
    print(f"  CLAUDE.md and .claude/settings.json are committed in {AI_ROOT};")
    print("  they auto-load and reduce permission prompts.")
    print()
    return state


# ──────────────────────────────────────────────────────────────────────────────
# entry points
# ──────────────────────────────────────────────────────────────────────────────

def cmd_status() -> None:
    state = load_state()
    if not state:
        info("No state yet. Run: python3 setup.py")
        return
    banner("Setup state")
    for k, v in state.items():
        info(f"{k}: {v}")
    if state.get("phase2_complete"):
        info("→ both steps complete; run `python3 setup.py` to re-verify")
    elif state.get("phase1_complete"):
        info("→ step 1 done, step 2 pending (bring up Moodle, then re-run)")
    else:
        info("→ step 1 in progress; run `python3 setup.py` to continue")


def cmd_reset() -> None:
    if not yes_no("This wipes _data/.setup_state.json (configs and built DBs are kept). Proceed?",
                  default=False):
        return
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        ok(f"Removed {STATE_FILE}")
    else:
        info("No state file to remove")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="show current state")
    sub.add_parser("reset",  help="wipe state (keeps built resources)")
    sub.add_parser("creds",  help="add or update optional credentials (Jira, TinyMCE)")
    rb = sub.add_parser("rebuild", help="force rebuild of one or more resources")
    rb.add_argument("targets", nargs="+",
                    choices=["devdocs", "indexer", "sitemap", "all"])
    args = parser.parse_args()

    if args.cmd == "status":
        cmd_status()
        return
    if args.cmd == "reset":
        cmd_reset()
        return
    if args.cmd == "creds":
        state = load_state()
        if not state:
            fail("Run setup.py first to bootstrap state.")
            sys.exit(1)
        state = collect_optional_credentials(state, force=True)
        if state.get("phase1_complete"):
            write_moodle_claude_config(state)
        return

    rebuild = set(getattr(args, "targets", []) or [])

    state = load_state()
    if not state.get("phase1_complete"):
        state = phase1(state, rebuild)
        # if phase1 finished and Moodle happens to already be running, continue to phase2
        if state.get("phase1_complete") and moodle_is_running(state.get("site_url", "")):
            phase2(state, rebuild)
        return

    if not state.get("phase2_complete"):
        phase2(state, rebuild)
        return

    # both phases complete — re-verify
    info("Both phases complete. Re-running verify...")
    run_verify()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        info("Interrupted.")
        sys.exit(130)
