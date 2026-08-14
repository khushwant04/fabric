-- Application database role for the Fabric control plane.
--
-- Row-level security is ignored by superusers and by any role with BYPASSRLS, and
-- table owners ignore it unless the table is FORCE'd. Managed PostgreSQL commonly
-- hands out an administrative role that has BYPASSRLS: Neon's default owner does,
-- so connecting the application as that role makes every policy decorative while
-- pg_class still reports them as enabled and forced. That is a silent failure, which
-- is why the control plane checks its own role at startup.
--
-- Run this once per database as the administrative role, then point
-- FABRIC_DATABASE_URL at fabric_app.
--
--   psql "$ADMIN_URL" -v password="'choose-a-strong-password'" \
--        -f control-plane/scripts/create-app-role.sql

BEGIN;

-- NOSUPERUSER and NOBYPASSRLS are the point of this role. NOCREATEROLE and
-- NOCREATEDB keep it from escalating.
CREATE ROLE fabric_app WITH
    LOGIN
    PASSWORD :password
    NOSUPERUSER
    NOBYPASSRLS
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT;

GRANT USAGE ON SCHEMA public TO fabric_app;

-- The application runs its own migrations, so it needs to create and alter its
-- tables. It owns what it creates, which policies still apply to because every
-- table is created with FORCE ROW LEVEL SECURITY.
GRANT CREATE ON SCHEMA public TO fabric_app;

-- Existing objects, for a database whose schema was created by another role.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fabric_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fabric_app;

-- And for objects a future migration adds.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fabric_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO fabric_app;

COMMIT;

-- Verify: this must report false, or the policies will not be enforced.
SELECT rolname, rolsuper, rolbypassrls
FROM pg_roles
WHERE rolname = 'fabric_app';
