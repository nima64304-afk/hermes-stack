#!/usr/bin/env python3
# ============================================================
#  Hermes Stack — one-click Hugging Face Space provisioning
#  Runs inside GitHub Actions. Reads everything from env,
#  creates the Space + backup dataset, injects secrets/vars,
#  uploads the space/ folder and waits for RUNNING.
# ============================================================
from __future__ import annotations

import inspect
import os
import sys
import time
import traceback
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("::error:: huggingface_hub is not installed — pip install huggingface_hub")
    sys.exit(1)

# ---------------------------------------------------------------- config
SPACE_SECRETS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "HERMES_API_KEY",
    "NINEROUTER_API_KEY",
    "OMNI_ROUTER_KEY",
    "OMNI_ADMIN_PASSWORD",
    "ROUTER_INITIAL_PASSWORD",
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD",
    "HF_TOKEN",
]
SPACE_VARIABLES = [
    "BACKUP_REPO",
    "HERMES_MODEL",
    "HERMES_TIMEZONE",
    "OMNI_ENABLED",
    "HERMES_WEB_BACKEND",
]
REQUIRED = ["HF_TOKEN", "HF_USERNAME", "HF_SPACE_NAME"]

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
PRIVATE_SPACE = os.environ.get("PRIVATE_SPACE", "").lower() in ("1", "true", "yes")
BUILD_TIMEOUT = int(os.environ.get("BUILD_TIMEOUT", "2700"))  # 45 min


