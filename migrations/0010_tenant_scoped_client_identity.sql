-- 0010 tenant-scoped client identity (ADR-0011 v1.2).
--
-- 0009 let a client supply the canonical id directly. That leaked. `id` is a
-- globally unique primary key, so a client-chosen value that another tenant
-- already used fails the insert while an unused one succeeds -- and the STATUS
-- CODE is the oracle regardless of how carefully the message is worded. A caller
-- holding a foreign id could test for its existence.
--
-- The fix is not a better message. It is to stop the client choosing the
-- canonical id at all:
--
--   id         stays server-generated, globally unique, canonical, unchanged;
--   client_id  is the client's own name for the row, unique only WITHIN a tenant.
--
-- Two tenants may use the same client_id for unrelated rows and neither can learn
-- anything about the other, because every uniqueness check is scoped to the
-- tenant doing it. Nothing outside the tenant is ever consulted, so there is
-- nothing to probe.
--
-- Additive and forward-only. 0009 is applied history and its bytes are untouched;
-- canonical primary and foreign keys are not altered; forced RLS is unchanged.

alter table blocks.projects        add column client_id uuid;
alter table blocks.parking_records add column client_id uuid;

comment on column blocks.projects.client_id is
  'The client''s own identifier for this project, unique only within its tenant. Never the canonical id: id stays server-generated so a client-chosen value cannot collide across tenants and cannot be used to probe for one.';

comment on column blocks.parking_records.client_id is
  'As blocks.projects.client_id.';

-- Tenant-scoped and partial: null client_id is the API 0.2/0.3 case and stays
-- unconstrained, so older writers are unaffected.
create unique index projects_tenant_client_id_key
  on blocks.projects (tenant_id, client_id) where client_id is not null;

create unique index parking_records_tenant_client_id_key
  on blocks.parking_records (tenant_id, client_id) where client_id is not null;

-- A creation fingerprint only means something next to the client identity it was
-- computed for. One without the other is a row that can never replay and can
-- never conflict -- a state with no behaviour, which is worth making unreachable
-- rather than merely unlikely.
alter table blocks.projects
  add constraint projects_client_identity_complete check (
    (client_id is null) = (creation_fingerprint is null)
  );

alter table blocks.parking_records
  add constraint parking_client_identity_complete check (
    (client_id is null) = (creation_fingerprint is null)
  );

-- ---------------------------------------------------------------------------
-- 0009-era backfill
-- ---------------------------------------------------------------------------
--
-- Rows created while 0009 was live took the client's value AS their canonical id
-- and recorded a fingerprint. Those clients already hold that value and will
-- replay with it, so client_id = id preserves an identity that is already in use.
--
-- Only those rows. A null fingerprint means the id was server-assigned and the
-- client never named it, so inventing a client_id would be inventing a fact --
-- and would also make the check constraint above a lie.
--
-- FORCE off for the sweep: a migration has no tenant context, so with it on this
-- updates nothing and reports success. Third time on this schema; the constraint
-- above is what would catch it now.
alter table blocks.projects        no force row level security;
alter table blocks.parking_records no force row level security;

update blocks.projects
   set client_id = id
 where creation_fingerprint is not null and client_id is null;

update blocks.parking_records
   set client_id = id
 where creation_fingerprint is not null and client_id is null;

alter table blocks.parking_records force row level security;
alter table blocks.projects        force row level security;
