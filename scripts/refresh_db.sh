#!/usr/bin/env bash
#
# Refresh the local `upmovies` database from production, one schema group at a time.
#
#   content : catalog + news + ingest — films, stories, events, run history.
#             Local app/user data is left completely alone. This is the default.
#   app     : app only — users, sessions, invites. Local content is left alone.
#   both    : drop and restore all four schemas from prod.
#
# The split is the whole point: a content refresh is the routine operation (you want prod's
# ~1,400 films and ~99k stories to measure against), and it must never cost you the local
# admin account you log in with. `app` is opt-in and prompts, because it drops the schema
# holding your local users.
#
# Direction is one-way by construction: prod is only ever read (pg_dump, printenv, psql
# SELECTs), and every write goes through `docker exec "$LOCAL_PG_CONTAINER"` with no ssh
# in front of it.
#
# Usage:
#   scripts/refresh_db.sh [content|app|both]      # or: task db:refresh
#
# Configuration lives in the repo-root .env (gitignored) — see .env.example:
#   PROD_SSH             required, e.g. tom@ssh.neuroticsasquat.ch
#   PROD_PG_CONTAINER    optional; auto-discovered by looking for the `catalog` schema
#   PROD_PG_USER         optional; defaults to the container's POSTGRES_USER
#   PROD_PG_DB           optional; defaults to the container's POSTGRES_DB
#   LOCAL_PG_CONTAINER   optional; defaults to tbc_postgresql_db
#   LOCAL_DB             optional; defaults to upmovies
#   LOCAL_DB_USER        optional; defaults to root
#   FORCE                optional; set to 1 to skip the app-mode confirmation prompt

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." &>/dev/null && pwd)

ENV_FILE="$REPO_ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

MODE="${1:-content}"
case "$MODE" in
  content) SCHEMAS=(catalog news ingest) ;;
  app) SCHEMAS=(app) ;;
  both) SCHEMAS=(app catalog news ingest) ;;
  *)
    echo "usage: $(basename "$0") [content|app|both]" >&2
    exit 1
    ;;
esac

LOCAL_PG_CONTAINER="${LOCAL_PG_CONTAINER:-tbc_postgresql_db}"
LOCAL_DB="${LOCAL_DB:-upmovies}"
LOCAL_DB_USER="${LOCAL_DB_USER:-root}"
# The marker schema container discovery looks for. `catalog` rather than `app`, because
# `app` is a common enough schema name to collide on a host running several stacks.
MARKER_SCHEMA="catalog"

if [[ -z "${PROD_SSH:-}" ]]; then
  echo "ERROR: PROD_SSH is not set. Add it to $ENV_FILE (see .env.example)." >&2
  exit 1
fi

# A quoted, comma-separated SQL list of the schemas being restored, for the introspection
# queries below: 'catalog','news','ingest'
schema_sql_list() {
  local out=""
  for s in "${SCHEMAS[@]}"; do out+="${out:+,}'$s'"; done
  printf '%s' "$out"
}
SCHEMA_LIST=$(schema_sql_list)

local_psql() {
  docker exec -i "$LOCAL_PG_CONTAINER" \
    psql -v ON_ERROR_STOP=1 -U "$LOCAL_DB_USER" -d "$LOCAL_DB" "$@"
}

echo "→ Refreshing local '$LOCAL_DB' schema(s): ${SCHEMAS[*]}"

if ! docker inspect -f '{{.State.Running}}' "$LOCAL_PG_CONTAINER" 2>/dev/null | grep -q true; then
  echo "ERROR: local Postgres container '$LOCAL_PG_CONTAINER' is not running." >&2
  echo "  Start the shared infra first (cd ../../tbc-localdev-infra && task up)." >&2
  exit 1
fi

if [[ "$MODE" != "content" && "${FORCE:-0}" != "1" ]]; then
  echo
  echo "  This drops the local 'app' schema — every local user, session and invite,"
  echo "  including the admin account you log in with. Local content is unaffected"
  echo "  unless you chose 'both'."
  echo
  if [[ ! -t 0 ]]; then
    echo "ERROR: refusing to drop 'app' without a terminal. Re-run with FORCE=1." >&2
    exit 1
  fi
  read -r -p "  Type the mode ('$MODE') to continue: " reply
  [[ "$reply" == "$MODE" ]] || { echo "Aborted."; exit 1; }
fi

