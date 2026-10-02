#!/usr/bin/env python3
"""PreToolUse guard for logovobot.

Blocks the few actions that can't be undone or that leak real data:

* writing/editing ``.env*`` (except ``.env.example``), SQLite databases and ``backups/``;
* reading a real ``.env`` with Read or Grep (it would land in the transcript);
* shell commands that write to, delete or ``git add`` those files;
* destructive ``scripts/*.py --apply`` runs (season reset, purge, bulk rebinding).

Reads the hook payload from stdin and answers with a ``deny`` decision on stdout.
Anything it cannot parse is allowed through: a broken guard must not freeze the session.
"""
import json
import re
import sys

PROTECTED_NAME = re.compile(
    r"(?:^|/)(?:"
    r"\.env(?!\.example\b)(?:\.[\w.-]+)?"      # .env, .env.local, .env.production
    r"|[\w.-]+\.(?:db|sqlite3?)(?:-wal|-shm|-journal)?"  # league.db, league.db-wal, x.sqlite3
    r"|[\w.-]+\.db\.(?:gz|bak[\w.-]*)"          # league-2026....db.gz, *.db.bak-ts
    r")$",
    re.IGNORECASE,
)
BACKUPS_DIR = re.compile(r"(?:^|/)backups/", re.IGNORECASE)

# The same names, for finding them inside a shell command line.
PROTECTED_IN_CMD = re.compile(
    r"(?<![\w.-])(?:"
    r"\.env(?!\.example\b)(?:\.[\w.-]+)?"
    r"|[\w./-]*[\w-]+\.(?:db|sqlite3?)(?:-wal|-shm|-journal)?"
    r"|[\w./-]*\.db\.(?:gz|bak[\w.-]*)"
    r"|(?:\./)?backups/[\w./-]*"
    r")(?![\w.-])",
    re.IGNORECASE,
)
# ``.env`` itself, as a path component, for the "read a secret" check.
ENV_IN_CMD = re.compile(r"(?<![\w.-])\.env(?!\.example\b)(?:\.[\w.-]+)?(?![\w.-])", re.IGNORECASE)

WRITE_VERBS = re.compile(
    r"(?:^|[\s;&|(])(?:rm|rmdir|del|erase|mv|move|ren|rename|cp|copy|truncate|shred|"
    r"remove-item|move-item|copy-item|set-content|add-content|clear-content|out-file|"
    r"tee|dd|install)\b"
    r"|\bsed\s+(?:-\w*i|--in-place)"
    r"|(?<![>&\d])>{1,2}(?!&)",                 # shell redirection (not 2>&1)
    re.IGNORECASE,
)
READ_VERBS = re.compile(
    r"(?:^|[\s;&|(])(?:cat|type|head|tail|less|more|bat|grep|rg|sed|awk|get-content|gc|"
    r"select-string|sls|strings|xxd|od|base64|source|\.)\s",
    re.IGNORECASE,
)
SQL_WRITE = re.compile(
    r"\b(?:insert|update|delete|drop|alter|create|replace|vacuum|reindex|attach|"
    r"pragma\s+\w+\s*=|\.restore|\.import|\.clone)\b",
    re.IGNORECASE,
)
GIT_STAGE = re.compile(
    r"\bgit\s+(?:-[cC]\s+\S+\s+|-\S+\s+)*(?:add|stage|update-index)\b", re.IGNORECASE
)
# A commit message may name a protected file ("don't move league.db"); it is prose, not a command.
GIT_COMMIT = re.compile(r"\bgit\s+(?:-[cC]\s+\S+\s+|-\S+\s+)*commit\b", re.IGNORECASE)
COMMIT_MESSAGE = re.compile(
    r"""(?:-m|--message)(?:\s+|=)(?:"[^"]*"|'[^']*')"""   # -m "..." / --message='...'
    r"""|<<-?\s*(['"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\b""",    # heredoc body (commit -F - / $(cat <<EOF))
    re.DOTALL,
)
GIT_FORCE = re.compile(r"\s(?:-f|--force)\b")   # case-sensitive: `git commit -F msg` is not a force-add
DESTRUCTIVE_SCRIPT = re.compile(
    r"\b(?:scripts[/\\])?(?:purge_old_season|reset_season_data|reset_wallets_and_levels|"
    r"cleanup_preseason_data|clear_squads_and_photos|merge_player_spellings|"
    r"bind_clubs_to_players|backfill_model_wiring|seed_cup_bracket|recalculate_odds)\.py\b"
    r"[^\n;&|]*--apply\b",
    re.IGNORECASE,
)


def norm(path: str) -> str:
    return path.replace("\\", "/")


def is_protected_path(path: str) -> bool:
    p = norm(path)
    return bool(PROTECTED_NAME.search(p) or BACKUPS_DIR.search(p))


def is_real_env(path: str) -> bool:
    name = norm(path).rsplit("/", 1)[-1].lower()
    return name.startswith(".env") and name != ".env.example"


def check_file_tool(tool: str, inp: dict):
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    if not path:
        return None
    if tool in ("Read", "Grep"):
        if is_real_env(path):
            return f"Reading {path} would put real secrets into the transcript. Use .env.example for variable names."
        return None
    if is_protected_path(path):
        return (
            f"{path} holds secrets or real user data (.env*, SQLite databases, backups/) "
            "and is off limits for edits. Change it yourself outside Claude."
        )
    return None


def check_bash(cmd: str):
    flat = norm(cmd)
    if GIT_COMMIT.search(flat):
        flat = COMMIT_MESSAGE.sub(" ", flat)

    if DESTRUCTIVE_SCRIPT.search(flat):
        return (
            "This script runs destructive writes against the live league.db with --apply. "
            "Run it without --apply to preview, and apply it yourself in your own terminal."
        )

    targets = PROTECTED_IN_CMD.findall(flat)

    if GIT_STAGE.search(flat):
        if GIT_FORCE.search(flat) or targets:
            return "Staging .env*, databases or backups/ is blocked: they hold secrets and real user data."

    if not targets:
        return None

    if ENV_IN_CMD.search(flat) and READ_VERBS.search(flat):
        return "Reading .env through the shell would leak real secrets into the transcript."

    if WRITE_VERBS.search(flat):
        return f"Shell command would write to or delete a protected file ({targets[0]})."

    if re.search(r"\bsqlite3\b", flat, re.IGNORECASE) and SQL_WRITE.search(flat):
        return "sqlite3 with a write statement against a real database is blocked. Run read-only queries, or do it yourself."

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input")
    if not isinstance(inp, dict):
        inp = {}

    reason = None
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep"):
        reason = check_file_tool(tool, inp)
    elif tool in ("Bash", "PowerShell"):
        reason = check_bash(inp.get("command") or "")

    if reason:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"protect_guard: {reason}",
                }
            },
            sys.stdout,
            ensure_ascii=False,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
