#!/usr/bin/env bash
# uninstall.sh — remove only deepseek-mcp assets this checkout can prove it owns.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATH_GUARD="$PROJECT_ROOT/scripts/installer_path_guard.py"
CONFIG_DIR="$HOME/.deepseek-mcp"
VENV_ROOT="$CONFIG_DIR/claude-venvs"
LEGACY_VENV="$PROJECT_ROOT/.venv"
INSTALL_LOCK="$CONFIG_DIR/claude-install.lock"
HELPER_ROOT="$CONFIG_DIR/claude-helpers"
CLAUDE_BIN="${DEEPSEEK_CLAUDE_BIN:-claude}"
SKILL_SRC="$PROJECT_ROOT/skills/delegate-to-deepseek"
SKILL_DST="$HOME/.claude/skills/delegate-to-deepseek"
COMMAND_SRC="$PROJECT_ROOT/commands/ds.md"
COMMAND_DST="$HOME/.claude/commands/ds.md"

echo "▶ deepseek-mcp uninstaller"

case "$(uname -s 2>/dev/null)" in
    MINGW*|CYGWIN*|MSYS*) PLATFORM=windows ;;
    *) PLATFORM=unix ;;
esac

PYTHON_CMD=""
supported_python() {
    "$@" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else 1)' \
        >/dev/null 2>&1
}

find_python() {
    local candidate version
    for candidate in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && supported_python "$candidate"; then
            PYTHON_CMD="$candidate"
            return 0
        fi
    done
    if command -v py >/dev/null 2>&1; then
        for version in -3.12 -3.11 -3.10; do
            if supported_python py "$version"; then
                PYTHON_CMD="py $version"
                return 0
            fi
        done
    fi
    return 1
}

if ! find_python; then
    echo "✗ No supported Python 3.10–3.12 found in PATH; cannot safely validate the uninstall paths." >&2
    exit 1
fi
$PYTHON_CMD "$PATH_GUARD" validate-private-dirs "$HOME" \
    ".claude" ".claude/skills" ".claude/commands"

normalize_command() {
    local converted=""
    if [ "$PLATFORM" = "windows" ] && command -v cygpath >/dev/null 2>&1; then
        converted="$(cygpath -u "$1" 2>/dev/null || true)"
    fi
    if [ -n "$converted" ]; then
        printf '%s' "$converted"
    else
        printf '%s' "$1"
    fi
}

same_command_path() {
    [ "$(normalize_command "$1")" = "$(normalize_command "$2")" ]
}

is_managed_registration() {
    local candidate relative generation suffix token first
    candidate="$(normalize_command "$1")"
    case "$candidate" in
        "$LEGACY_VENV/bin/deepseek-mcp"|"$LEGACY_VENV/bin/deepseek-mcp.exe"|\
        "$LEGACY_VENV/Scripts/deepseek-mcp"|"$LEGACY_VENV/Scripts/deepseek-mcp.exe")
            return 0
            ;;
        "$VENV_ROOT"/*) relative="${candidate#"$VENV_ROOT"/}" ;;
        *) return 1 ;;
    esac
    generation="${relative%%/*}"
    suffix="${relative#*/}"
    [ "$generation/$suffix" = "$relative" ] || return 1
    case "$suffix" in
        bin/deepseek-mcp|bin/deepseek-mcp.exe|\
        Scripts/deepseek-mcp|Scripts/deepseek-mcp.exe) ;;
        *) return 1 ;;
    esac
    case "$generation" in generation.*) token="${generation#generation.}" ;; *) return 1 ;; esac
    [ -n "$token" ] && [ "${#token}" -le 128 ] || return 1
    case "$token" in *[!A-Za-z0-9_-]*) return 1 ;; esac
    first="${token%"${token#?}"}"
    case "$first" in [A-Za-z0-9]) return 0 ;; *) return 1 ;; esac
}

