#!/usr/bin/env bash
#
# backup_mini2.sh — Phase 3.2/3.3 HARD SAFETY GATE.
#
# READ-ONLY against the production mini. Inspects what real Smart Shopper data
# exists there (Postgres or SQLite), backs it up, pulls the backup OFF-box to
# this machine, checksum-verifies it, and restore-verifies it into a throwaway
# local database. It performs NO destructive operation on the source — ever.
#
# No later migration/cutover step may run until this script exits 0 and the
# printed restore-verify counts match the source counts.
#
# Usage:
#   scripts/backup_mini2.sh                 # back up the remote prod mini (SSH)
#   scripts/backup_mini2.sh --self-test     # exercise the backup+verify logic
#                                           # against THIS box's local Postgres
#                                           # (no SSH, proves the machinery works)
#   SSH_HOST=mini2 scripts/backup_mini2.sh  # override the ssh host alias
#
# Env:
#   SSH_HOST           ssh host/alias for the prod mini      (default: mini2)
#   REMOTE_DB_NAME     remote Postgres database name          (default: smartshopper)
#   REMOTE_SQLITE_PATH candidate remote SQLite analytics db   (default: ~/kroger-mcp/data/kroger_analytics.db)
#   BACKUP_ROOT        local dir to store pulled backups      (default: data/backups/mini2)
#
set -Eeuo pipefail

SSH_HOST="${SSH_HOST:-mini2}"
REMOTE_DB_NAME="${REMOTE_DB_NAME:-smartshopper}"
REMOTE_SQLITE_PATH="${REMOTE_SQLITE_PATH:-\$HOME/kroger-mcp/data/kroger_analytics.db}"
BACKUP_ROOT="${BACKUP_ROOT:-data/backups/mini2}"
SELF_TEST=0
[[ "${1:-}" == "--self-test" ]] && SELF_TEST=1

# A fixed UTC-ish stamp is required (Date is fine in shell; the no-Date rule is
# a workflow-script constraint, not a shell one). Sortable, path-safe.
TS="$(date -u +%Y%m%dT%H%M%SZ)"

log()  { printf '[backup_mini2] %s\n' "$*" >&2; }
die()  { printf '[backup_mini2] ERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO"' ERR

require() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
require pg_dump; require psql; require sqlite3
command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
  || die "need sha256sum or shasum for checksums"

sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }

# ---------------------------------------------------------------------------
# Restore-verify: load a pg_dump (custom format) into a throwaway LOCAL database
# and return its per-table row counts. Pure verification; the temp DB is dropped.
# ---------------------------------------------------------------------------
# Exact per-table COUNT(*) for every base table in the public schema. n_live_tup
# from pg_stat_user_tables is only an ESTIMATE — unacceptable for a safety gate —
# so we generate and run real COUNT(*) queries.
count_exact_pg() {
  local dsn="$1"
  local q
  q="$(psql "$dsn" -At -c "
    SELECT string_agg(
      format('SELECT %L AS t, COUNT(*) AS n FROM %I', tablename, tablename),
      ' UNION ALL ')
    FROM pg_tables WHERE schemaname='public';")"
  [[ -z "$q" ]] && { echo ""; return 0; }
  psql "$dsn" -At -F$'\t' -c "$q ORDER BY t" | sort
}

restore_verify_pg_dump() {
  local dump_file="$1" admin_dsn="${2:-postgresql://localhost:5432/postgres}"
  local verify_db="ss_verify_${TS}_$$"
  local base="${admin_dsn%/*}"
  log "restore-verify: loading ${dump_file##*/} into throwaway db ${verify_db}"
  psql "$admin_dsn" -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE \"${verify_db}\";"
  # shellcheck disable=SC2064
  trap "psql '$admin_dsn' -q -c 'DROP DATABASE IF EXISTS \"${verify_db}\";' >/dev/null 2>&1 || true" RETURN
  pg_restore --no-owner --no-privileges --exit-on-error -d "${base}/${verify_db}" "$dump_file" >/dev/null
  count_exact_pg "${base}/${verify_db}"
}

