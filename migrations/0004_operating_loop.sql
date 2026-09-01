-- 0004 the operating loop: blocks, bindings, the map, work sessions, parking.
--
-- This is the product. Three constitutional invariants shape the schema more
-- than any feature does, and each is enforced here rather than trusted to
-- application code:
--
--   Invariant 1 -- the map is timeless. map_items carry NO date column of any
--     kind. Not nullable, not optional: absent. A column that exists is a column
--     something will eventually write a manufactured date into.
--   Invariant 3 -- close something at every boundary. A work session commits to a
--     core outcome, with full and polish as optional tiers, and cannot be created
--     without one.
--   Invariant 4 -- the parking contract answers exactly three questions, and
--     history is never silently overwritten. Corrections append and link; the
--     table is INSERT-only to the runtime role by grant, not by convention.

-- ---------------------------------------------------------------------------
-- Physical blocks and their bindings
-- ---------------------------------------------------------------------------

create type blocks.block_state as enum ('active', 'retired');

create table blocks.physical_blocks (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references blocks.tenants (id) on delete cascade,
  label       text not null,
  -- The NFC tag carries a high-entropy random token. Only its hash is stored, so
  -- a database disclosure does not yield working tags. Possession of the token
  -- grants no access on its own -- it is a lookup key, never a credential
  -- (data-architecture, "physical_block").
  token_hash  bytea unique,
  state       blocks.block_state not null default 'active',
  created_at  timestamptz not null default now(),
  constraint physical_blocks_label_not_blank check (length(btrim(label)) > 0)
);

create index physical_blocks_tenant_idx on blocks.physical_blocks (tenant_id);

create table blocks.block_bindings (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references blocks.tenants (id) on delete cascade,
  block_id    uuid not null references blocks.physical_blocks (id) on delete cascade,
  project_id  uuid not null references blocks.projects (id) on delete cascade,
  is_current  boolean not null default true,
  created_at  timestamptz not null default now(),
  ended_at    timestamptz
);

-- Rebinding a block appends a new row and ends the old one; work sessions that
-- referenced the previous binding keep their history. At most one current
-- binding per block, enforced here rather than by a read-modify-write.
create unique index block_bindings_one_current
  on blocks.block_bindings (block_id) where is_current;

create index block_bindings_tenant_idx on blocks.block_bindings (tenant_id);

-- ---------------------------------------------------------------------------
-- The map: ordered milestones and dependencies. No dates. Ever.
-- ---------------------------------------------------------------------------

create type blocks.map_item_status as enum ('open', 'in_progress', 'done', 'dropped');

create table blocks.map_items (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references blocks.tenants (id) on delete cascade,
  project_id  uuid not null references blocks.projects (id) on delete cascade,
  title       text not null,
  status      blocks.map_item_status not null default 'open',
  -- Sparse ordering: gaps let an item be inserted between two others without
  -- renumbering the rest, which would otherwise make every reorder a
  -- whole-list write and a synchronization conflict for offline clients.
  sort_key    bigint not null,
  row_version bigint not null default 1,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint map_items_title_not_blank check (length(btrim(title)) > 0)
);

comment on table blocks.map_items is
  'Constitution invariant 1: the map is timeless. This table deliberately has no planned/target/due date column. Forecasts are derived from observed velocity and must not be written back here.';

create unique index map_items_project_sort on blocks.map_items (project_id, sort_key);
create index map_items_tenant_idx on blocks.map_items (tenant_id);

create table blocks.map_dependencies (
  tenant_id      uuid not null references blocks.tenants (id) on delete cascade,
  predecessor_id uuid not null references blocks.map_items (id) on delete cascade,
  successor_id   uuid not null references blocks.map_items (id) on delete cascade,
  created_at     timestamptz not null default now(),
  primary key (predecessor_id, successor_id),
  -- Self-dependency is a cycle of length one and the only one cheap to catch
  -- declaratively. Longer cycles are the domain service's job.
  constraint map_dependencies_no_self check (predecessor_id <> successor_id)
);

create index map_dependencies_tenant_idx on blocks.map_dependencies (tenant_id);

-- ---------------------------------------------------------------------------
-- Work sessions: the durable march record
-- ---------------------------------------------------------------------------

create type blocks.work_session_state as enum ('active', 'parked', 'closed', 'cancelled');