registration_snapshot() {
    local details="" command="" custom="" listing=""
    if details="$("$CLAUDE_BIN" mcp get deepseek 2>/dev/null)"; then
        command="$(printf '%s\n' "$details" | sed -n 's/^  Command: //p' | tr -d '\r')"
        custom="$(printf '%s\n' "$details" | sed -n -e 's/^  Args: //p' \
            -e '/^  Environment:$/,/^$/ { /^    /p; }')"
        [ -n "$command" ] && [ -z "$custom" ] \
            && printf '%s\n' "$details" | grep -q '^  Scope: User' \
            && printf '%s\n' "$details" | grep -q '^  Type: stdio$' \
            && is_managed_registration "$command" || return 1
        printf 'present:%s' "$command"
        return 0
    fi
    listing="$("$CLAUDE_BIN" mcp list 2>/dev/null)" || return 1
    printf '%s\n' "$listing" | grep -q '^deepseek:' && return 1
    printf 'absent'
}

registration_matches_expected() {
    local expected="$1" snapshot="" current=""
    snapshot="$(registration_snapshot)" || return 1
    if [ -z "$expected" ]; then
        [ "$snapshot" = "absent" ]
        return
    fi
    case "$snapshot" in present:*) current="${snapshot#present:}" ;; *) return 1 ;; esac
    same_command_path "$current" "$expected"
}

is_managed_helper_target() {
    local label="$1" candidate="$2" relative generation suffix token first
    case "$candidate" in
        "$HELPER_ROOT"/*) relative="${candidate#"$HELPER_ROOT"/}" ;;
        *) return 1 ;;
    esac
    generation="${relative%%/*}"
    suffix="${relative#*/}"
    [ "$generation/$suffix" = "$relative" ] || return 1
    case "$label:$suffix" in skill:skill|command:ds.md) ;; *) return 1 ;; esac
    case "$generation" in generation.*) token="${generation#generation.}" ;; *) return 1 ;; esac
    [ -n "$token" ] && [ "${#token}" -le 128 ] || return 1
    case "$token" in *[!A-Za-z0-9_-]*) return 1 ;; esac
    first="${token%"${token#?}"}"
    case "$first" in [A-Za-z0-9]) return 0 ;; *) return 1 ;; esac
}

asset_is_current() {
    local label="$1" src="$2" dst="$3" target=""
    if [ -L "$dst" ]; then
        target="$(readlink "$dst" 2>/dev/null || true)"
        [ "$target" = "$src" ] || is_managed_helper_target "$label" "$target"
    else
        $PYTHON_CMD "$PATH_GUARD" helper-current "$label" "$src" "$dst" \
            >/dev/null 2>&1
    fi
}

published_copy_is_owned() {
    $PYTHON_CMD "$PATH_GUARD" helper-published "$1" "$2" \
        >/dev/null 2>&1
}

asset_state() {
    local label="$1" src="$2" dst="$3"
    if [ ! -e "$dst" ] && [ ! -L "$dst" ]; then
        printf 'absent'
    elif asset_is_current "$label" "$src" "$dst" \
        || published_copy_is_owned "$label" "$dst"; then
        printf 'owned'
    else
        return 1
    fi
}

restore_quarantined_asset() {
    local quarantined="$1" dst="$2"
    mv -n -- "$quarantined" "$dst" 2>/dev/null || true
    if [ -e "$quarantined" ] || [ -L "$quarantined" ]; then
        echo "✗ The raced asset was preserved at $quarantined; nothing was deleted." >&2
        return 1
    fi
}

set_staged_quarantine() {
    case "$1" in
        skill) SKILL_QUARANTINE="$2" ;;
        command) COMMAND_QUARANTINE="$2" ;;
        *) return 1 ;;
    esac
}

clear_staged_quarantine() {
    case "$1" in
        skill) SKILL_QUARANTINE="" ;;
        command) COMMAND_QUARANTINE="" ;;
        *) return 1 ;;
    esac
}