# --- Locate prod ------------------------------------------------------------------------
# Discovered by *content*, not by image tag: the host runs several Postgres containers
# behind Coolify, whose names are service UUIDs that change on redeploy, so neither a name
# nor an `ancestor=` filter identifies this app's database. The one holding a `catalog`
# schema does, and it keeps working after a redeploy.
if [[ -z "${PROD_PG_CONTAINER:-}" ]]; then
  echo "→ Locating the prod Postgres container on $PROD_SSH..."
  PROD_PG_CONTAINER=$(ssh "$PROD_SSH" bash -s <<REMOTE
set -euo pipefail
for c in \$(docker ps --filter ancestor=postgres --format '{{.Names}}'; \
            docker ps --format '{{.Names}}\t{{.Image}}' | awk '/postgres/ {print \$1}'); do
  u=\$(docker exec "\$c" printenv POSTGRES_USER 2>/dev/null || true)
  d=\$(docker exec "\$c" printenv POSTGRES_DB 2>/dev/null || true)
  [[ -n "\$u" ]] || continue
  if docker exec "\$c" psql -U "\$u" -d "\${d:-postgres}" -tAc \
       "SELECT 1 FROM pg_namespace WHERE nspname = '$MARKER_SCHEMA'" 2>/dev/null | grep -q 1; then
    echo "\$c"
    exit 0
  fi
done
exit 1
REMOTE
  ) || {
    echo "ERROR: no prod Postgres container has a '$MARKER_SCHEMA' schema." >&2
    echo "  Set PROD_PG_CONTAINER in $ENV_FILE to pin it by hand." >&2
    exit 1
  }
fi

PROD_PG_USER="${PROD_PG_USER:-$(ssh "$PROD_SSH" "docker exec $PROD_PG_CONTAINER printenv POSTGRES_USER")}"
# Coolify-managed Postgres usually leaves POSTGRES_DB at `postgres` and the app simply uses
# that database, its schemas created by Alembic on top. Overridable for the same reason.
PROD_PG_DB="${PROD_PG_DB:-$(ssh "$PROD_SSH" "docker exec $PROD_PG_CONTAINER printenv POSTGRES_DB")}"
echo "  prod container=$PROD_PG_CONTAINER user=$PROD_PG_USER db=$PROD_PG_DB"

prod_psql() {
  ssh "$PROD_SSH" "docker exec -i $PROD_PG_CONTAINER psql -U $PROD_PG_USER -d $PROD_PG_DB -tA" "$@"
}

# A dump from a newer major than the local server may use an archive format the local
# pg_restore cannot read. Same major today (both 17); this is here so the day they diverge
# fails with the reason rather than a parse error.
PROD_MAJOR=$(ssh "$PROD_SSH" "docker exec $PROD_PG_CONTAINER pg_dump --version" | grep -oE '[0-9]+' | head -1)
LOCAL_MAJOR=$(docker exec "$LOCAL_PG_CONTAINER" pg_restore --version | grep -oE '[0-9]+' | head -1)
if (( PROD_MAJOR > LOCAL_MAJOR )); then
  echo "ERROR: prod Postgres is $PROD_MAJOR, local is $LOCAL_MAJOR." >&2
  echo "  A newer pg_dump archive is not guaranteed readable by an older pg_restore." >&2
  echo "  Bump the shared local container (tbc-localdev-infra) to $PROD_MAJOR and retry." >&2
  exit 1
fi

# --- Foreign keys that cross the boundary of what we are restoring ------------------------
# Two directions, two different problems, both silent if ignored.
#
# Inbound: a constraint on a table *outside* the restored set that points *into* it. The
# DROP SCHEMA ... CASCADE below deletes it, and a schema-scoped dump does not bring it back.
# Snapshot the definitions now, replay them after the restore. Introspected rather than
# hardcoded, because a hardcoded list is exactly how such a constraint goes missing unnoticed
# the next time someone adds one.
echo "→ Snapshotting foreign keys that point into ${SCHEMAS[*]}..."
FK_RESTORE_SQL=$(local_psql -tA <<SQL
SELECT format('ALTER TABLE %I.%I ADD CONSTRAINT %I %s;',
              rn.nspname, rt.relname, c.conname, pg_get_constraintdef(c.oid))
FROM pg_constraint c
JOIN pg_class     rt ON rt.oid = c.conrelid
JOIN pg_namespace rn ON rn.oid = rt.relnamespace
JOIN pg_class     ft ON ft.oid = c.confrelid
JOIN pg_namespace fn ON fn.oid = ft.relnamespace
WHERE c.contype = 'f'
  AND fn.nspname IN ($SCHEMA_LIST)
  AND rn.nspname NOT IN ($SCHEMA_LIST)
ORDER BY rn.nspname, rt.relname, c.conname;
SQL
)

# Outbound: a restored table pointing *out* at a schema we are not restoring — today just
# `news.event_summary.edited_by -> app.user`, an admin who edited a summary. Prod's rows
# name prod's users, who do not exist in the local `app` schema we are deliberately
# preserving, so the constraint would fail to validate. The column is nullable with
# ON DELETE SET NULL, and severing it says exactly the true thing: that editor is not an
# account on this machine. Read from prod, since prod is where the dump's shape comes from.
ORPHAN_ROWS=$(prod_psql <<SQL
SELECT a.attnotnull || E'\t' ||
       format('UPDATE %I.%I SET %I = NULL WHERE %I IS NOT NULL AND %I NOT IN (SELECT %I FROM %I.%I);',
              rn.nspname, rt.relname, a.attname, a.attname, a.attname,
              fa.attname, fn.nspname, ft.relname)
FROM pg_constraint c
JOIN pg_class     rt ON rt.oid = c.conrelid
JOIN pg_namespace rn ON rn.oid = rt.relnamespace
JOIN pg_class     ft ON ft.oid = c.confrelid
JOIN pg_namespace fn ON fn.oid = ft.relnamespace
JOIN unnest(c.conkey)  WITH ORDINALITY AS k(attnum, ord)  ON true
JOIN unnest(c.confkey) WITH ORDINALITY AS fk(attnum, ord) ON fk.ord = k.ord
JOIN pg_attribute a  ON a.attrelid  = rt.oid AND a.attnum  = k.attnum
JOIN pg_attribute fa ON fa.attrelid = ft.oid AND fa.attnum = fk.attnum
WHERE c.contype = 'f'
  AND rn.nspname IN ($SCHEMA_LIST)
  AND fn.nspname NOT IN ($SCHEMA_LIST)
ORDER BY rn.nspname, rt.relname, a.attname;
SQL
)