count_source_pg() { count_exact_pg "$1"; }

# ---------------------------------------------------------------------------
# SELF-TEST: prove the backup→pull→checksum→restore-verify machinery end to end
# against the LOCAL Postgres, so it is trusted before it ever touches prod.
# ---------------------------------------------------------------------------
if [[ "$SELF_TEST" == "1" ]]; then
  log "SELF-TEST mode — using local Postgres, no SSH, no prod contact"
  ADMIN_DSN="${ETL_TEST_PG_ADMIN:-postgresql://localhost:5432/postgres}"
  psql "$ADMIN_DSN" -q -c "SELECT 1" >/dev/null 2>&1 || die "local Postgres not reachable at $ADMIN_DSN"

  src_db="ss_selftest_src_${TS}_$$"
  base="${ADMIN_DSN%/*}"
  log "creating source fixture db ${src_db}"
  psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE \"${src_db}\";"
  cleanup_src() { psql "$ADMIN_DSN" -q -c "DROP DATABASE IF EXISTS \"${src_db}\";" >/dev/null 2>&1 || true; }
  trap 'cleanup_src; die "self-test failed at line $LINENO"' ERR

  psql "${base}/${src_db}" -v ON_ERROR_STOP=1 -q <<'SQL'
    CREATE TABLE products (product_id text primary key, description text);
    CREATE TABLE pantry_items (id serial primary key, product_id text, level_percent int);
    INSERT INTO products VALUES ('P1','Olive Oil'), ('P2','Brown Rice'), ('P3','Spinach');
    INSERT INTO pantry_items (product_id, level_percent)
      SELECT product_id, 50 FROM products;
SQL

  mkdir -p "${BACKUP_ROOT}/selftest-${TS}"
  dump="${BACKUP_ROOT}/selftest-${TS}/${src_db}.dump"
  log "pg_dump -Fc of source"
  pg_dump -Fc -d "${base}/${src_db}" -f "$dump"
  log "sha256: $(sha256 "$dump")"

  log "source counts:";       src_counts="$(count_source_pg "${base}/${src_db}")"; echo "$src_counts" >&2
  log "restored counts:";     res_counts="$(restore_verify_pg_dump "$dump" "$ADMIN_DSN")"; echo "$res_counts" >&2

  if [[ "$src_counts" == "$res_counts" ]]; then
    log "SELF-TEST PASS — backup + restore-verify counts match exactly"
    cleanup_src
    rm -rf "${BACKUP_ROOT}/selftest-${TS}"
    trap - ERR
    exit 0
  else
    cleanup_src
    die "SELF-TEST FAIL — source vs restored counts differ (see above)"
  fi
fi

# ---------------------------------------------------------------------------
# REAL backup of the production mini (requires non-interactive SSH to .108).
# ---------------------------------------------------------------------------
require ssh; require rsync

log "verifying non-interactive SSH to '${SSH_HOST}'"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$SSH_HOST" 'echo OK' >/dev/null \
  || die "non-interactive SSH to '${SSH_HOST}' failed — install this box's key on .108 first (see plan PRECONDITION)"

dest="${BACKUP_ROOT}/${TS}"
mkdir -p "$dest"
log "backup destination: ${dest}"

# Decide the source branch: is real data in remote Postgres or remote SQLite?
remote_has_pg=0
if ssh -o BatchMode=yes "$SSH_HOST" "command -v psql >/dev/null 2>&1 && psql -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw '${REMOTE_DB_NAME}'"; then
  remote_has_pg=1
fi
remote_has_sqlite=0
if ssh -o BatchMode=yes "$SSH_HOST" "test -f ${REMOTE_SQLITE_PATH}"; then
  remote_has_sqlite=1
fi