create table blocks.work_sessions (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null references blocks.tenants (id) on delete cascade,
  project_id         uuid not null references blocks.projects (id) on delete cascade,
  physical_block_id  uuid references blocks.physical_blocks (id) on delete set null,
  started_by_user_id uuid not null references blocks.users (id) on delete cascade,
  -- The boundary the march ends at. A real-world edge owned by the calendar
  -- (invariant 5); stored here as the commitment, not as a second calendar.
  boundary_at        timestamptz,
  core_outcome       text not null,
  full_outcome       text,
  polish_outcome     text,
  state              blocks.work_session_state not null default 'active',
  row_version        bigint not null default 1,
  started_at         timestamptz not null default now(),
  parked_at          timestamptz,
  resumed_at         timestamptz,
  closed_at          timestamptz,
  updated_at         timestamptz not null default now(),
  -- Invariant 3: a block without a core outcome cannot close anything.
  constraint work_sessions_core_outcome_not_blank check (length(btrim(core_outcome)) > 0)
);

-- One active session per user (data-architecture, "Component implication").
-- Picking up another block must first park or close the current one; enforced
-- here so two devices racing offline cannot both win.
create unique index work_sessions_one_active_per_user
  on blocks.work_sessions (started_by_user_id) where state = 'active';

create index work_sessions_tenant_idx on blocks.work_sessions (tenant_id, started_at desc);
create index work_sessions_project_idx on blocks.work_sessions (project_id);

-- ---------------------------------------------------------------------------
-- Parking records: the three questions, append-only
-- ---------------------------------------------------------------------------

create table blocks.parking_records (
  id                   uuid primary key default gen_random_uuid(),
  tenant_id            uuid not null references blocks.tenants (id) on delete cascade,
  work_session_id      uuid not null references blocks.work_sessions (id) on delete cascade,
  -- The contract, in the constitution's own words and order. All three are
  -- required: a parking record that answers two of them is not a parking record.
  where_i_am           text not null,
  next_physical_action text not null,
  must_not_forget      text not null,
  authored_by_user_id  uuid not null references blocks.users (id) on delete cascade,
  device_id            uuid,
  sequence             bigint not null,
  -- A correction appends and points at what it replaces. Nothing is overwritten,
  -- so the history of a re-entry is always reconstructable.
  supersedes_id        uuid references blocks.parking_records (id) on delete set null,
  created_at           timestamptz not null default now(),
  constraint parking_where_not_blank  check (length(btrim(where_i_am)) > 0),
  constraint parking_action_not_blank check (length(btrim(next_physical_action)) > 0),
  constraint parking_forget_not_blank check (length(btrim(must_not_forget)) > 0)
);

create unique index parking_records_session_sequence
  on blocks.parking_records (work_session_id, sequence);

create index parking_records_tenant_idx on blocks.parking_records (tenant_id);
create unique index parking_records_supersedes_once
  on blocks.parking_records (supersedes_id) where supersedes_id is not null;

comment on table blocks.parking_records is
  'Constitution invariant 4. Immutable: blocks_app is granted SELECT and INSERT only, so a correction must append and link via supersedes_id. Re-entry reads the newest non-superseded record.';

-- ---------------------------------------------------------------------------
-- Row-level security: same shape as 0001, applied to every new tenant table
-- ---------------------------------------------------------------------------

do $$
declare t text;
begin
  foreach t in array array[
    'physical_blocks', 'block_bindings', 'map_items',
    'map_dependencies', 'work_sessions', 'parking_records'
  ] loop
    execute format('alter table blocks.%I enable row level security', t);
    execute format('alter table blocks.%I force  row level security', t);
    execute format(
      'create policy %I on blocks.%I for all'
      ' using (tenant_id = blocks.current_tenant())'
      ' with check (tenant_id = blocks.current_tenant())',
      t || '_tenant_isolation', t);
  end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- Grants. Note what blocks_app is NOT given on parking_records.
-- ---------------------------------------------------------------------------

grant select, insert, update, delete on
  blocks.physical_blocks, blocks.block_bindings, blocks.map_items,
  blocks.map_dependencies, blocks.work_sessions
  to blocks_app;

-- Immutability as a privilege, not a convention: no UPDATE, no DELETE. An
-- application bug that tries to edit history is refused by the database.
grant select, insert on blocks.parking_records to blocks_app;
