#!/usr/bin/env bash
#
# provision_prod.sh — Phase 3.4. Provision PostgreSQL 16 + Redis on the prod
# mini via Homebrew, tuned for an 8GB box, bound to localhost only, secured, and
# set to autostart. Idempotent: re-running converges, never re-initdbs over data.
#
# Runs LOCALLY on the mini (copy it over and execute there), OR remotely:
#   ssh prod 'bash -s' < scripts/provision_prod.sh
#
# It NEVER touches application data and NEVER runs before backup_prod.sh has
# produced a verified off-box backup — guard your runbook accordingly.
#
# Env (override as needed):
#   PG_PORT        Postgres port                       (default: 5433 — avoids a pre-existing pg14 on 5432)
#   APP_DB         application database name            (default: smartshopper)
#   APP_ROLE       application role                     (default: smartshopper_app)
#   REDIS_MAXMEM   redis maxmemory                       (default: 512mb)
#
# Passwords are NOT generated here. Set them in the macOS Keychain on the mini
# (see docs/cutover-runbook.md, step 7) and reference them from the per-box .env.
#
set -Eeuo pipefail

PG_PORT="${PG_PORT:-5433}"
APP_DB="${APP_DB:-smartshopper}"
APP_ROLE="${APP_ROLE:-smartshopper_app}"
REDIS_MAXMEM="${REDIS_MAXMEM:-512mb}"
PG_FORMULA="postgresql@16"
# Memory tuning — env-overridable so a RAM-tight box doesn't over-allocate.
# Defaults suit a dedicated box; on the SHARED 8GB prod mini pass small values
# (e.g. PG_SHARED_BUFFERS=192MB PG_EFFECTIVE_CACHE=512MB) to avoid swap pressure.
PG_SHARED_BUFFERS="${PG_SHARED_BUFFERS:-1GB}"
PG_EFFECTIVE_CACHE="${PG_EFFECTIVE_CACHE:-3GB}"
PG_WORK_MEM="${PG_WORK_MEM:-16MB}"
PG_MAINT_MEM="${PG_MAINT_MEM:-256MB}"
PG_MAX_CONN="${PG_MAX_CONN:-50}"

