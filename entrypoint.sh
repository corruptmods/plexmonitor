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

exec "$@"
