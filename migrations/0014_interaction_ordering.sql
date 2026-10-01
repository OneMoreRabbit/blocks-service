-- 0014 — API 0.7 interaction ordering and Park tombstones (ADR-0024).
--
-- Additive and forward-only. API 0.6 tables and rows remain readable. This
-- migration adds versioned order aggregates, durable interaction receipts and
-- Park visibility tombstones without editing any earlier migration.

-- Extend the existing Link rule without copying its long HTTP/Obsidian regular
-- expression. pg_get_expr returns the exact installed 0013 expression; the new
-- constraint accepts that expression OR an ordinary safe OneNote URI.
do $$
declare old_rule text;
begin
  select pg_get_expr(conbin, conrelid)
    into old_rule
    from pg_constraint
   where conrelid = 'blocks.project_resource_links'::regclass
     and conname = 'project_resource_links_uri_valid';
  if old_rule is null then
    raise exception '0013 project_resource_links_uri_valid constraint is missing';
  end if;
  execute 'alter table blocks.project_resource_links drop constraint project_resource_links_uri_valid';
  execute format(
    'alter table blocks.project_resource_links add constraint project_resource_links_uri_valid check ((%s) or '
    || '(uri ~ ''^[Oo][Nn][Ee][Nn][Oo][Tt][Ee]:[!-~]+$'' and '
    || 'uri !~ ''^[Oo][Nn][Ee][Nn][Oo][Tt][Ee]://[^/?#]*@''))',
    old_rule
  );
end
$$;

create table blocks.project_order_aggregates (
  tenant_id          uuid        primary key references blocks.tenants (id) on delete cascade,
  aggregate_id       uuid        not null default gen_random_uuid(),
  version            bigint      not null default 1,
  last_operation_id  uuid,
  updated_at         timestamptz not null default now(),
  unique (tenant_id, aggregate_id),
  constraint project_order_version_positive check (version >= 1)
);

create table blocks.project_order_positions (
  tenant_id   uuid   not null,
  project_id  uuid   not null,
  position    bigint not null,
  primary key (tenant_id, project_id),
  unique (tenant_id, position),
  foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade,
  constraint project_order_position_positive check (position >= 1)
);

create table blocks.link_order_aggregates (
  tenant_id          uuid        not null,
  project_id         uuid        not null,
  aggregate_id       uuid        not null default gen_random_uuid(),
  version            bigint      not null default 1,
  last_operation_id  uuid,
  updated_at         timestamptz not null default now(),
  primary key (tenant_id, project_id),
  unique (tenant_id, aggregate_id),
  foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade,
  constraint link_order_version_positive check (version >= 1)
);

create table blocks.link_order_positions (
  tenant_id   uuid   not null,
  project_id  uuid   not null,
  link_id     uuid   not null,
  position    bigint not null,
  primary key (tenant_id, project_id, link_id),
  unique (tenant_id, project_id, position),
  foreign key (tenant_id, project_id)
    references blocks.projects (tenant_id, id) on delete cascade,
  foreign key (tenant_id, link_id)
    references blocks.project_resource_links (tenant_id, id) on delete cascade,
  constraint link_order_position_positive check (position >= 1)
);

create table blocks.interaction_operations (
  tenant_id            uuid        not null references blocks.tenants (id) on delete cascade,
  operation_id         uuid        not null,
  operation_type       text        not null,
  aggregate_id         uuid        not null,
  project_id           uuid,
  target_id            uuid,
  base_version         bigint      not null,
  envelope_fingerprint bytea       not null,
  result               jsonb       not null,
  accepted_at          timestamptz not null default now(),
  primary key (tenant_id, operation_id),
  constraint interaction_operation_type check (
    operation_type in ('REORDER_PROJECTS', 'REORDER_LINKS', 'DELETE_PARKING')
  ),
  constraint interaction_base_version_positive check (base_version >= 1)
);

-- Existing rows acquire deterministic complete order without changing their
-- identities or authored content.
-- A migration has no tenant context. FORCE must be lifted on every source table
-- for this bounded sweep, then restored before the transaction can commit.
alter table blocks.tenants no force row level security;
alter table blocks.projects no force row level security;
alter table blocks.project_resource_links no force row level security;

