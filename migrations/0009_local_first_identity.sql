-- 0009 local-first: client-supplied identity and authored parking time (ADR-0011).
--
-- Additive and forward-only. Nothing existing is dropped or rewritten, and no
-- already-applied migration is touched.
--
-- Two capabilities, both driven by the same problem: a client that works offline
-- for an unbounded time cannot rely on the server still remembering an operation.
--
--   1. CREATION IDENTITY, retained per resource forever. The ordinary
--      idempotency-result store answers "what did this operation return"; it is
--      allowed to expire. This answers "is this the same operation that created
--      this resource", which must never expire, because that is what stops a
--      replay after expiry becoming a duplicate.
--   2. AUTHORED TIME on parking. Ordering by arrival lets late delivery rewrite
--      the user's own history; a park written at 09:00 and delivered at 17:00
--      belongs at 09:00.

-- ---------------------------------------------------------------------------
-- 1. Per-resource creation identity
-- ---------------------------------------------------------------------------
--
-- Nullable on purpose. Rows created before 0.4, and rows created by a client that
-- supplies no id, have no creation identity to compare against -- and inventing
-- one would be inventing a fact. A null means "server-assigned, not replayable by
-- resource identity", which is exactly the 0.3 behaviour those rows were created
-- under and which remains supported.

alter table blocks.projects        add column creation_fingerprint bytea;
alter table blocks.parking_records add column creation_fingerprint bytea;

comment on column blocks.projects.creation_fingerprint is
  'Identity of the operation that created this row, for clients supplying their own id. Retained for the life of the resource -- unlike blocks.idempotency_keys, which may expire. Same id + same fingerprint is a replay; same id + different fingerprint is a conflict.';

comment on column blocks.parking_records.creation_fingerprint is
  'As blocks.projects.creation_fingerprint. Null for rows written by API 0.2/0.3 or by a client that supplied no id.';

-- ---------------------------------------------------------------------------
-- 2. Authored occurrence time on parking
-- ---------------------------------------------------------------------------
--
-- created_at keeps its meaning unchanged -- the instant Service ACCEPTED the row --
-- and is not renamed, because 0.2 and 0.3 both publish it and both remain
-- runtime-supported. occurred_at is when the user committed it locally.

alter table blocks.parking_records add column occurred_at timestamptz;

-- Backfill from acceptance time. For every existing row the two are the only
-- honest answer: nothing recorded when the user actually wrote it, and the
-- acceptance instant is the closest true statement available.
--
-- FORCE off for the sweep. A migration has no tenant context, so with FORCE on
-- this updates zero rows and silently backfills nothing -- the failure this schema
-- has now produced twice. The NOT NULL below is what would catch it a third time.
alter table blocks.parking_records no force row level security;

update blocks.parking_records
   set occurred_at = created_at
 where occurred_at is null;

alter table blocks.parking_records force row level security;

alter table blocks.parking_records
  alter column occurred_at set not null,
  alter column occurred_at set default now();

comment on column blocks.parking_records.occurred_at is
  'When the user committed this park, in UTC. Supplied by a local-first client at local commit; defaults to acceptance time for API 0.2/0.3 writers and for rows migrated from before ADR-0011. Never silently rewritten.';

comment on column blocks.parking_records.created_at is
  'When Service ACCEPTED this row. Unchanged meaning, deliberately not renamed: API 0.2 and 0.3 both publish it and both remain runtime-supported.';

-- Newest-first is (occurred_at, created_at, id), all descending: authored time
-- first so delayed delivery cannot reorder the user's history, acceptance time to
-- break a tie between two rows authored in the same instant, and the id last so
-- the order is total and deterministic rather than merely usually-stable.
create index parking_records_authored_order_idx
  on blocks.parking_records (tenant_id, project_id, occurred_at desc, created_at desc, id desc);
