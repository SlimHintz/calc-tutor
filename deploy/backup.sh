#!/bin/sh
# Nightly WAL-safe backup, keeps the last 14.
set -e
DB="${CALC_TUTOR_DB_PATH:-$HOME/calc-tutor-data/tutor.db}"
DIR="$(dirname "$DB")/backups"
mkdir -p "$DIR"
sqlite3 "$DB" ".backup '$DIR/tutor-$(date +%Y%m%d).db'"
ls -1t "$DIR"/tutor-*.db | tail -n +15 | xargs -r rm --