insert into blocks.project_order_aggregates (tenant_id)
select id from blocks.tenants;

insert into blocks.project_order_positions (tenant_id, project_id, position)
select tenant_id, id,
       row_number() over (partition by tenant_id order by created_at, id)
  from blocks.projects
 where status = 'active';

insert into blocks.link_order_aggregates (tenant_id, project_id)
select tenant_id, id from blocks.projects;

insert into blocks.link_order_positions (tenant_id, project_id, link_id, position)
select tenant_id, project_id, id,
       row_number() over (partition by tenant_id, project_id order by created_at, id)
 from blocks.project_resource_links
 where deleted_at is null;

alter table blocks.project_resource_links force row level security;
alter table blocks.projects force row level security;
alter table blocks.tenants force row level security;

-- New Projects and Links append. Archive and Link tombstones remove the member;
-- reopen appends it again. The aggregate row is locked before max(position), so
-- two concurrent appends cannot choose the same position.
create function blocks.maintain_project_order() returns trigger language plpgsql as $$
declare next_position bigint;
begin
  insert into blocks.project_order_aggregates (tenant_id)
  values (new.tenant_id) on conflict (tenant_id) do nothing;
  insert into blocks.link_order_aggregates (tenant_id, project_id)
  values (new.tenant_id, new.id) on conflict (tenant_id, project_id) do nothing;

  if tg_op = 'INSERT' or (old.status = 'archived' and new.status = 'active') then
    perform 1 from blocks.project_order_aggregates
     where tenant_id = new.tenant_id for update;
    select coalesce(max(position), 0) + 1 into next_position
      from blocks.project_order_positions where tenant_id = new.tenant_id;
    insert into blocks.project_order_positions (tenant_id, project_id, position)
    values (new.tenant_id, new.id, next_position)
    on conflict (tenant_id, project_id) do nothing;
    update blocks.project_order_aggregates
       set version = version + 1, last_operation_id = null, updated_at = now()
     where tenant_id = new.tenant_id;
  elsif old.status = 'active' and new.status = 'archived' then
    perform 1 from blocks.project_order_aggregates
     where tenant_id = new.tenant_id for update;
    delete from blocks.project_order_positions
     where tenant_id = new.tenant_id and project_id = new.id;
    with numbered as (
      select project_id, row_number() over (order by position) as n
        from blocks.project_order_positions where tenant_id = new.tenant_id
    )
    update blocks.project_order_positions p set position = numbered.n
      from numbered
     where p.tenant_id = new.tenant_id and p.project_id = numbered.project_id;
    update blocks.project_order_aggregates
       set version = version + 1, last_operation_id = null, updated_at = now()
     where tenant_id = new.tenant_id;
  end if;
  return new;
end
$$;

create trigger projects_maintain_custom_order
  after insert or update of status on blocks.projects
  for each row execute function blocks.maintain_project_order();

create function blocks.maintain_link_order() returns trigger language plpgsql as $$
declare next_position bigint;
begin
  insert into blocks.link_order_aggregates (tenant_id, project_id)
  values (new.tenant_id, new.project_id)
  on conflict (tenant_id, project_id) do nothing;

  if tg_op = 'INSERT' or (old.deleted_at is not null and new.deleted_at is null) then
    perform 1 from blocks.link_order_aggregates
     where tenant_id = new.tenant_id and project_id = new.project_id for update;
    select coalesce(max(position), 0) + 1 into next_position
      from blocks.link_order_positions
     where tenant_id = new.tenant_id and project_id = new.project_id;
    insert into blocks.link_order_positions (tenant_id, project_id, link_id, position)
    values (new.tenant_id, new.project_id, new.id, next_position)
    on conflict (tenant_id, project_id, link_id) do nothing;
    update blocks.link_order_aggregates
       set version = version + 1, last_operation_id = null, updated_at = now()
     where tenant_id = new.tenant_id and project_id = new.project_id;
  elsif old.deleted_at is null and new.deleted_at is not null then
    perform 1 from blocks.link_order_aggregates
     where tenant_id = new.tenant_id and project_id = new.project_id for update;
    delete from blocks.link_order_positions
     where tenant_id = new.tenant_id and project_id = new.project_id and link_id = new.id;
    with numbered as (
      select link_id, row_number() over (order by position) as n
        from blocks.link_order_positions
       where tenant_id = new.tenant_id and project_id = new.project_id
    )
    update blocks.link_order_positions p set position = numbered.n
      from numbered
     where p.tenant_id = new.tenant_id and p.project_id = new.project_id
       and p.link_id = numbered.link_id;
    update blocks.link_order_aggregates
       set version = version + 1, last_operation_id = null, updated_at = now()
     where tenant_id = new.tenant_id and project_id = new.project_id;
  end if;
  return new;
