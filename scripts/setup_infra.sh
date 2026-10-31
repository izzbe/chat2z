#!/usr/bin/env bash
# One-time infra for auth + persistence. Idempotent. Run by a human with owner rights:
#   - GCS bucket for artifact payloads (private, uniform access, public access prevented)
#   - bucket access for the runtime service account
#   - a restricted Postgres role `chat2z_app` that can only touch the chat2z schema
#   - Secret Manager secret `chat2z-database-url` holding that role's connection string
# Secret values go straight into Secret Manager; nothing sensitive is printed.
set -euo pipefail
P=meteo-512021
BUCKET=$P-chat2z-artifacts
RUN=chat2z-sa@$P.iam.gserviceaccount.com
SCHEMA=${DB_SCHEMA:-chat2z}

gcloud storage buckets describe gs://$BUCKET --project $P >/dev/null 2>&1 || \
  gcloud storage buckets create gs://$BUCKET --project $P --location us-central1 \
    --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member serviceAccount:$RUN \
  --role roles/storage.objectAdmin --project $P >/dev/null

ADMIN="$(gcloud secrets versions access latest --secret database-url --project $P)"
ADMIN="postgresql://${ADMIN#*://}"
# Supabase's pooler wants user.<project-ref>; derive the ref from the admin user
rest="${ADMIN#postgresql://}"; adminuser="${rest%%:*}"; tail="${rest#*@}"
ref="${adminuser#*.}"
[ "$ref" != "$adminuser" ] && suffix=".$ref" || suffix=""
PW="$(openssl rand -hex 24)"

psql "$ADMIN" -v ON_ERROR_STOP=1 -v pw="$PW" -v schema="$SCHEMA" -q <<'SQL'
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'chat2z_app') then
    create role chat2z_app login nosuperuser nocreatedb nocreaterole nobypassrls;
  end if;
end $$;
alter role chat2z_app with password :'pw';
grant usage on schema :"schema" to chat2z_app;
grant select, insert, update, delete on all tables in schema :"schema" to chat2z_app;
grant usage, select on all sequences in schema :"schema" to chat2z_app;
alter default privileges in schema :"schema" grant select, insert, update, delete on tables to chat2z_app;
alter default privileges in schema :"schema" grant usage, select on sequences to chat2z_app;
SQL

APPURL="postgresql://chat2z_app${suffix}:${PW}@${tail}"
gcloud secrets describe chat2z-database-url --project $P >/dev/null 2>&1 || \
  gcloud secrets create chat2z-database-url --replication-policy=automatic --project $P
printf '%s' "$APPURL" | gcloud secrets versions add chat2z-database-url --data-file=- --project $P >/dev/null
gcloud secrets add-iam-policy-binding chat2z-database-url --member serviceAccount:$RUN \
  --role roles/secretmanager.secretAccessor --project $P >/dev/null
echo "infra ready: bucket gs://$BUCKET, role chat2z_app, secret chat2z-database-url"
