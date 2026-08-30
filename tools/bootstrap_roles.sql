-- Cluster/database role bootstrap for Blocks Service.
--
-- NOT a schema migration. This is an OPERATOR/ORCHESTRATOR concern: it needs a
-- superuser, it is cluster-scoped, and it runs once per environment before any
-- migration. It is reproduced here so a developer seat can stand the database up
-- locally and so the blocks-deployment contract has something concrete to point at.
--
-- Three roles, because forced row-level security is only meaningful when the
-- runtime role can neither own the tables nor bypass the policies:
--
--   blocks_owner     NOLOGIN. Owns the schema and every object in it.
--   blocks_migrator  LOGIN, member of blocks_owner. Runs migrations, SET ROLE
--                    blocks_owner so new objects belong to the owner, not to it.
--   blocks_app       LOGIN, NOSUPERUSER, NOBYPASSRLS. DML only. This is the role
--                    the service runs as, and it is subject to every policy.
--
-- Passwords are supplied by the caller via psql variables; none are baked in.
--   psql -v owner_pw=... -v migrator_pw=... -v app_pw=... -f tools/bootstrap_roles.sql

\set ON_ERROR_STOP on

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'blocks_owner') then
    create role blocks_owner nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'blocks_migrator') then
    create role blocks_migrator login in role blocks_owner;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'blocks_app') then
    create role blocks_app login nosuperuser nobypassrls noinherit;
  end if;
end
$$;

-- Re-assert the properties every run: a role that drifted to BYPASSRLS would
-- silently disable tenant isolation, so this is not a create-time-only concern.
alter role blocks_app nosuperuser nobypassrls nocreatedb nocreaterole;
alter role blocks_owner nosuperuser nobypassrls;

alter role blocks_migrator password :'migrator_pw';
alter role blocks_app      password :'app_pw';

grant connect on database :"db" to blocks_owner, blocks_migrator, blocks_app;

-- blocks_owner creates the schema, so it needs CREATE on the database. blocks_app
-- pointedly does not: the runtime role cannot introduce an object that would sit
-- outside the policies.
grant create on database :"db" to blocks_owner;
