#!/usr/bin/env bash
# Optional Claude helpers. Sourced by install.sh after core MCP registration.

HELPER_ROOT="$CONFIG_DIR/claude-helpers"
HELPER_TRANSACTION=0
HELPER_TX_QUARANTINE=""
HELPER_TX_QUARANTINED=""
HELPER_TX_DST=""
HELPER_TX_TARGET=""

managed_helper_target() {
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

published_copy_is_owned() {
    $PYTHON_CMD "$PATH_GUARD" helper-published "$1" "$2" \
        >/dev/null 2>&1
}

asset_is_owned() {
    local label="$1" src="$2" dst="$3" target=""
    if [ -L "$dst" ]; then
        target="$(readlink "$dst" 2>/dev/null || true)"
        [ "$target" = "$src" ] || managed_helper_target "$label" "$target"
    else
        $PYTHON_CMD "$PATH_GUARD" helper-current "$label" "$src" "$dst" \
            >/dev/null 2>&1 || published_copy_is_owned "$label" "$dst"
    fi
}

restore_quarantined_asset() {
    local quarantined="$1" dst="$2"
    mv -n -- "$quarantined" "$dst" 2>/dev/null || true
    if [ -e "$quarantined" ] || [ -L "$quarantined" ]; then
        echo "warning: the raced asset was preserved at $quarantined; nothing was deleted." >&2
        return 1
    fi
}

clear_helper_transaction() {
    HELPER_TRANSACTION=0
    HELPER_TX_QUARANTINE=""
    HELPER_TX_QUARANTINED=""
    HELPER_TX_DST=""
    HELPER_TX_TARGET=""
}

rollback_helper_transaction() {
    local failed=0
    [ "$HELPER_TRANSACTION" -eq 1 ] || return 0
    if [ -L "$HELPER_TX_DST" ] \
        && [ "$(readlink "$HELPER_TX_DST" 2>/dev/null || true)" = "$HELPER_TX_TARGET" ]; then
        rm -- "$HELPER_TX_DST" 2>/dev/null || failed=1
    elif [ -e "$HELPER_TX_DST" ] || [ -L "$HELPER_TX_DST" ]; then
        [ ! -e "$HELPER_TX_QUARANTINED" ] \
            && [ ! -L "$HELPER_TX_QUARANTINED" ] || failed=1
    fi
    if [ "$failed" -eq 0 ] \
        && { [ -e "$HELPER_TX_QUARANTINED" ] || [ -L "$HELPER_TX_QUARANTINED" ]; }; then
        restore_quarantined_asset "$HELPER_TX_QUARANTINED" "$HELPER_TX_DST" \
            || failed=1
    fi
    if [ "$failed" -eq 0 ]; then
        rmdir "$HELPER_TX_QUARANTINE" 2>/dev/null || true
        clear_helper_transaction
    else
        echo "warning: helper publish recovery is incomplete; the old asset is preserved at $HELPER_TX_QUARANTINE" >&2
    fi
    return "$failed"
}

begin_helper_transaction() {
    [ "$HELPER_TRANSACTION" -eq 0 ] || return 1
    HELPER_TX_QUARANTINE="$1"
    HELPER_TX_QUARANTINED="$2"
    HELPER_TX_DST="$3"
    HELPER_TX_TARGET="$4"
    HELPER_TRANSACTION=1
}

commit_helper_transaction() {
    local quarantine="$HELPER_TX_QUARANTINE"
    clear_helper_transaction
    rm -rf -- "$quarantine" 2>/dev/null \
        || echo "warning: failed to clean up the old helper quarantine: $quarantine" >&2
}

publish_helper_link() {
    local label="$1" src="$2" target="$3" dst="$4" quarantine quarantined
    if [ ! -e "$dst" ] && [ ! -L "$dst" ]; then
        ln -s "$target" "$dst" 2>/dev/null && return 0
        echo "warning: $dst raced or the platform does not support a safe symlink; the original state was preserved." >&2
        return 1
    fi
    if ! asset_is_owned "$label" "$src" "$dst"; then
        echo "warning: $dst already exists and is not owned by this installer; it was preserved." >&2
        return 1
    fi
    quarantine="$(mktemp -d "${dst}.deepseek-mcp.XXXXXX")" || return 1
    quarantined="$quarantine/asset"
    if ! begin_helper_transaction "$quarantine" "$quarantined" "$dst" "$target"; then
        rmdir "$quarantine" 2>/dev/null || true
        return 1
    fi
    if ! mv -n -- "$dst" "$quarantined" 2>/dev/null \
        || [ -e "$dst" ] || [ -L "$dst" ]; then
        rollback_helper_transaction || true
        echo "warning: $dst changed during the helper switch; it was preserved." >&2
        return 1
    fi
    if ! asset_is_owned "$label" "$src" "$quarantined"; then
        rollback_helper_transaction || true
        echo "warning: $dst ownership changed during the switch; overwrite refused." >&2
        return 1
    fi
    if ! ln -s "$target" "$dst" 2>/dev/null; then
        rollback_helper_transaction || true
        echo "warning: could not safely publish $dst; the old asset was restored." >&2
        return 1
    fi
    commit_helper_transaction
    return 0
}

publish_helper_file() {
    local label="$1" src="$2" target="$3" dst="$4" quarantine quarantined staged
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        if ! asset_is_owned "$label" "$src" "$dst"; then
            echo "warning: $dst already exists and is not owned by this installer; it was preserved." >&2
            return 1
        fi
        quarantine="$(mktemp -d "${dst}.deepseek-mcp.XXXXXX")" || return 1
        quarantined="$quarantine/asset"
        if ! begin_helper_transaction "$quarantine" "$quarantined" "$dst" "$target"; then
            rmdir "$quarantine" 2>/dev/null || true
            return 1
        fi
        if ! mv -n -- "$dst" "$quarantined" 2>/dev/null \
            || [ -e "$dst" ] || [ -L "$dst" ]; then
            rollback_helper_transaction || true
            echo "warning: $dst changed during the helper switch; it was preserved." >&2
            return 1
        fi
        if ! asset_is_owned "$label" "$src" "$quarantined"; then
            rollback_helper_transaction || true
            echo "warning: $dst ownership changed during the switch; overwrite refused." >&2
            return 1
        fi
    fi
    staged="$dst.deepseek-mcp.$$"
    if ! ( umask 077 && cp -- "$target" "$staged" ) 2>/dev/null \
        || ! cmp -s -- "$target" "$staged" 2>/dev/null \
        || ! mv -n -- "$staged" "$dst" 2>/dev/null \
        || [ -e "$staged" ] || [ -L "$staged" ] \
        || [ ! -f "$dst" ] || [ -L "$dst" ] \
        || ! cmp -s -- "$target" "$dst" 2>/dev/null; then
        rm -f -- "$staged" 2>/dev/null || true
        rollback_helper_transaction || true
        echo "warning: could not safely publish $dst; the original state was preserved." >&2
        return 1
    fi
    if [ "$HELPER_TRANSACTION" -eq 1 ]; then
        commit_helper_transaction
    fi
    return 0
}

cleanup_helper_generation() {
    [ -n "${HELPER_GENERATION:-}" ] || return 0
    if [ -e "$HELPER_GENERATION" ] \
        && ! $PYTHON_CMD "$PATH_GUARD" delete-generation \
            "$HELPER_ROOT" "$HELPER_GENERATION"; then
        echo "warning: could not clean up the incomplete helper generation: $HELPER_GENERATION" >&2
        return 1
    fi
    HELPER_GENERATION=""
}

stage_helper_generation() {
    local name
    name="${GENERATION_DIR##*/}"
    HELPER_GENERATION="$HELPER_ROOT/$name"
    if ! $PYTHON_CMD "$PATH_GUARD" prepare-dirs "$HELPER_ROOT" \
        || ! mkdir "$HELPER_GENERATION" \
        || ! chmod 700 "$HELPER_GENERATION" \
        || ! mkdir "$HELPER_GENERATION/skill" \
        || ! cp "$PROJECT_ROOT/skills/delegate-to-deepseek/SKILL.md" \
            "$HELPER_GENERATION/skill/SKILL.md" \
        || ! cmp -s "$PROJECT_ROOT/skills/delegate-to-deepseek/SKILL.md" \
            "$HELPER_GENERATION/skill/SKILL.md" \
        || ! cp "$PROJECT_ROOT/commands/ds.md" "$HELPER_GENERATION/ds.md" \
        || ! cmp -s "$PROJECT_ROOT/commands/ds.md" "$HELPER_GENERATION/ds.md" \
        || ! chmod 700 "$HELPER_GENERATION/skill" \
        || ! chmod 600 "$HELPER_GENERATION/skill/SKILL.md" \
            "$HELPER_GENERATION/ds.md"; then
        cleanup_helper_generation || true
        return 1
    fi
}

prepare_helper_directory() {
    local label="$1" relative="$2"
    if $PYTHON_CMD "$PATH_GUARD" prepare-private-dirs "$HOME" \
        ".claude" "$relative" >/dev/null 2>&1; then
        return 0
    fi
    echo "warning: could not secure the Claude $label path $HOME/$relative; this helper was not deployed." >&2
    $PYTHON_CMD "$PATH_GUARD" advise-private-dirs "$HOME" ".claude" "$relative" >&2 || true
    return 1
}

deploy_claude_helpers() {
    HELPER_WARNINGS=0
    echo "[helper] Deploying protected skill and slash command copies..."
    if [ -L "$PROJECT_ROOT/skills/delegate-to-deepseek/SKILL.md" ] \
        || [ -L "$PROJECT_ROOT/commands/ds.md" ]; then
        echo "warning: a helper source asset is a symlink; nothing was deployed." >&2
        HELPER_WARNINGS=1
        return 0
    fi
    SKILL_READY=0 COMMAND_READY=0
    if prepare_helper_directory "skill" ".claude/skills"; then
        SKILL_READY=1
    else
        HELPER_WARNINGS=1
    fi
    if prepare_helper_directory "command" ".claude/commands"; then
        COMMAND_READY=1
    else
        HELPER_WARNINGS=1
    fi
    if [ "$SKILL_READY" -ne 1 ] && [ "$COMMAND_READY" -ne 1 ]; then
        echo "warning: none of the Claude helper paths could be secured; check the guidance above." >&2
        return 0
    fi
    if ! stage_helper_generation; then
        echo "warning: could not safely create the Claude helper generation; the core MCP is still usable." >&2
        HELPER_WARNINGS=1
        return 0
    fi
    SKILL_PUBLISHED=0 COMMAND_PUBLISHED=0
    if [ "$SKILL_READY" -eq 1 ]; then
        if publish_helper_link skill "$PROJECT_ROOT/skills/delegate-to-deepseek" \
            "$HELPER_GENERATION/skill" "$CLAUDE_SKILLS/delegate-to-deepseek"; then
            SKILL_PUBLISHED=1
        else
            HELPER_WARNINGS=1
            [ "$HELPER_TRANSACTION" -eq 0 ] || return 0
        fi
    fi
    if [ "$COMMAND_READY" -eq 1 ]; then
        if publish_helper_file command "$PROJECT_ROOT/commands/ds.md" \
            "$HELPER_GENERATION/ds.md" "$CLAUDE_COMMANDS/ds.md"; then
            COMMAND_PUBLISHED=1
        else
            HELPER_WARNINGS=1
            [ "$HELPER_TRANSACTION" -eq 0 ] || return 0
        fi
    fi
    if [ "$SKILL_READY" -ne 1 ] || [ "$COMMAND_READY" -ne 1 ] \
        || [ "$SKILL_PUBLISHED" -ne 1 ] || [ "$COMMAND_PUBLISHED" -ne 1 ]; then
        echo "warning: helper switch did not complete; keeping all generations to avoid breaking old references." >&2
    elif ! $PYTHON_CMD "$PATH_GUARD" prune-generations \
        "$HELPER_ROOT" "$HELPER_GENERATION"; then
        echo "warning: failed to clean up the old helper generation; it was preserved." >&2
        HELPER_WARNINGS=1
    fi
    return 0
}