stage_owned_asset_removal() {
    local label="$1" src="$2" dst="$3" state="$4" quarantine quarantined
    [ "$state" = "owned" ] || return 0
    quarantine="$(mktemp -d "${dst}.deepseek-mcp.XXXXXX")" || return 1
    quarantined="$quarantine/asset"
    set_staged_quarantine "$label" "$quarantine"
    if ! mv -n -- "$dst" "$quarantined" 2>/dev/null; then
        clear_staged_quarantine "$label"
        rmdir "$quarantine" 2>/dev/null || true
        echo "✗ $dst changed during uninstall; deletion refused." >&2
        return 1
    fi
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        echo "✗ $dst changed during uninstall; deletion refused." >&2
        return 1
    fi
    if ! asset_is_current "$label" "$src" "$quarantined" \
        && ! published_copy_is_owned "$label" "$quarantined"; then
        restore_quarantined_asset "$quarantined" "$dst" || true
        if [ ! -e "$quarantined" ] && [ ! -L "$quarantined" ]; then
            clear_staged_quarantine "$label"
            rmdir "$quarantine" 2>/dev/null || true
        fi
        echo "✗ $dst changed during uninstall; deletion refused." >&2
        return 1
    fi
}

restore_staged_helpers() {
    local failed=0 label quarantine quarantined dst
    for label in skill command; do
        if [ "$label" = "skill" ]; then
            quarantine="$SKILL_QUARANTINE" dst="$SKILL_DST"
        else
            quarantine="$COMMAND_QUARANTINE" dst="$COMMAND_DST"
        fi
        [ -n "$quarantine" ] || continue
        quarantined="$quarantine/asset"
        if restore_quarantined_asset "$quarantined" "$dst"; then
            clear_staged_quarantine "$label"
            rmdir "$quarantine" 2>/dev/null || true
        else
            failed=1
        fi
    done
    return "$failed"
}

discard_staged_helpers() {
    local label quarantine
    for label in skill command; do
        if [ "$label" = "skill" ]; then
            quarantine="$SKILL_QUARANTINE"
        else
            quarantine="$COMMAND_QUARANTINE"
        fi
        [ -n "$quarantine" ] || continue
        if rm -rf -- "$quarantine"; then
            clear_staged_quarantine "$label"
        else
            echo "warning: failed to clean up helper quarantine: $quarantine" >&2
        fi
    done
}

$PYTHON_CMD "$PATH_GUARD" prepare-dirs "$CONFIG_DIR"
LOCK_HELD=0 UNINSTALL_SUCCEEDED=0 REGISTRATION_TRANSACTION=0
SKILL_QUARANTINE="" COMMAND_QUARANTINE=""
release_install_lock() {
    if [ "$LOCK_HELD" -eq 1 ]; then
        rmdir "$INSTALL_LOCK" \
            || echo "warning: could not release the install lock: $INSTALL_LOCK" >&2
        LOCK_HELD=0
    fi
}
restore_uninstall_registration() {
    local snapshot="" current=""
    [ -n "$REGISTERED_COMMAND" ] || return 0
    snapshot="$(registration_snapshot)" || return 1
    case "$snapshot" in
        present:*)
            current="${snapshot#present:}"
            same_command_path "$current" "$REGISTERED_COMMAND"
            return ;;
        absent) ;;
        *) return 1 ;;
    esac
    "$CLAUDE_BIN" mcp add deepseek -s user -- "$REGISTERED_COMMAND" \
        >/dev/null 2>&1 || return 1
    registration_matches_expected "$REGISTERED_COMMAND"
}

