"""The PreToolUse guard in .claude/hooks/protect_guard.py (secrets, databases, backups)."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / ".claude" / "hooks" / "protect_guard.py"

spec = importlib.util.spec_from_file_location("protect_guard", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def decide(tool, **tool_input):
    """The guard's reason for denying the call, or None when it lets it through."""
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep"):
        return guard.check_file_tool(tool, tool_input)
    return guard.check_bash(tool_input.get("command", ""))


class TestFileTools:
    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
    @pytest.mark.parametrize("path", [
        ".env",
        r"C:\proj\.env",
        "C:/proj/.env.local",
        ".env.production",
        "league.db",
        "log/server_league.db",
        "league.db-wal",
        "x.sqlite3",
        "backups/league-20261002-120000.db.gz",
        r"backups\anything.txt",
        "league.db.bak-20261002",
    ])
    def test_writes_to_protected_paths_are_denied(self, tool, path):
        assert decide(tool, file_path=path)

    @pytest.mark.parametrize("path", [
        ".env.example",
        "config.py",
        "database.py",
        "tests/test_db_backup.py",
        "services/db_backup.py",
        "docs/backups.md",
        ".claude/settings.json",
        ".mcp.json",
    ])
    def test_ordinary_files_are_allowed(self, path):
        assert decide("Write", file_path=path) is None

    @pytest.mark.parametrize("path", [".env", "C:/proj/.env.local", r"C:\proj\.env.production"])
    def test_reading_a_real_env_is_denied(self, path):
        assert decide("Read", file_path=path)

    @pytest.mark.parametrize("path", [".env.example", "league.db", "config.py"])
    def test_reading_other_files_is_allowed(self, path):
        assert decide("Read", file_path=path) is None

    def test_grep_straight_at_a_real_env_is_denied(self):
        assert decide("Grep", path=".env", pattern="TOKEN")

    def test_grep_over_a_directory_is_allowed(self):
        assert decide("Grep", path="services", pattern="TOKEN") is None
        assert decide("Grep", pattern="TOKEN") is None


class TestShell:
    @pytest.mark.parametrize("cmd", [
        "rm league.db",
        "rm -f backups/league-1.db.gz",
        "cp .env /tmp/x",
        "mv league.db league.old",
        "echo x > .env",
        "echo x >> .env.local",
        "Remove-Item league.db",
        "sed -i s/a/b/ .env",
        "git add .env",
        "git add league.db",
        "git add -f anything",
        "git -C repo add backups/x.db.gz",
        "sqlite3 league.db 'DELETE FROM users'",
        "sqlite3 league.db \"drop table users\"",
        "cat .env",
        "grep TOKEN .env",
        "type .env",
        "Get-Content .env",
        "head -5 .env.production",
        "python scripts/purge_old_season.py --apply",
        "python scripts/reset_season_data.py --dry --apply",
        "python scripts/bind_clubs_to_players.py --apply",
        "python scripts/seed_cup_bracket.py --division 2 --from-winners --apply",
    ])
    def test_dangerous_commands_are_denied(self, cmd):
        assert decide("Bash", command=cmd), cmd

    @pytest.mark.parametrize("cmd", [
        "python -m pytest tests/ -q",
        "python -m pytest tests/ > out.txt 2>&1",
        "git status",
        "git add database.py handlers/admin.py",
        "git add .claude/hooks/protect_guard.py .claude/settings.json",
        "git add .env.example",
        "cat .env.example",
        "ls backups/",
        "sqlite3 league.db 'SELECT count(*) FROM users'",
        "sqlite3 league.db .tables",
        "python scripts/purge_old_season.py",
        "python scripts/bind_clubs_to_players.py",
        "python scripts/audit_team_resolution.py",
        "python scripts/seed_cup_bracket.py --division 2 --from-winners",
        "git commit -m 'feat: db backups'",
        "git push origin HEAD:main",
    ])
    def test_ordinary_commands_are_allowed(self, cmd):
        assert decide("Bash", command=cmd) is None, cmd

    def test_add_then_commit_from_file_or_heredoc_is_allowed(self):
        cmd = (
            "git add config.py && git commit -q -F - <<'EOF'\n"
            "chore: guard .env*, league.db and backups/ (do not copy them)\n"
            "EOF\n"
            "git push origin HEAD:main"
        )
        assert decide("Bash", command=cmd) is None
        assert decide("Bash", command="git add config.py && git commit -F msg.txt") is None

    @pytest.mark.parametrize("cmd", [
        "git commit -m 'fix: do not move league.db into the repo'",
        "git commit -m 'chore: copy .env.example to .env docs'",
    ])
    def test_commit_message_mentioning_a_protected_file_is_allowed(self, cmd):
        assert decide("Bash", command=cmd) is None, cmd


def run_hook(payload):
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


class TestProtocol:
    def test_deny_is_a_pretooluse_decision_with_exit_zero(self):
        res = run_hook(json.dumps({"tool_name": "Write", "tool_input": {"file_path": ".env"}}))
        assert res.returncode == 0
        out = json.loads(res.stdout)["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        assert out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"].startswith("protect_guard:")

    def test_bash_deny_goes_through_stdin_payload(self):
        res = run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm league.db"}}))
        assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_allowed_call_prints_nothing(self):
        res = run_hook(json.dumps({"tool_name": "Write", "tool_input": {"file_path": "config.py"}}))
        assert (res.returncode, res.stdout) == (0, "")

    @pytest.mark.parametrize("payload", ["", "not json", "[]", json.dumps({"tool_name": "Write"}),
                                         json.dumps({"tool_name": "Bash", "tool_input": None})])
    def test_garbage_input_fails_open(self, payload):
        res = run_hook(payload)
        assert (res.returncode, res.stdout) == (0, "")

    def test_unrelated_tools_are_ignored(self):
        res = run_hook(json.dumps({"tool_name": "WebFetch", "tool_input": {"url": "https://x/.env"}}))
        assert (res.returncode, res.stdout) == (0, "")


class TestWiring:
    """settings.json must reach the script that these tests exercise."""

    def test_settings_runs_the_guard_for_every_tool_it_checks(self):
        cfg = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        (entry,) = cfg["hooks"]["PreToolUse"]
        assert "protect_guard.py" in entry["hooks"][0]["command"]
        matched = set(entry["matcher"].split("|"))
        assert {"Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Bash", "PowerShell", "Grep"} <= matched

    def test_mcp_json_declares_context7(self):
        cfg = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        assert "@upstash/context7-mcp" in " ".join(cfg["mcpServers"]["context7"]["args"])
        s = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "context7" in s["enabledMcpjsonServers"]
