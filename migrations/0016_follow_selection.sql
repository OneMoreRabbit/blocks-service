-- Proposed Sprint 2 Follow selection (ADR-0030).
-- A navigation hint, separate from Project content and the outbox. Additive;
-- existing API 0.8 tables, rows and contract artifacts remain unchanged.

create table blocks.follow_user_state (
  tenant_id          uuid   not null references blocks.tenants (id) on delete cascade,
  user_id            uuid   not null references blocks.users (id) on delete cascade,
  revision           bigint not null default 0,
  current_source_id  uuid,
  run_start_revision bigint not null default 0,
  primary key (tenant_id, user_id),
  constraint follow_revision_valid check (
    revision >= 0 and run_start_revision >= 0
    and run_start_revision <= revision
    and ((revision = 0 and current_source_id is null)
      or (revision > 0 and current_source_id is not null))
  )
);

-- The high-water/last accepted receipt survives hint expiry. It must not be
-- pruned on an inactivity timer: doing so would make an old sequence new again.
create table blocks.follow_sources (
  tenant_id             uuid        not null,
  user_id               uuid        not null,
  source_device_id      uuid        not null,
  high_water_sequence   bigint      not null,
  last_project_id       uuid        not null,
  last_kind             text        not null,
  last_dispatch_class   text        not null,
  last_observed_revision bigint,
  receipt_revision      bigint      not null,
  accepted_at           timestamptz not null,
  expires_at            timestamptz not null,
  primary key (tenant_id, user_id, source_device_id),
  foreign key (tenant_id, user_id)
    references blocks.follow_user_state (tenant_id, user_id) on delete cascade,
  constraint follow_source_sequence_positive check (high_water_sequence >= 1),
  constraint follow_source_receipt_positive check (receipt_revision >= 1),
  constraint follow_source_kind check (last_kind in ('explicit_button', 'automatic_nfc')),
  constraint follow_source_dispatch check (
    (last_dispatch_class = 'immediate' and last_observed_revision is null)
    or (last_dispatch_class = 'deferred' and last_observed_revision >= 0)
  )
);

-- One latest explicit signal reaches every opted-in same-user browser. The
-- selected target is re-authorized at read time and never read as content.
create table blocks.follow_explicit (
  tenant_id        uuid        not null,
  user_id          uuid        not null,
  source_device_id uuid        not null,
  project_id       uuid        not null,
  revision         bigint      not null,
  accepted_at      timestamptz not null,
  expires_at       timestamptz not null,
  primary key (tenant_id, user_id),
  foreign key (tenant_id, user_id, source_device_id)
    references blocks.follow_sources (tenant_id, user_id, source_device_id) on delete cascade,
  foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade
);

-- Automatic NFC is scoped to one source chosen by the browser. A source
-- preference never hides the latest explicit button signal.
create table blocks.follow_automatic (
  tenant_id        uuid        not null,
  user_id          uuid        not null,
  source_device_id uuid        not null,
  project_id       uuid        not null,
  revision         bigint      not null,
  accepted_at      timestamptz not null,
  expires_at       timestamptz not null,
  primary key (tenant_id, user_id, source_device_id),
  foreign key (tenant_id, user_id, source_device_id)
    references blocks.follow_sources (tenant_id, user_id, source_device_id) on delete cascade,
  foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade
);

-- Tenant AND actor are required. A pooled connection with only a tenant
-- context cannot enumerate other users' selection or source receipts.
alter table blocks.follow_user_state enable row level security;
alter table blocks.follow_sources enable row level security;
alter table blocks.follow_explicit enable row level security;
alter table blocks.follow_automatic enable row level security;
alter table blocks.follow_user_state force row level security;
alter table blocks.follow_sources force row level security;
alter table blocks.follow_explicit force row level security;
alter table blocks.follow_automatic force row level security;

create policy follow_user_state_actor on blocks.follow_user_state for all
  using (tenant_id = blocks.current_tenant() and user_id = blocks.current_actor())
  with check (tenant_id = blocks.current_tenant() and user_id = blocks.current_actor());
create policy follow_sources_actor on blocks.follow_sources for all
  using (tenant_id = blocks.current_tenant() and user_id = blocks.current_actor())
  with check (tenant_id = blocks.current_tenant() and user_id = blocks.current_actor());
create policy follow_explicit_actor on blocks.follow_explicit for all
  using (tenant_id = blocks.current_tenant() and user_id = blocks.current_actor())
  with check (tenant_id = blocks.current_tenant() and user_id = blocks.current_actor());
create policy follow_automatic_actor on blocks.follow_automatic for all
  using (tenant_id = blocks.current_tenant() and user_id = blocks.current_actor())
  with check (tenant_id = blocks.current_tenant() and user_id = blocks.current_actor());

grant select, insert, update on blocks.follow_user_state to blocks_app;
grant select, insert, update on blocks.follow_sources to blocks_app;
grant select, insert, update on blocks.follow_explicit to blocks_app;
grant select, insert, update on blocks.follow_automatic to blocks_app;
