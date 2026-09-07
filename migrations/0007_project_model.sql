-- 0007 the project model (ADR-0009).
--
-- Physical-device UAT rejected the work-session workflow. Projects become the
-- unit the product is about: freely browsable, active or archived, each with one
-- explicitly-saved notes document and optional immutable parking.
--
-- FORWARD-ONLY AND ADDITIVE. Nothing legacy is dropped here: work sessions,
-- outcomes, calibration and the map stay exactly as they are, readable, for the
-- declared API 0.2 compatibility window. A migration that removes what a
-- supported client still reads is not a migration, it is an outage.

-- ---------------------------------------------------------------------------
-- Project lifecycle, rename, and optimistic concurrency
-- ---------------------------------------------------------------------------

create type blocks.project_status as enum ('active', 'archived');

alter table blocks.projects
  add column status      blocks.project_status not null default 'active',
  add column row_version bigint not null default 1,
  add column archived_at timestamptz;

-- Archiving hides a project; it never deletes one, and reopening restores it
-- (ADR-0009). The check keeps the timestamp honest in both directions rather
-- than letting an archived_at linger on a reopened project.
alter table blocks.projects
  add constraint projects_archived_at_matches_status check (
    (status = 'archived' and archived_at is not null) or
    (status = 'active'   and archived_at is null)
  );

create index projects_tenant_status_idx on blocks.projects (tenant_id, status, created_at);

-- Composite key, so every new tenant-owned relation can carry tenant_id in its
-- foreign key and a row physically cannot reference a parent in another tenant.
-- RLS already prevents reading across tenants; this prevents *writing* a
-- cross-tenant reference at all, which is a different guarantee and the one a
-- foreign key is actually good at.
alter table blocks.projects
  add constraint projects_tenant_id_key unique (tenant_id, id);

-- ---------------------------------------------------------------------------
-- Notes: one mutable document per project, explicitly saved
-- ---------------------------------------------------------------------------

create table blocks.project_notes (
  tenant_id   uuid not null,
  project_id  uuid not null,
  -- Lightweight project context. NOT tasks, milestones, dates, priorities, or a
  -- second document-management system (ADR-0009) -- so it is one text column,
  -- with no structure for those things to accrete into.
  body        text not null default '',
  row_version bigint not null default 1,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  primary key (tenant_id, project_id),
  constraint project_notes_project_fk
    foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade
);

comment on table blocks.project_notes is
  'One notes document per project, saved explicitly (never autosaved into a conflict). row_version carries optimistic concurrency; body is user-authored text and must never reach an event payload or a log.';

-- ---------------------------------------------------------------------------
-- Parking belongs to a project
-- ---------------------------------------------------------------------------
--
-- ASSOCIATED IN PLACE, not copied. The brief asks that each legacy record reach
-- its historical project "exactly once" while "retaining identity, author,
-- timestamps, ordering and supersession". Adding the relation and backfilling
-- satisfies all of that by construction: ids do not move, nothing is rewritten,
-- and a restart cannot duplicate a row that was only ever updated. Copying into
-- a second relation would need dedup logic to be restart-safe -- which is the
-- failure mode the requirement is guarding against.

alter table blocks.parking_records
  add column project_id uuid;

-- Backfill from the historical work session. Idempotent by the WHERE clause:
-- a second run matches nothing, and an interrupted run resumes where it stopped.
--
-- FORCE ROW LEVEL SECURITY has to come off for the duration, and this is the
-- one place in the schema where that is correct. FORCE removes the table owner's
-- exemption, and a migration has no tenant context to satisfy the policy with --
-- so the UPDATE below matches ZERO rows with FORCE on, silently migrating
-- nothing. The NOT NULL two statements down is what caught that; without it the
-- migration would have reported success having moved not one row.
--
-- The exemption is: the owner only (never blocks_app), inside this transaction
-- only, with the policies themselves untouched. DDL is transactional, so a
-- failure anywhere below rolls the FORCE flag back with everything else.
-- BOTH sides. The first attempt lifted it only on the table being written and
-- still migrated nothing, because the UPDATE ... FROM reads work_sessions, which
-- was itself still forced: the join found no rows to join to. Worth stating,
-- because "the table I am writing" is the intuitive half and it is not enough.
alter table blocks.parking_records no force row level security;
alter table blocks.work_sessions  no force row level security;

update blocks.parking_records p
   set project_id = w.project_id
  from blocks.work_sessions w
 where w.id = p.work_session_id
   and p.project_id is null;

alter table blocks.work_sessions  force row level security;
alter table blocks.parking_records force row level security;

-- Every legacy record now has a project, so the relation can be required and
-- keyed compositely from here on.
alter table blocks.parking_records
  alter column project_id set not null,
  add constraint parking_records_project_fk
    foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade;

-- work_session_id becomes optional: legacy rows keep theirs as provenance, new
-- rows have none. Dropping it would discard history the brief says to preserve.
alter table blocks.parking_records
  alter column work_session_id drop not null;

create index parking_records_project_idx
  on blocks.parking_records (tenant_id, project_id, created_at desc);

comment on column blocks.parking_records.work_session_id is
  'Legacy provenance only. Populated for records that predate ADR-0009; null for records appended to a project directly. Retained rather than dropped so session history stays reconstructable.';

-- `sequence` was unique per work session. Scoped to a project it can legitimately
-- collide -- two sessions of the same project each numbered from 1 -- so the old
-- unique index goes. The COLUMN stays: it is the ordering the brief says to
-- retain, and dropping it would discard history to tidy up an index.
--
-- Ordering is therefore created_at, with id as a deterministic tiebreak. New
-- records still take max+1 within their project, which keeps the column
-- meaningful going forward without pretending it is unique across the legacy set.
drop index if exists blocks.parking_records_session_sequence;

comment on column blocks.parking_records.sequence is
  'Append order. Was unique per work session; after ADR-0009 it is per project and NOT unique across legacy rows, because two sessions of one project each numbered from 1. Order by created_at, id.';

-- ---------------------------------------------------------------------------
-- Row-level security on the new relation, same shape as every other
-- ---------------------------------------------------------------------------

alter table blocks.project_notes enable row level security;
alter table blocks.project_notes force  row level security;

create policy project_notes_tenant_isolation on blocks.project_notes
  for all
  using      (tenant_id = blocks.current_tenant())
  with check (tenant_id = blocks.current_tenant());

grant select, insert, update on blocks.project_notes to blocks_app;
