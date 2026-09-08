-- 0011 let an idempotency key be RESERVED before its operation runs.
--
-- 0005 stored a result: the row was written after `produce()` had already created
-- the resource and its outbox event. That ordering has a hole that only opens
-- under simultaneous delivery. Two requests in one tenant with the SAME key and
-- DIFFERENT bodies both find no stored result, both run their operation, and both
-- write a project and an event. One then wins the key; the loser rolls back only
-- its key insert, so its extra resource and event commit anyway -- and it is told
-- the winner's answer. One operation key, two resources, and the caller never
-- learns.
--
-- The fix is ordering, not detection: claim the key BEFORE doing any work, so at
-- most one request is ever entitled to execute. PostgreSQL then supplies the
-- barrier for free -- a second inserter of the same key BLOCKS on the unique index
-- until the first transaction commits or aborts, and learns the outcome rather
-- than racing it.
--
-- For the claim to exist before the result, the result columns must be able to be
-- absent for the moment between claiming and completing. Additive: relaxing NOT
-- NULL invalidates no existing row.

alter table blocks.idempotency_keys
  alter column status_code drop not null,
  alter column response    drop not null;

comment on column blocks.idempotency_keys.status_code is
  'Null only between reserving the key and completing the operation, inside one transaction. A committed row always has it: a reservation that never completes is rolled back with the work it was reserving.';

comment on column blocks.idempotency_keys.response is
  'As status_code. A waiter that loses the reservation therefore always reads a completed result, never a half-written one.';

-- ---------------------------------------------------------------------------
-- The one write the reservation needs, and not one more
-- ---------------------------------------------------------------------------
--
-- 0005 granted blocks_app no UPDATE at all, with the reasoning that "rewriting a
-- record would change what a retry replays, which is the one thing it must never
-- do". That reasoning is still exactly right for a COMPLETED record, so the grant
-- here is deliberately the narrowest thing that makes reservation possible:
--
--   column-level, so tenant_id, key and fingerprint remain unwritable -- the
--   identity of an operation can never be edited after the fact;
--   write-once, enforced by the trigger below, so a completed result is as
--   immutable as it was before this migration.
--
-- The only transition permitted is null -> result, exactly once.

grant update (status_code, response) on blocks.idempotency_keys to blocks_app;

create function blocks.idempotency_result_is_write_once() returns trigger
  language plpgsql
  as $$
begin
  if old.status_code is not null then
    raise exception 'idempotency result is write-once and has already been recorded'
      using errcode = 'integrity_constraint_violation';
  end if;
  return new;
end
$$;

comment on function blocks.idempotency_result_is_write_once() is
  'A stored result may be completed once and never rewritten. Without this, the column grant above would let a retry change what an earlier retry had already been told.';

create trigger idempotency_keys_write_once
  before update on blocks.idempotency_keys
  for each row execute function blocks.idempotency_result_is_write_once();
