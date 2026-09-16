-- 0013 — discrete project items: Notes, resource Links, navigation aliases
-- (ADR-0021, ADR-0022, needs/blocks-service-api-0-6-project-items-v0_1.md,
--  components/blocks-android/docs/provides/blocks-android-project-items-sync-boundary-v0_1.md).
--
-- Additive and forward-only. No existing column, row or constraint is altered.
-- In particular `project_notes` -- the API 0.4 singleton -- is NOT read, written,
-- derived from, or migrated here. ADR-0022 makes that a clean break rather than a
-- conversion, so the absence of any statement touching it is the point.

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------
--
-- The item's identity IS the client-generated UUID, scoped by tenant: a composite
-- primary key `(tenant_id, id)` rather than a globally unique `id`. That single
-- choice carries the isolation guarantee Android asks for -- "its uniqueness check
-- must never consult another tenant" -- because there is no global index to
-- consult. Two tenants may use the same UUID for unrelated items and neither can
-- learn that the other did, so the create path has no existence oracle to leak.
--
-- Migrations 0010 and 0012 each had to retrofit exactly this property onto an
-- identifier that started global. Starting composite is the same lesson applied
-- before it costs anything.

create table blocks.project_note_items (
  tenant_id            uuid        not null references blocks.tenants (id) on delete cascade,
  id                   uuid        not null,
  project_id           uuid        not null,
  body                 text        not null,
  pinned               boolean     not null default false,
  -- Derived from the authenticated principal, never from the request body.
  authored_by_user_id  uuid        references blocks.users (id) on delete set null,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  row_version          integer     not null default 1,
  deleted_at           timestamptz,
  -- Durable creation identity over (tenant, client id, initial body), so replay
  -- stays correct after the idempotency result expires -- the same reason
  -- ADR-0011 retains one for projects.
  creation_fingerprint bytea       not null,
  -- The operation whose acknowledgement last produced this state. Events and
  -- replays name it so a client can retire exactly one queued operation.
  last_operation_id    uuid        not null,
  primary key (tenant_id, id),
  -- Tenant-composite: a project in another tenant cannot be referenced even if
  -- its UUID is guessed (the defect 0012 closed for block bindings).
  foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade,
  constraint project_note_items_body_length check (length(body) <= 100000),
  constraint project_note_items_row_version_positive check (row_version >= 1)
);

create index project_note_items_project_idx
  on blocks.project_note_items (tenant_id, project_id);

create table blocks.project_resource_links (
  tenant_id            uuid        not null references blocks.tenants (id) on delete cascade,
  id                   uuid        not null,
  project_id           uuid        not null,
  title                text        not null,
  uri                  text        not null,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  row_version          integer     not null default 1,
  deleted_at           timestamptz,
  creation_fingerprint bytea       not null,
  last_operation_id    uuid        not null,
  primary key (tenant_id, id),
  foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade,
  constraint project_resource_links_title_length
    check (length(title) > 0 and length(title) <= 200),
  constraint project_resource_links_title_not_blank check (length(btrim(title)) > 0),
  -- The allowlist Android publishes, enforced in the database as well as the
  -- handler. Service stores a logical value; it never resolves, fetches or
  -- launches it.
  constraint project_resource_links_uri_scheme
    check (uri ~ '^(https?|obsidian)://'),
  constraint project_resource_links_row_version_positive check (row_version >= 1)
);

create index project_resource_links_project_idx
  on blocks.project_resource_links (tenant_id, project_id);

-- ---------------------------------------------------------------------------
-- Project navigation aliases
-- ---------------------------------------------------------------------------
--
-- A DISTINCT identity from `projects.client_id`, deliberately. The accepted
-- project-link review established that an Android-held navigation key is not
-- always the creation identity: a key allocated before, or independently of,
-- Service identity stays distinct and must never be rotated into one or inferred
-- from the other. Storing it here rather than in `projects.client_id` is what
-- keeps those two facts separable -- and it avoids the
-- `projects_client_identity_complete` invariant, which would otherwise demand a
-- `creation_fingerprint` attesting to a create that never happened.

create table blocks.project_navigation_aliases (
  tenant_id   uuid        not null references blocks.tenants (id) on delete cascade,
  project_key uuid        not null,
  project_id  uuid        not null,
  created_at  timestamptz not null default now(),
  primary key (tenant_id, project_key),
  foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade
);

create index project_navigation_aliases_project_idx
  on blocks.project_navigation_aliases (tenant_id, project_id);

-- ---------------------------------------------------------------------------
-- Tenancy
-- ---------------------------------------------------------------------------

alter table blocks.project_note_items        enable row level security;
alter table blocks.project_resource_links    enable row level security;
alter table blocks.project_navigation_aliases enable row level security;

alter table blocks.project_note_items        force row level security;
alter table blocks.project_resource_links    force row level security;
alter table blocks.project_navigation_aliases force row level security;

create policy project_note_items_tenant on blocks.project_note_items
  using (tenant_id = current_setting('blocks.tenant_id', true)::uuid)
  with check (tenant_id = current_setting('blocks.tenant_id', true)::uuid);

create policy project_resource_links_tenant on blocks.project_resource_links
  using (tenant_id = current_setting('blocks.tenant_id', true)::uuid)
  with check (tenant_id = current_setting('blocks.tenant_id', true)::uuid);

create policy project_navigation_aliases_tenant on blocks.project_navigation_aliases
  using (tenant_id = current_setting('blocks.tenant_id', true)::uuid)
  with check (tenant_id = current_setting('blocks.tenant_id', true)::uuid);

-- Deletion is a tombstone, so DELETE is granted nowhere: `blocks_app` can set
-- `deleted_at` and nothing else can remove the row's history.
grant select, insert, update on blocks.project_note_items         to blocks_app;
grant select, insert, update on blocks.project_resource_links     to blocks_app;
grant select, insert         on blocks.project_navigation_aliases to blocks_app;

-- The dispatcher reads the outbox only; it needs nothing here.
