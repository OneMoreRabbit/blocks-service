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
  -- Case-insensitive: schemes are case-insensitive (RFC 3986 section 3.1) and the
  -- handler accepts them that way. A case-sensitive check here rejected on insert
  -- a value the handler had already accepted, so the two layers disagreed about
  -- what is storable. They now share one rule; the handler additionally rejects
  -- malformed authorities this pattern cannot see.
  constraint project_resource_links_uri_scheme
    check (uri ~* '^(https?|obsidian):'),
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

-- ONE key per project, and (by the primary key) one project per key. Android holds
-- a single `ProjectLinkIdentity` per project, so a second key for the same project
-- is not an alias -- it is a conflict with a link already printed on a tag or QR,
-- and it must be refused rather than stored beside it.
create unique index project_navigation_aliases_one_per_project
  on blocks.project_navigation_aliases (tenant_id, project_id);

-- ---------------------------------------------------------------------------
-- Operation receipts
-- ---------------------------------------------------------------------------
--
-- `operation_id` is the identity an offline client retires a queued mutation
-- against, so echoing it is not enough: it has to be ENFORCED. Without a durable
-- receipt, an edit retried after an ambiguous response ran again or reported a
-- stale-version conflict for a write that had in fact succeeded, and one
-- operation id could be spent on several resources -- either of which can make a
-- client retire the wrong queue entry.
--
-- A receipt binds the operation to its complete immutable envelope: project, item,
-- mutation type, If-Match and payload. The same envelope replays the recorded
-- result with no second write and no second event; anything else under that id is
-- a conflict. Tenant-scoped by composite key, for the usual reason -- no global
-- index for a retry to probe.

create table blocks.project_item_operations (
  tenant_id            uuid        not null references blocks.tenants (id) on delete cascade,
  operation_id         uuid        not null,
  project_id           uuid        not null,
  item_kind            text        not null,
  item_id              uuid        not null,
  mutation             text        not null,
  envelope_fingerprint bytea       not null,
  accepted_row_version integer     not null,
  accepted_at          timestamptz not null default now(),
  primary key (tenant_id, operation_id),
  foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade,
  constraint project_item_operations_kind check (item_kind in ('note', 'link')),
  constraint project_item_operations_mutation
    check (mutation in ('create', 'edit', 'pin', 'delete'))
);

-- ---------------------------------------------------------------------------
-- Tenancy
-- ---------------------------------------------------------------------------

alter table blocks.project_note_items        enable row level security;
alter table blocks.project_resource_links    enable row level security;
alter table blocks.project_navigation_aliases enable row level security;
alter table blocks.project_item_operations   enable row level security;

alter table blocks.project_note_items        force row level security;
alter table blocks.project_resource_links    force row level security;
alter table blocks.project_navigation_aliases force row level security;
alter table blocks.project_item_operations   force row level security;

create policy project_note_items_tenant on blocks.project_note_items
  using (tenant_id = current_setting('blocks.tenant_id', true)::uuid)
  with check (tenant_id = current_setting('blocks.tenant_id', true)::uuid);

create policy project_resource_links_tenant on blocks.project_resource_links
  using (tenant_id = current_setting('blocks.tenant_id', true)::uuid)
  with check (tenant_id = current_setting('blocks.tenant_id', true)::uuid);

create policy project_navigation_aliases_tenant on blocks.project_navigation_aliases
  using (tenant_id = current_setting('blocks.tenant_id', true)::uuid)
  with check (tenant_id = current_setting('blocks.tenant_id', true)::uuid);

create policy project_item_operations_tenant on blocks.project_item_operations
  using (tenant_id = current_setting('blocks.tenant_id', true)::uuid)
  with check (tenant_id = current_setting('blocks.tenant_id', true)::uuid);

-- Deletion is a tombstone, so DELETE is granted nowhere: `blocks_app` can set
-- `deleted_at` and nothing else can remove the row's history.
grant select, insert, update on blocks.project_note_items         to blocks_app;
grant select, insert, update on blocks.project_resource_links     to blocks_app;
grant select, insert         on blocks.project_navigation_aliases to blocks_app;
-- Receipts are append-only: a recorded operation can be read and replayed, never
-- rewritten or removed.
grant select, insert         on blocks.project_item_operations    to blocks_app;

-- The dispatcher reads the outbox only; it needs nothing here.
