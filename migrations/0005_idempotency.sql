-- 0005 idempotency keys.
--
-- Both clients queue operations offline and retry them on reconnect, so the
-- service must apply each once (data-architecture, "Offline operation and
-- synchronization"). The client supplies the key; the server remembers the
-- outcome and replays it.
--
-- The fingerprint is what makes this safe rather than merely convenient. A key
-- replayed with a DIFFERENT request is a client bug -- two distinct operations
-- sharing an identifier -- and returning the first response would silently
-- discard the second operation. That case is refused, loudly.

create table blocks.idempotency_keys (
  tenant_id    uuid  not null references blocks.tenants (id) on delete cascade,
  key          text  not null,
  -- Hash of method + path + body. Compared, never displayed; a request body may
  -- contain anything the user typed.
  fingerprint  bytea not null,
  status_code  int   not null,
  response     jsonb not null,
  created_at   timestamptz not null default now(),
  primary key (tenant_id, key),
  constraint idempotency_key_not_blank check (length(btrim(key)) > 0)
);

-- Retention is an operational concern, not a correctness one: replaying a key
-- older than the window simply executes again, which is why the operations it
-- guards must also be safe to repeat where possible.
create index idempotency_keys_created_idx on blocks.idempotency_keys (created_at);

alter table blocks.idempotency_keys enable row level security;
alter table blocks.idempotency_keys force  row level security;

create policy idempotency_keys_tenant_isolation on blocks.idempotency_keys
  for all
  using      (tenant_id = blocks.current_tenant())
  with check (tenant_id = blocks.current_tenant());

-- No UPDATE: an idempotency record is written once. Rewriting one would change
-- what a retry replays, which is the one thing it must never do.
grant select, insert, delete on blocks.idempotency_keys to blocks_app;
