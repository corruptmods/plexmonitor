#!/bin/sh
set -e

CONFIG_DIR="/app/data"
DEFAULTS_DIR="/app/defaults"

mkdir -p "$CONFIG_DIR"

# Copy each missing default individually. The original only did this when the
# whole directory was empty, so a volume that already had config.json never
# received newly added files on upgrade.
for source in "$DEFAULTS_DIR"/*; do
    [ -e "$source" ] || continue
    target="$CONFIG_DIR/$(basename "$source")"
    if [ ! -e "$target" ]; then
        echo "Seeding default: $(basename "$source")"
        cp -r "$source" "$target"
    fi
done

# Run as PUID:PGID when they're set (recommended), handing data/ to that
# user first. Unset, the bot runs as root, as it always has -- data/ from an
# older install belongs to root, and changing that unasked could lock you
# out of editing it.
if [ -n "${PUID:-}" ] && [ "$(id -u)" = "0" ] && [ "$PUID" != "0" ]; then
    PGID="${PGID:-$PUID}"
    if chown -R "$PUID:$PGID" "$CONFIG_DIR"; then
        exec su-exec "$PUID:$PGID" "$@"
    fi
    echo "Could not hand $CONFIG_DIR to $PUID:$PGID -- running as root"
fi

exec "$@"
