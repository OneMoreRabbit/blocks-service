-- 0015 — API 0.8 Note ordering (ADR-0028).
--
-- Additive and forward-only. Existing Note bodies, timestamps, identities,
-- tombstones and legacy pin values are never rewritten. The URI constraint from
-- 0014 already implements the v0.4 general printable-ASCII rule, including the
-- owner-supplied HTTPS browser value, so this migration does not replace it.

create table blocks.note_order_aggregates (
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
  constraint note_order_version_positive check (version >= 1)
);

alter table blocks.project_note_items
  add constraint project_note_items_tenant_project_id_key
  unique (tenant_id, project_id, id);

create table blocks.note_order_positions (
  tenant_id   uuid   not null,
  project_id  uuid   not null,
  note_id     uuid   not null,
  position    bigint not null,
  primary key (tenant_id, project_id, note_id),
  unique (tenant_id, project_id, position) deferrable initially deferred,
  foreign key (tenant_id, project_id)
    references blocks.note_order_aggregates (tenant_id, project_id) on delete cascade,
  foreign key (tenant_id, project_id, note_id)
    references blocks.project_note_items (tenant_id, project_id, id) on delete cascade,
  constraint note_order_position_positive check (position >= 1)
);

alter table blocks.interaction_operations
  drop constraint interaction_operation_type;
alter table blocks.interaction_operations
  add constraint interaction_operation_type check (
    operation_type in ('REORDER_PROJECTS', 'REORDER_LINKS', 'REORDER_NOTES', 'DELETE_PARKING')
  );

-- The prior visible sequence is pinned Notes first, then unpinned Notes, with
-- each group in its released Android 0.9.6 visible order: most recently updated
-- first, then ID descending. Only live membership is copied into the new aggregate.
alter table blocks.projects no force row level security;
alter table blocks.project_note_items no force row level security;

insert into blocks.note_order_aggregates (tenant_id, project_id)
select tenant_id, id from blocks.projects;

insert into blocks.note_order_positions (tenant_id, project_id, note_id, position)
select tenant_id, project_id, id,
       row_number() over (
         partition by tenant_id, project_id
         order by pinned desc, updated_at desc, id desc
       )
  from blocks.project_note_items
 where deleted_at is null;

alter table blocks.project_note_items force row level security;
alter table blocks.projects force row level security;

-- Every future Project has an empty aggregate immediately, so GET has one
-- authoritative version even before its first Note exists.
create function blocks.create_note_order_for_project() returns trigger language plpgsql as $$
begin
  insert into blocks.note_order_aggregates (tenant_id, project_id)
  values (new.tenant_id, new.id)
  on conflict (tenant_id, project_id) do nothing;
  return new;
end
$$;

create trigger projects_create_note_order
  after insert on blocks.projects
  for each row execute function blocks.create_note_order_for_project();

-- Create appends; tombstone removes. Edits and legacy pin changes do not touch
-- order. Locking the aggregate before max(position) makes concurrent appends
-- choose distinct positions.
create function blocks.maintain_note_order() returns trigger language plpgsql as $$
declare next_position bigint;
begin
  insert into blocks.note_order_aggregates (tenant_id, project_id)
  values (new.tenant_id, new.project_id)
  on conflict (tenant_id, project_id) do nothing;

  if tg_op = 'INSERT' or (old.deleted_at is not null and new.deleted_at is null) then
    perform 1 from blocks.note_order_aggregates
     where tenant_id = new.tenant_id and project_id = new.project_id for update;
    select coalesce(max(position), 0) + 1 into next_position
      from blocks.note_order_positions
     where tenant_id = new.tenant_id and project_id = new.project_id;
    insert into blocks.note_order_positions (tenant_id, project_id, note_id, position)
    values (new.tenant_id, new.project_id, new.id, next_position)
    on conflict (tenant_id, project_id, note_id) do nothing;
    update blocks.note_order_aggregates
       set version = version + 1, last_operation_id = null, updated_at = now()
     where tenant_id = new.tenant_id and project_id = new.project_id;
  elsif old.deleted_at is null and new.deleted_at is not null then
    perform 1 from blocks.note_order_aggregates
     where tenant_id = new.tenant_id and project_id = new.project_id for update;
    delete from blocks.note_order_positions
     where tenant_id = new.tenant_id and project_id = new.project_id and note_id = new.id;
    with numbered as (
      select note_id, row_number() over (order by position) as n
        from blocks.note_order_positions
       where tenant_id = new.tenant_id and project_id = new.project_id
    )
    update blocks.note_order_positions p set position = numbered.n
      from numbered
     where p.tenant_id = new.tenant_id and p.project_id = new.project_id
       and p.note_id = numbered.note_id;
    update blocks.note_order_aggregates
       set version = version + 1, last_operation_id = null, updated_at = now()
     where tenant_id = new.tenant_id and project_id = new.project_id;
  end if;
  return new;
end
$$;

create trigger notes_maintain_custom_order
  after insert or update of deleted_at on blocks.project_note_items
  for each row execute function blocks.maintain_note_order();

alter table blocks.note_order_aggregates enable row level security;
alter table blocks.note_order_positions enable row level security;
alter table blocks.note_order_aggregates force row level security;
alter table blocks.note_order_positions force row level security;

create policy note_order_aggregates_tenant on blocks.note_order_aggregates
  using (tenant_id = blocks.current_tenant()) with check (tenant_id = blocks.current_tenant());
create policy note_order_positions_tenant on blocks.note_order_positions
  using (tenant_id = blocks.current_tenant()) with check (tenant_id = blocks.current_tenant());

grant select, insert, update on blocks.note_order_aggregates to blocks_app;
grant select, insert, update, delete on blocks.note_order_positions to blocks_app;