SEVER_SQL=""
while IFS=$'\t' read -r notnull statement; do
  [[ -n "${statement:-}" ]] || continue
  if [[ "$notnull" == "t" ]]; then
    echo "ERROR: a NOT NULL column in ${SCHEMAS[*]} references a schema you are not" >&2
    echo "  restoring, so it cannot be severed:" >&2
    echo "    $statement" >&2
    echo "  Re-run with 'both' to restore the referenced schema as well." >&2
    exit 1
  fi
  SEVER_SQL+="$statement"$'\n'
done <<<"$ORPHAN_ROWS"

# --- Dump -------------------------------------------------------------------------------
DUMP_FLAGS=(--format=custom --no-owner --no-acl)
for s in "${SCHEMAS[@]}"; do DUMP_FLAGS+=("--schema=$s"); done

DUMP_FILE=$(mktemp -t upmovies-refresh.XXXXXX.dump)
trap 'rm -f "$DUMP_FILE"; docker exec -i "$LOCAL_PG_CONTAINER" rm -f /tmp/upmovies-refresh.dump 2>/dev/null || true' EXIT

echo "→ Dumping ${SCHEMAS[*]} from prod..."
ssh "$PROD_SSH" \
  "docker exec -i $PROD_PG_CONTAINER pg_dump ${DUMP_FLAGS[*]} -U $PROD_PG_USER $PROD_PG_DB" \
  >"$DUMP_FILE"
echo "  $(du -h "$DUMP_FILE" | cut -f1) dumped"

# --- Restore ----------------------------------------------------------------------------
DROP_SQL=""
for s in "${SCHEMAS[@]}"; do DROP_SQL+="DROP SCHEMA IF EXISTS $s CASCADE;"; done
echo "→ Dropping local ${SCHEMAS[*]}..."
local_psql -c "$DROP_SQL" >/dev/null

# pg_restore needs a seekable file for a custom archive, so the dump goes into the container
# rather than down a pipe.
docker cp "$DUMP_FILE" "$LOCAL_PG_CONTAINER:/tmp/upmovies-refresh.dump" >/dev/null

# Restored in sections so the orphan sweep can land between the rows arriving and the
# constraints being validated. A single-shot restore would try to validate the outbound
# foreign key against local users who do not exist, and fail.
restore_section() {
  docker exec -i "$LOCAL_PG_CONTAINER" pg_restore --no-owner --no-acl --exit-on-error \
    --section="$1" -U "$LOCAL_DB_USER" -d "$LOCAL_DB" /tmp/upmovies-refresh.dump
}
echo "→ Restoring schema definitions..."
restore_section pre-data
echo "→ Restoring rows..."
restore_section data

if [[ -n "$SEVER_SQL" ]]; then
  echo "→ Severing references to rows outside the restored schemas..."
  printf '%s' "$SEVER_SQL" | local_psql >/dev/null
fi

echo "→ Restoring indexes and constraints..."
restore_section post-data

if [[ -n "$FK_RESTORE_SQL" ]]; then
  echo "→ Re-adding foreign keys that point into ${SCHEMAS[*]}..."
  if ! printf '%s\n' "$FK_RESTORE_SQL" | local_psql >/dev/null; then
    echo "ERROR: could not re-add a foreign key after the restore." >&2
    echo "  A local row references a row prod no longer has. Delete the offending rows," >&2
    echo "  then re-add the constraint by hand:" >&2
    printf '%s\n' "$FK_RESTORE_SQL" | sed 's/^/    /' >&2
    exit 1
  fi
fi

# The Alembic version table lives in the `app` schema, so a `content` refresh leaves the
# local revision untouched and this applies whatever the branch has that prod does not.
echo "→ Applying any newer migrations from this branch..."
(cd "$REPO_ROOT" && task migrate)

echo "→ Done. Local '$LOCAL_DB' now holds:"
local_psql -tAc "
SELECT '  films='       || (SELECT count(*) FROM catalog.film)
    || ' stories='      || (SELECT count(*) FROM news.story)
    || ' events='       || (SELECT count(*) FROM news.event)
    || ' users='        || (SELECT count(*) FROM app.\"user\")"