on_exit() {
    local exit_code=$?
    if [ "$REGISTRATION_TRANSACTION" -eq 1 ] \
        && [ "$UNINSTALL_SUCCEEDED" -ne 1 ]; then
        trap '' INT TERM HUP
        if ! restore_uninstall_registration; then
            echo "✗ Uninstall failed and the original Claude registration could not be restored; verify manually." >&2
        fi
    fi
    if [ "$UNINSTALL_SUCCEEDED" -ne 1 ]; then
        restore_staged_helpers \
            || echo "✗ Uninstall failed and helper recovery is incomplete; check the quarantine." >&2
    fi
    release_install_lock
    return "$exit_code"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

if ! mkdir "$INSTALL_LOCK" 2>/dev/null; then
    echo "✗ Another install/uninstall transaction is running, or an unacknowledged lock remains: $INSTALL_LOCK" >&2
    exit 1
fi
LOCK_HELD=1

REGISTERED_COMMAND=""
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    echo "✗ claude CLI is not in PATH; cannot verify the registration; nothing was deleted." >&2
    echo "  Fix PATH or set DEEPSEEK_CLAUDE_BIN and retry." >&2
    exit 1
fi
REGISTERED_SNAPSHOT="$(registration_snapshot)" || {
    echo "✗ The deepseek MCP registration is not owned by this installer, or cannot be parsed safely; nothing was deleted." >&2
    exit 1
}
case "$REGISTERED_SNAPSHOT" in
    present:*) REGISTERED_COMMAND="${REGISTERED_SNAPSHOT#present:}" ;;
    absent) REGISTERED_COMMAND="" ;;
    *) echo "✗ Could not read the deepseek MCP registration." >&2; exit 1 ;;
esac

# Preflight every asset before the first destructive operation.
SKILL_STATE="$(asset_state skill "$SKILL_SRC" "$SKILL_DST")" || {
    echo "✗ $SKILL_DST is not owned by this installer; nothing was deleted." >&2
    exit 1
}
COMMAND_STATE="$(asset_state command "$COMMAND_SRC" "$COMMAND_DST")" || {
    echo "✗ $COMMAND_DST is not owned by this installer; nothing was deleted." >&2
    exit 1
}

echo "[1/4] Removing skill / command deployments..."
stage_owned_asset_removal skill "$SKILL_SRC" "$SKILL_DST" "$SKILL_STATE"
stage_owned_asset_removal command "$COMMAND_SRC" "$COMMAND_DST" "$COMMAND_STATE"
echo "       Removed assets whose ownership matched"

echo "[2/4] Removing mcp from Claude Code..."
if ! registration_matches_expected "$REGISTERED_COMMAND"; then
    echo "✗ The deepseek MCP registration changed during uninstall; it was not removed." >&2
    exit 1
elif [ -z "$REGISTERED_COMMAND" ]; then
    echo "       Not registered; skipping"
else
    REGISTRATION_TRANSACTION=1
    "$CLAUDE_BIN" mcp remove deepseek -s user >/dev/null 2>&1
    registration_matches_expected "" || {
        echo "✗ Could not confirm the deepseek MCP registration was removed." >&2
        exit 1
    }
    echo "       Removed this installer's registration"
fi

echo "[3/4] Config directory:"
echo "       $CONFIG_DIR still exists (contains the API key, logs, and runtime)"
echo "       To delete it, first review its contents and remove it manually."

echo "[4/4] Legacy shell rc pure aliases that older versions may have left behind:"
FOUND_RC=0
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    if [ -f "$rc" ] && grep -q "===== deepseek-orchestrator:" "$rc" 2>/dev/null; then
        echo "       still present in $rc; manually remove the deepseek-orchestrator block"
        FOUND_RC=1
    fi
done
[ "$FOUND_RC" = "0" ] && echo "       None found"

UNINSTALL_SUCCEEDED=1
REGISTRATION_TRANSACTION=0
discard_staged_helpers

echo ""
echo "✅ Claude registrations and assets verifiably owned by this installer have been cleaned up"
echo "   The project directory $PROJECT_ROOT and user configuration were not deleted"
