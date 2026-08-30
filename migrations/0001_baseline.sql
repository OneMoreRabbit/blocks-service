-- 0001 baseline — identity plane, tenancy, membership authorization, and the
-- first tenant-scoped domain table under forced row-level security.

create schema if not exists blocks authorization blocks_owner;

-- ---------------------------------------------------------------------------
-- Transaction-local request context.
--
-- Both helpers read a custom GUC set with SET LOCAL, so the context lives and
-- dies with the transaction and can never outlive a pooled connection. Both
-- return NULL when unset, and every policy below compares against them with
-- `=`, so an unset context yields NULL, not true: no context means no rows.
-- Isolation therefore fails closed if the application forgets to scope a
-- transaction, rather than falling open to the whole table.
-- ---------------------------------------------------------------------------

create function blocks.current_tenant() returns uuid
  language sql stable
  as $$ select nullif(current_setting('blocks.tenant_id', true), '')::uuid $$;

create function blocks.current_actor() returns uuid
  language sql stable
  as $$ select nullif(current_setting('blocks.user_id', true), '')::uuid $$;

-- ---------------------------------------------------------------------------
-- Identity plane. Global, not tenant-scoped: a user exists before any tenant
-- context does, and is the subject the context is later derived from. These two
-- tables carry no tenant data and deliberately have no RLS in 0.1 -- see the
-- component architecture doc, "Known boundaries", for why and what changes.
-- ---------------------------------------------------------------------------

create table blocks.users (
  id          uuid primary key default gen_random_uuid(),
  issuer      text        not null,
  subject     text        not null,
  email       text,
  created_at  timestamptz not null default now(),
  constraint users_issuer_subject_key unique (issuer, subject)
);

comment on table blocks.users is
  'OIDC identity mapping. (issuer, subject) is the external identity; id is the internal one. Never keyed on email, which is neither stable nor unique across issuers.';

create type blocks.tenant_kind as enum ('personal', 'organization');

create table blocks.tenants (
  id            uuid primary key default gen_random_uuid(),
  kind          blocks.tenant_kind not null,
  name          text        not null,
  -- Set only for personal tenants. UNIQUE gives the "exactly one personal
  -- tenant per user" invariant a database-level home rather than a
  -- best-effort check in application code.
  personal_for  uuid unique references blocks.users (id) on delete cascade,
  created_at    timestamptz not null default now(),
  constraint tenants_personal_shape check (
    (kind = 'personal'     and personal_for is not null) or
    (kind = 'organization' and personal_for is null)
  )
);

-- ---------------------------------------------------------------------------
-- Membership: the authorization edge between a user and a tenant.
--
-- Under RLS this table does double duty. The membership check runs *inside* the
-- tenant-scoped transaction, so the same policy that isolates domain data also
-- decides authorization: a non-member's check sees zero rows and is denied, and
-- cannot enumerate other tenants' members while failing. One mechanism, not two
-- that can disagree.
-- ---------------------------------------------------------------------------

create type blocks.membership_role as enum ('owner', 'member');

create table blocks.memberships (
  tenant_id   uuid not null references blocks.tenants (id) on delete cascade,
  user_id     uuid not null references blocks.users (id)   on delete cascade,
  role        blocks.membership_role not null,
  created_at  timestamptz not null default now(),
  primary key (tenant_id, user_id)
);

create index memberships_user_idx on blocks.memberships (user_id);

-- ---------------------------------------------------------------------------
-- Domain plane. Tenant-scoped; the isolation boundary this milestone proves.
-- ---------------------------------------------------------------------------

create table blocks.projects (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references blocks.tenants (id) on delete cascade,
  name        text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint projects_name_not_blank check (length(btrim(name)) > 0)
);

create index projects_tenant_idx on blocks.projects (tenant_id);

-- ---------------------------------------------------------------------------
-- Row-level security.
--
-- ENABLE alone exempts the table owner, which would make the policies decorative
-- the moment anything connected as blocks_owner. FORCE removes that exemption,
-- so the only way to read another tenant's rows is a role with BYPASSRLS --
-- which bootstrap_roles.sql denies blocks_app and re-asserts on every run.
-- ---------------------------------------------------------------------------

alter table blocks.memberships enable row level security;
alter table blocks.memberships force  row level security;

create policy memberships_tenant_isolation on blocks.memberships
  for all
  using      (tenant_id = blocks.current_tenant())
  with check (tenant_id = blocks.current_tenant());

alter table blocks.projects enable row level security;
alter table blocks.projects force  row level security;

create policy projects_tenant_isolation on blocks.projects
  for all
  using      (tenant_id = blocks.current_tenant())
  with check (tenant_id = blocks.current_tenant());

-- ---------------------------------------------------------------------------
-- Grants. blocks_app gets DML and nothing else: no DDL, no ownership, no
-- privilege to alter a policy out of its own way.
-- ---------------------------------------------------------------------------

grant usage on schema blocks to blocks_app;
grant select, insert, update, delete
  on blocks.users, blocks.tenants, blocks.memberships, blocks.projects
  to blocks_app;
grant execute on function blocks.current_tenant(), blocks.current_actor() to blocks_app;