def log(msg: str) -> None:
    print(f"::{msg}" if msg.startswith(("error", "warning", "notice")) else msg, flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def check_env() -> tuple[str, str]:
    missing = [k for k in REQUIRED if not env(k)]
    if missing and DRY_RUN:
        print(f"::warning:: Dry-run without {', '.join(missing)} — using placeholders (no HF calls will be made).")
        return "dry-user", "dry-space"
    if missing:
        print(f"::error:: Missing required env/secrets: {', '.join(missing)}")
        print("::error:: Add HF_TOKEN / HF_USERNAME / HF_SPACE_NAME to the repo Secrets (the web wizard does this automatically).")
        sys.exit(1)
    hf_user = env("HF_USERNAME")
    space_name = env("HF_SPACE_NAME")
    if not space_name.replace("-", "").replace("_", "").isalnum():
        print("::error:: HF_SPACE_NAME may only contain letters, digits, '-' and '_'")
        sys.exit(1)
    return hf_user, space_name


def make_space_repo(api, repo_id: str, private: bool):
    """create the Space with whichever SDK kwarg this huggingface_hub has.

    0.3x/1.x used sdk=...; 2.x renamed it to space_sdk=... and dropped sdk
    entirely — that rename is what broke the deploy with
    "TypeError: HfApi.create_repo() got an unexpected keyword argument 'sdk'".
    Inspect the class (not the instance) so the check is stable.
    """
    params = inspect.signature(HfApi.create_repo).parameters
    kwargs = dict(repo_id=repo_id, repo_type="space", private=private, exist_ok=True)
    kwargs["space_sdk" if "space_sdk" in params else "sdk"] = "docker"
    if "space_hardware" in params:
        kwargs["space_hardware"] = "cpu-basic"
    return api.create_repo(**kwargs)


def write_summary(text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text)


def main() -> int:
    hf_user, space_name = check_env()
    repo_id = f"{hf_user}/{space_name}"
    backup_repo = env("BACKUP_REPO", f"{hf_user}/{space_name}-backup")

    api = HfApi(token=env("HF_TOKEN"))

    print("=" * 62)
    print(f" Target Space  : https://huggingface.co/spaces/{repo_id}")
    print(f" Backup dataset: {backup_repo} (private)")
    print(f" Mode          : {'DRY-RUN (no HF calls)' if DRY_RUN else 'LIVE'}")
    print(f" Visibility    : {'PRIVATE' if PRIVATE_SPACE else 'public'}")
    print("=" * 62)

    if DRY_RUN:
        space_dir = Path(__file__).resolve().parent.parent / "space"
        files = sorted(p.relative_to(space_dir).as_posix() for p in space_dir.rglob("*") if p.is_file())
        print(f"[dry-run] space/ contains {len(files)} files:")
        for f in files:
            print(f"  - {f}")
        print("[dry-run] Would set secrets: " + ", ".join(SPACE_SECRETS))
        print("[dry-run] Would set variables: " + ", ".join(SPACE_VARIABLES))
        write_summary(
            "## 🧪 Dry-run passed\n\n"
            f"- Space folder valid (`{len(files)}` files)\n"
            f"- Would deploy to **`{repo_id}`**\n"
            f"- Backup dataset: `{backup_repo}`\n\n"
            "Add real secrets + run again without `dry_run` to deploy. 🚀"
        )
        print("[dry-run] OK — workflow logic verified without touching Hugging Face.")
        return 0

    # ------------------------------------------------------------ whoami
    try:
        who = api.whoami()
        login = who.get("name", "?")
        print(f"[auth] Hugging Face token OK — logged in as: {login}")
        if login != hf_user:
            print(f"::warning:: HF_USERNAME ('{hf_user}') != token owner ('{login}') — using '{hf_user}' as repo owner.")
    except Exception as e:
        print(f"::error:: HF token invalid or expired: {e}")
        print("::error:: Create a WRITE token at https://huggingface.co/settings/tokens and update the HF_TOKEN secret.")
        return 1

    # ------------------------------------------------------ create repos
    print(f"[1/5] Creating (or reusing) Space {repo_id} (sdk=docker, cpu-basic, {'private' if PRIVATE_SPACE else 'public'})…")
    try:
        url = make_space_repo(api, repo_id, PRIVATE_SPACE)
    except Exception as e:
        # Print HF's own explanation — 402 here means the account cannot create
        # a Space for free any more, and the body says so explicitly.
        resp = getattr(e, "response", None)
        body = ""
        if resp is not None:
            try:
                body = resp.text
            except Exception:
                body = "<no body>"
        print(f"::error:: create_repo failed: {type(e).__name__}: {e}")
        if body:
            print(f"::error:: Hugging Face said: {body[:800]}")
        print("::error:: If this is 402, this Hugging Face account cannot create Spaces "
              "for free any more (HF removed the free tier). Either add a payment method "
              "at https://huggingface.co/settings/billing, or create the Space by hand at "
              "https://huggingface.co/new-space (Docker) and re-run this workflow — "
              "exist_ok=True will then reuse it.")
        raise
    print(f"      -> {url}")

    print(f"[1/5] Creating (or reusing) private backup dataset {backup_repo}…")
    try:
        api.create_repo(repo_id=backup_repo, repo_type="dataset", private=True, exist_ok=True)
        print("      -> backup dataset ready")
    except Exception as e:
        print(f"::warning:: could not create backup dataset: {e}")

    # ------------------------------------------------- secrets & vars
    print("[2/5] Injecting Space secrets…")
    for key in SPACE_SECRETS:
        val = env(key)
        if not val:
            print(f"      - {key}: (skipped — empty)")
            continue
        api.add_space_secret(repo_id=repo_id, key=key, value=val)
        print(f"      - {key}: ✓ set")

    print("[3/5] Injecting Space variables…")
    defaults = {
        "HERMES_TIMEZONE": "Asia/Tehran",
        "OMNI_ENABLED": "true",
        "HERMES_WEB_BACKEND": "tavily",
    }
    for key in SPACE_VARIABLES:
        val = env(key, defaults.get(key, ""))
        if not val:
            print(f"      - {key}: (skipped — empty)")
            continue
        api.add_space_variable(repo_id=repo_id, key=key, value=val)
        print(f"      - {key}: {key in ('BACKUP_REPO',) and val or '✓ set'}")

    # -------------------------------------------------------- upload
    print("[4/5] Uploading space/ files (Dockerfile, Caddyfile, hfkit)…")
    space_dir = Path(__file__).resolve().parent.parent / "space"
    api.upload_folder(
        folder_path=str(space_dir),
        repo_id=repo_id,
        repo_type="space",
        commit_message="deploy: hermes-stack via GitHub Actions",
    )
    print("      -> uploaded. Build started on Hugging Face…")

    # ---------------------------------------------------------- wait
    print(f"[5/5] Waiting for build (timeout {BUILD_TIMEOUT}s)…")
    start = time.time()
    last_stage = ""
    space_url = f"https://huggingface.co/spaces/{repo_id}"
    app_url = f"https://{hf_user}-{space_name}.hf.space"
    while time.time() - start < BUILD_TIMEOUT:
        try:
            rt = api.get_space_runtime(repo_id=repo_id)
            stage = getattr(rt, "stage", str(rt))
            if stage != last_stage:
                elapsed = int(time.time() - start)
                print(f"      [{elapsed:>4}s] stage: {stage}")
                last_stage = stage
            if stage == "RUNNING":
                print()
                print("🎉 SPACE IS RUNNING!")
                write_summary(
                    "## 🎉 Deployment successful — your Space is RUNNING!\n\n"
                    f"| Service | URL |\n|---|---|\n"
                    f"| 🏠 Space | {space_url} |\n"
                    f"| 🌐 Router dashboard (root) | {app_url} |\n"
                    f"| 🧠 Agent web dashboard | {app_url}/hermes/ |\n"
                    f"| 🔌 Agent API (OpenAI-compatible) | {app_url}/hermes-api/v1 |\n"
                    f"| 💾 Backups | https://huggingface.co/datasets/{backup_repo} |\n\n"
                    "**Next steps**\n\n"
                    "1. Open the Telegram bot and send `/start`.\n"
                    "2. The `Keep Space Awake` GitHub Action (already in this repo) pings "
                    "`/healthz` every 10 minutes — no external cron service needed.\n"
                    "3. Change any value later in Space **Settings → Variables and secrets**, "
                    "then restart the Space.\n"
                )
                return 0
            if stage in ("BUILD_ERROR", "RUN_ERROR", "CONFIG_ERROR", "NO_APP_FILE"):
                print(f"::error:: Space entered error state: {stage}")
                print(f"::error:: Logs: {space_url}/logs/build")
                write_summary(f"## ❌ Space build failed\n\nState: `{stage}`\n\n[Build logs]({space_url}/logs/build)")
                return 1
        except Exception as e:
            print(f"      (runtime poll failed: {e} — retrying)")
        time.sleep(30)

    print(f"::warning:: Timed out after {BUILD_TIMEOUT}s — the build may still be running (large base images).")
    write_summary(
        "## ⏳ Build still in progress\n\n"
        f"The first build downloads large base images (10–25 min). Check the status here: {space_url}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        print("::error:: Unexpected failure — see traceback above.")
        sys.exit(1)