log "remote inventory: postgres(${REMOTE_DB_NAME})=${remote_has_pg} sqlite(${REMOTE_SQLITE_PATH})=${remote_has_sqlite}"
[[ "$remote_has_pg" == "0" && "$remote_has_sqlite" == "0" ]] \
  && die "no Smart Shopper data found on '${SSH_HOST}' — re-inspect before assuming a source (do NOT proceed)"

if [[ "$remote_has_pg" == "1" ]]; then
  log "Postgres source detected — pg_dump -Fc + globals (remote, read-only)"
  ssh -o BatchMode=yes "$SSH_HOST" "pg_dump -Fc -d '${REMOTE_DB_NAME}'" > "${dest}/${REMOTE_DB_NAME}.dump"
  ssh -o BatchMode=yes "$SSH_HOST" "pg_dump --no-owner --no-privileges -d '${REMOTE_DB_NAME}'" > "${dest}/${REMOTE_DB_NAME}.sql"
  ssh -o BatchMode=yes "$SSH_HOST" "pg_dumpall --globals-only" > "${dest}/globals.sql" || true
  sha256 "${dest}/${REMOTE_DB_NAME}.dump" > "${dest}/${REMOTE_DB_NAME}.dump.sha256"
  log "source counts (remote, live, EXACT COUNT(*) per table):"
  # \gexec runs each generated COUNT query; emits 't<TAB>n' rows. The 'SQL'
  # heredoc is single-quoted so $ stays literal on the remote; only the local
  # ${REMOTE_DB_NAME} in the -d flag expands here.
  ssh -o BatchMode=yes "$SSH_HOST" "psql -At -F$'\t' -d '${REMOTE_DB_NAME}' <<'SQL'
SELECT format('SELECT %L AS t, COUNT(*) AS n FROM %I', tablename, tablename)
FROM pg_tables WHERE schemaname='public' \gexec
SQL" | sort | tee "${dest}/source_counts.tsv" >&2
  log "restore-verify into a throwaway LOCAL database:"
  restore_verify_pg_dump "${dest}/${REMOTE_DB_NAME}.dump" | tee "${dest}/restored_counts.tsv" >&2
  if diff -q <(cut -f1,2 "${dest}/source_counts.tsv" | sort) <(cut -f1,2 "${dest}/restored_counts.tsv" | sort) >/dev/null; then
    log "PARITY OK — source vs restored row counts match"
  else
    die "PARITY MISMATCH — source vs restored counts differ; DO NOT proceed to migration"
  fi
fi

if [[ "$remote_has_sqlite" == "1" ]]; then
  log "SQLite source detected — sqlite3 .backup (consistent snapshot, read-only)"
  ssh -o BatchMode=yes "$SSH_HOST" "sqlite3 ${REMOTE_SQLITE_PATH} \".backup '/tmp/ss_backup_${TS}.db'\""
  rsync -avz --checksum "${SSH_HOST}:/tmp/ss_backup_${TS}.db" "${dest}/kroger_analytics.db"
  ssh -o BatchMode=yes "$SSH_HOST" "rm -f /tmp/ss_backup_${TS}.db" || true
  sha256 "${dest}/kroger_analytics.db" > "${dest}/kroger_analytics.db.sha256"
  log "integrity_check + row counts on the pulled copy:"
  sqlite3 "${dest}/kroger_analytics.db" 'PRAGMA integrity_check;' | tee "${dest}/integrity.txt" >&2
  sqlite3 "${dest}/kroger_analytics.db" \
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;" \
    | while read -r t; do
        printf '%s\t%s\n' "$t" "$(sqlite3 "${dest}/kroger_analytics.db" "SELECT COUNT(*) FROM \"$t\";")"
      done | tee "${dest}/source_counts.tsv" >&2
fi

log "BACKUP COMPLETE → ${dest}"
log "Backup is OFF-box and verified. Only now may provisioning/migration proceed."