end
$$;

create trigger links_maintain_custom_order
  after insert or update of deleted_at on blocks.project_resource_links
  for each row execute function blocks.maintain_link_order();

-- Park content stays immutable. Only this four-column visibility transition is
-- writable by blocks_app, and the trigger admits it exactly once.
alter table blocks.parking_records
  add column row_version bigint not null default 1,
  add column deleted_at timestamptz,
  add column last_operation_id uuid,
  add constraint parking_row_version_positive check (row_version >= 1),
  add constraint parking_tombstone_has_operation check (
    deleted_at is null or last_operation_id is not null
  );

create function blocks.parking_tombstone_only() returns trigger language plpgsql as $$
begin
  if old.deleted_at is not null then
    raise exception 'a Park tombstone is terminal' using errcode = 'integrity_constraint_violation';
  end if;
  if new.deleted_at is null
     or new.row_version <> old.row_version + 1
     or new.last_operation_id is null
     or new.where_i_am is distinct from old.where_i_am
     or new.next_physical_action is distinct from old.next_physical_action
     or new.must_not_forget is distinct from old.must_not_forget
     or new.occurred_at is distinct from old.occurred_at
     or new.authored_by_user_id is distinct from old.authored_by_user_id
     or new.id is distinct from old.id
     or new.client_id is distinct from old.client_id
     or new.project_id is distinct from old.project_id then
    raise exception 'Park content is immutable; only a versioned tombstone is allowed'
      using errcode = 'integrity_constraint_violation';
  end if;
  return new;
end
$$;

create trigger parking_tombstone_only
  before update on blocks.parking_records
  for each row execute function blocks.parking_tombstone_only();

alter table blocks.project_order_aggregates enable row level security;
alter table blocks.project_order_positions enable row level security;
alter table blocks.link_order_aggregates enable row level security;
alter table blocks.link_order_positions enable row level security;
alter table blocks.interaction_operations enable row level security;
alter table blocks.project_order_aggregates force row level security;
alter table blocks.project_order_positions force row level security;
alter table blocks.link_order_aggregates force row level security;
alter table blocks.link_order_positions force row level security;
alter table blocks.interaction_operations force row level security;

create policy project_order_aggregates_tenant on blocks.project_order_aggregates
  using (tenant_id = blocks.current_tenant()) with check (tenant_id = blocks.current_tenant());
create policy project_order_positions_tenant on blocks.project_order_positions
  using (tenant_id = blocks.current_tenant()) with check (tenant_id = blocks.current_tenant());
create policy link_order_aggregates_tenant on blocks.link_order_aggregates
  using (tenant_id = blocks.current_tenant()) with check (tenant_id = blocks.current_tenant());
create policy link_order_positions_tenant on blocks.link_order_positions
  using (tenant_id = blocks.current_tenant()) with check (tenant_id = blocks.current_tenant());
create policy interaction_operations_tenant on blocks.interaction_operations
  using (tenant_id = blocks.current_tenant()) with check (tenant_id = blocks.current_tenant());

grant select, insert, update on blocks.project_order_aggregates to blocks_app;
grant select, insert, update, delete on blocks.project_order_positions to blocks_app;
grant select, insert, update on blocks.link_order_aggregates to blocks_app;
grant select, insert, update, delete on blocks.link_order_positions to blocks_app;
grant select, insert on blocks.interaction_operations to blocks_app;
grant update (deleted_at, row_version, last_operation_id) on blocks.parking_records to blocks_app;
