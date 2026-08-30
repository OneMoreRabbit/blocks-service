-- 0002 transactional outbox.
--
-- Domain events are written in the SAME transaction as the state change that
-- produced them, so "the row exists but the event was lost" is not a reachable
-- state. Delivery is a separate, later concern: the dispatcher reads this table
-- and marks rows dispatched. That split is the point -- committing a fact and
-- publishing it are different problems with different failure modes.

create table blocks.outbox (
  id             bigint generated always as identity primary key,
  tenant_id      uuid        not null references blocks.tenants (id) on delete cascade,
  event_type     text        not null,
  -- The version of the event schema this payload was written against. Stamped
  -- per row, not per deployment: a consumer reading history must be able to tell
  -- which shape it is looking at.
  event_version  text        not null,
  payload        jsonb       not null,
  occurred_at    timestamptz not null default now(),
  dispatched_at  timestamptz,
  constraint outbox_event_type_not_blank check (length(btrim(event_type)) > 0)
);

-- Partial index: the dispatcher's only query is "what is still undelivered",
-- and that set stays small while the table does not.
create index outbox_undispatched_idx
  on blocks.outbox (id)
  where dispatched_at is null;

create index outbox_tenant_idx on blocks.outbox (tenant_id, id);

alter table blocks.outbox enable row level security;
alter table blocks.outbox force  row level security;

create policy outbox_tenant_isolation on blocks.outbox
  for all
  using      (tenant_id = blocks.current_tenant())
  with check (tenant_id = blocks.current_tenant());

-- blocks_app appends and reads its own tenant's events. It cannot mark rows
-- dispatched: that is the dispatcher's privilege, and the dispatcher is a
-- separate role that does not exist yet -- see the component architecture doc,
-- "Known boundaries".
grant select, insert on blocks.outbox to blocks_app;