log() { printf '[provision_prod] %s\n' "$*" >&2; }
die() { printf '[provision_prod] ERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO"' ERR

command -v brew >/dev/null 2>&1 || die "Homebrew not found — install it first"

# ---------------------------------------------------------------------------
# 1. Install (idempotent — brew install is a no-op if already present).
# ---------------------------------------------------------------------------
log "installing ${PG_FORMULA} + redis (no-op if present)"
brew list "${PG_FORMULA}" >/dev/null 2>&1 || brew install "${PG_FORMULA}"
brew list redis >/dev/null 2>&1 || brew install redis

PG_PREFIX="$(brew --prefix "${PG_FORMULA}")"
PG_DATA="$(brew --prefix)/var/${PG_FORMULA}"
PG_CONF="${PG_DATA}/postgresql.conf"
PG_HBA="${PG_DATA}/pg_hba.conf"
export PATH="${PG_PREFIX}/bin:${PATH}"

# Never initdb over an existing data dir — that would destroy data.
if [[ ! -d "${PG_DATA}" || ! -f "${PG_CONF}" ]]; then
  log "initializing fresh PG cluster at ${PG_DATA}"
  initdb --auth-host=scram-sha-256 --auth-local=scram-sha-256 -D "${PG_DATA}"
else
  log "existing PG data dir found at ${PG_DATA} — NOT re-initializing (data-safe)"
fi

# ---------------------------------------------------------------------------
# 2. Tune for 8GB + localhost-only (idempotent: rewrite our managed block).
# ---------------------------------------------------------------------------
log "applying 8GB tuning + localhost binding to ${PG_CONF}"
MARK_BEGIN="# >>> smartshopper managed >>>"
MARK_END="# <<< smartshopper managed <<<"
# Strip any prior managed block, then append the current one.
if grep -qF "${MARK_BEGIN}" "${PG_CONF}"; then
  /usr/bin/sed -i '' "/${MARK_BEGIN}/,/${MARK_END}/d" "${PG_CONF}"
fi
cat >> "${PG_CONF}" <<EOF
${MARK_BEGIN}
listen_addresses = 'localhost'
port = ${PG_PORT}
shared_buffers = ${PG_SHARED_BUFFERS}
effective_cache_size = ${PG_EFFECTIVE_CACHE}
work_mem = ${PG_WORK_MEM}
maintenance_work_mem = ${PG_MAINT_MEM}
max_connections = ${PG_MAX_CONN}
wal_compression = on
${MARK_END}
EOF

# Local Unix socket uses peer (OS-authenticated: the macmini1 superuser bootstraps
# the role/DB passwordless). TCP loopback requires scram-sha-256 (the app role's
# password). Anything non-loopback is refused outright.
log "hardening ${PG_HBA} (socket=peer admin, loopback TCP=scram-sha-256)"
cat > "${PG_HBA}" <<'EOF'
# Managed by provision_prod.sh — socket=peer (admin), loopback TCP=scram.
local   all   all                  peer
host    all   all   127.0.0.1/32   scram-sha-256
host    all   all   ::1/128        scram-sha-256
EOF

# ---------------------------------------------------------------------------
# 3. Redis: localhost bind, password (from env at runtime), LRU, AOF.
# ---------------------------------------------------------------------------
REDIS_CONF="$(brew --prefix)/etc/redis.conf"
log "configuring redis at ${REDIS_CONF}"
[[ -f "${REDIS_CONF}" ]] || die "redis.conf not found at ${REDIS_CONF}"
RBEGIN="# >>> smartshopper managed >>>"
REND="# <<< smartshopper managed <<<"
if grep -qF "${RBEGIN}" "${REDIS_CONF}"; then
  /usr/bin/sed -i '' "/${RBEGIN}/,/${REND}/d" "${REDIS_CONF}"
fi
cat >> "${REDIS_CONF}" <<EOF
${RBEGIN}
bind 127.0.0.1 ::1
protected-mode yes
maxmemory ${REDIS_MAXMEM}
maxmemory-policy allkeys-lru
appendonly yes
# requirepass: set via the per-box .env / Keychain, NOT committed here.
${REND}
EOF

# ---------------------------------------------------------------------------
# 4. Autostart via brew services, then wait for readiness.
# ---------------------------------------------------------------------------
log "starting + enabling autostart (brew services)"
brew services restart "${PG_FORMULA}"
brew services restart redis

for _ in $(seq 1 30); do
  if pg_isready -h localhost -p "${PG_PORT}" >/dev/null 2>&1; then break; fi
  sleep 1
done
pg_isready -h localhost -p "${PG_PORT}" >/dev/null 2>&1 || die "Postgres did not become ready on :${PG_PORT}"

# ---------------------------------------------------------------------------
# 5. Create the app role + database if missing (idempotent). Password is set
#    separately (ALTER ROLE ... PASSWORD) from the Keychain-sourced secret.
# ---------------------------------------------------------------------------
log "ensuring role ${APP_ROLE} + database ${APP_DB} exist"
psql -p "${PG_PORT}" -d postgres -tAc \
  "SELECT 1 FROM pg_roles WHERE rolname='${APP_ROLE}'" | grep -q 1 \
  || psql -p "${PG_PORT}" -d postgres -c "CREATE ROLE ${APP_ROLE} LOGIN;"
psql -p "${PG_PORT}" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='${APP_DB}'" | grep -q 1 \
  || psql -p "${PG_PORT}" -d postgres -c "CREATE DATABASE ${APP_DB} OWNER ${APP_ROLE};"

log "PROVISION COMPLETE — PG16 on :${PG_PORT} (localhost), redis (localhost), both autostart"
log "NEXT: set ${APP_ROLE}'s password + redis requirepass from the Keychain, then run the ETL."
