"""
Tests for plugin format structure and manifest validity.

These are static checks — they verify the declarative plugin files are
correct so Claude Code can load them without a manual installer.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# plugin.json
# ---------------------------------------------------------------------------

class TestPluginManifest:

    def _load(self):
        return json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    def test_plugin_json_exists(self):
        assert (REPO_ROOT / ".claude-plugin" / "plugin.json").exists()

    def test_plugin_json_is_valid_json(self):
        data = self._load()
        assert isinstance(data, dict)

    def test_plugin_json_has_required_fields(self):
        data = self._load()
        for field in ("name", "description", "version", "author", "license"):
            assert field in data, f"plugin.json missing field: {field!r}"

    def test_plugin_name_is_nb(self):
        assert self._load()["name"] == "nb"

    def test_plugin_version_is_semver(self):
        version = self._load()["version"]
        assert re.match(r"^\d+\.\d+\.\d+", version), f"Not semver: {version!r}"

    def test_plugin_description_is_nonempty(self):
        assert self._load()["description"].strip()

    def test_plugin_author_has_name(self):
        author = self._load()["author"]
        assert isinstance(author, dict) and author.get("name", "").strip()


# ---------------------------------------------------------------------------
# marketplace.json
# ---------------------------------------------------------------------------

class TestMarketplaceManifest:
    """The repo self-hosts as a single-plugin marketplace so
    `/plugin marketplace add owner/repo` works — plugin install has no
    direct-from-repo-URL form."""

    def _load(self):
        return json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))

    def test_marketplace_json_exists(self):
        assert (REPO_ROOT / ".claude-plugin" / "marketplace.json").exists()

    def test_marketplace_json_is_valid_json(self):
        data = self._load()
        assert isinstance(data, dict)

    def test_marketplace_has_name_and_owner(self):
        data = self._load()
        assert data.get("name", "").strip()
        assert isinstance(data.get("owner"), dict) and data["owner"].get("name", "").strip()

    def test_marketplace_lists_exactly_the_nb_plugin(self):
        plugins = self._load()["plugins"]
        assert len(plugins) == 1
        assert plugins[0]["name"] == "nb"

    def test_marketplace_source_is_repo_root(self):
        source = self._load()["plugins"][0]["source"]
        assert source.startswith("./"), f"relative source must start with ./ : {source!r}"

    def test_marketplace_plugin_name_matches_plugin_json(self):
        plugin = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        assert self._load()["plugins"][0]["name"] == plugin["name"]


# ---------------------------------------------------------------------------
# hooks/hooks.json
# ---------------------------------------------------------------------------

class TestHooksManifest:

    def _load(self):
        return json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    def _nb_guard_hooks(self):
        """Return all hook entries for nb-guard."""
        data = self._load()
        entries = data.get("hooks", {}).get("PreToolUse", [])
        hooks = []
        for e in entries:
            for h in e.get("hooks", []):
                if "nb-guard" in h.get("command", "") or any("nb-guard" in a for a in h.get("args", [])):
                    hooks.append(h)
        return hooks

    def test_hooks_json_exists(self):
        assert (REPO_ROOT / "hooks" / "hooks.json").exists()

    def test_hooks_json_is_valid_json(self):
        data = self._load()
        assert isinstance(data, dict)

    def test_hooks_json_has_pre_tool_use(self):
        data = self._load()
        assert "PreToolUse" in data.get("hooks", {}), (
            "hooks.json must declare a PreToolUse hook"
        )

    def test_no_post_tool_use_hooks(self):
        data = self._load()
        assert "PostToolUse" not in data.get("hooks", {}), (
            "nb hook must not fire on PostToolUse"
        )

    def test_hook_matcher_covers_all_five_tools(self):
        data = self._load()
        entries = data["hooks"]["PreToolUse"]
        matchers = " | ".join(e.get("matcher", "") for e in entries)
        for tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
            assert tool in matchers, f"hooks.json matcher must cover {tool!r}"

    def test_hook_uses_plugin_root_env_var(self):
        for h in self._nb_guard_hooks():
            assert "${CLAUDE_PLUGIN_ROOT}" in h.get("command", ""), (
                "hook command must use ${CLAUDE_PLUGIN_ROOT}, not hardcoded path"
            )

    def test_no_absolute_paths_in_hook_command(self):
        for h in self._nb_guard_hooks():
            stripped = h.get("command", "").replace("${CLAUDE_PLUGIN_ROOT}", "")
            assert not re.search(r"(/home/|/Users/|[A-Z]:\\\\)", stripped), (
                f"Hardcoded absolute path in hook command: {h!r}"
            )

    def test_hook_resolves_interpreter_portably(self):
        """The hook must not hardcode a single interpreter name: python3 is
        absent on stock Windows, python may be absent on Debian. Shell-form
        with a command -v fallback chain runs under bash on POSIX and Git
        Bash on Windows (which Claude Code requires there)."""
        for h in self._nb_guard_hooks():
            cmd = h.get("command", "")
            assert "command -v python3" in cmd and "command -v python" in cmd, (
                f"hook must resolve the interpreter with a fallback chain: {cmd!r}"
            )

    def test_hook_references_nb_guard_py(self):
        hooks = self._nb_guard_hooks()
        assert hooks, "No nb-guard hook found in hooks.json"
        for h in hooks:
            assert "nb-guard.py" in h.get("command", ""), (
                f"hook command must reference nb-guard.py: {h}"
            )

    def test_hook_quotes_plugin_root_expansion(self):
        """Shell-form: the ${CLAUDE_PLUGIN_ROOT} expansion must be inside
        double quotes (the cache path can contain spaces), and the resolved
        interpreter variable must be quoted too."""
        for h in self._nb_guard_hooks():
            cmd = h.get("command", "")
            assert '"${CLAUDE_PLUGIN_ROOT}' in cmd, (
                f"plugin-root expansion must be double-quoted: {cmd!r}"
            )
            assert '"$PYBIN"' in cmd, f"interpreter var must be quoted: {cmd!r}"

    def test_hook_type_is_command(self):
        data = self._load()
        entries = data["hooks"]["PreToolUse"]
        for entry in entries:
            for h in entry.get("hooks", []):
                if "nb-guard" in h.get("command", "") or any("nb-guard" in a for a in h.get("args", [])):
                    assert h.get("type") == "command", (
                        f"hook 'type' must be 'command': {h}"
                    )


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

class TestSkillFiles:

    def test_skill_md_exists(self):
        assert (REPO_ROOT / "skills" / "nb" / "SKILL.md").exists()

    def test_skill_md_is_nonempty(self):
        content = (REPO_ROOT / "skills" / "nb" / "SKILL.md").read_text(encoding="utf-8")
        assert len(content.strip()) > 500, (
            "SKILL.md looks suspiciously short — a real skill file needs at least "
            "a frontmatter block plus a protocol section (>500 chars)"
        )

    def test_skill_md_not_at_wrong_nesting(self):
        """skills/SKILL.md would be silently ignored; must be skills/nb/SKILL.md."""
        assert not (REPO_ROOT / "skills" / "SKILL.md").exists(), (
            "skills/SKILL.md found at wrong nesting level — "
            "Claude Code expects skills/<name>/SKILL.md"
        )


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------

class TestScriptFiles:

    def test_hook_script_exists(self):
        assert (REPO_ROOT / "scripts" / "nb-guard.py").exists()

    def test_no_bash_scripts(self):
        sh_files = list((REPO_ROOT / "scripts").glob("*.sh"))
        assert not sh_files, (
            f"Bash scripts found — runtime must be pure Python: {sh_files}"
        )

    def test_hook_script_is_python(self):
        content = (REPO_ROOT / "scripts" / "nb-guard.py").read_text(encoding="utf-8")
        assert content.startswith("#!/usr/bin/env python3") or "import sys" in content

    def test_hook_script_is_executable(self):
        """On POSIX the script must be +x so the OS can exec it directly."""
        if sys.platform == "win32":
            return  # Windows does not use the executable bit
        script = REPO_ROOT / "scripts" / "nb-guard.py"
        assert os.access(script, os.X_OK), (
            f"{script} is not executable — run: chmod +x {script.name}"
        )


# ---------------------------------------------------------------------------
# No installer files
# ---------------------------------------------------------------------------

class TestNoInstallerFiles:

    def test_no_install_py(self):
        assert not (REPO_ROOT / "install.py").exists(), (
            "install.py should not exist — plugin format uses Claude Code's native install"
        )

    def test_no_uninstall_py(self):
        assert not (REPO_ROOT / "uninstall.py").exists(), (
            "uninstall.py should not exist — use 'claude plugin remove nb'"
        )

    def test_no_common_installer_module(self):
        assert not (REPO_ROOT / "_nb_install_common.py").exists(), (
            "_nb_install_common.py is dead weight without install.py"
        )

    def test_no_install_sh(self):
        assert not (REPO_ROOT / "install.sh").exists(), (
            "install.sh (old bash installer wrapper) should not exist — "
            "plugin format uses Claude Code's native install"
        )

    def test_no_uninstall_sh(self):
        assert not (REPO_ROOT / "uninstall.sh").exists(), (
            "uninstall.sh (old bash uninstaller wrapper) should not exist — "
            "use 'claude plugin remove nb'"
        )
