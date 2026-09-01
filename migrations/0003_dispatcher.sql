-- 0003 outbox dispatcher privilege (ADR-0005).
--
-- The dispatcher must read undispatched events across ALL tenants and mark them
-- delivered. It acts for no tenant, so it has no context to set, and under the
-- 0002 policies it would see nothing.
--
-- ADR-0005 rejected BYPASSRLS for this. A role attribute is invisible in the
-- schema, absent from pg_policies, unbounded across every table, and would defeat
-- the readiness probe that asserts the runtime role cannot bypass RLS. It also
-- rejected a SECURITY DEFINER function: FORCE ROW LEVEL SECURITY removes the
-- owner's exemption, so a function owned by blocks_owner is still filtered --
-- measured at 0 rows of 4.
--
-- What is used instead is two separate mechanisms doing two separate jobs:
--
--   the POLICIES below grant VISIBILITY  -- which rows the dispatcher may see;
--   the COLUMN GRANT grants AUTHORITY    -- which column it may write.
--
-- Neither alone is sufficient, and neither alone is dangerous.

create policy outbox_dispatcher_read on blocks.outbox
  for select to blocks_dispatcher
  using (true);

create policy outbox_dispatcher_mark on blocks.outbox
  for update to blocks_dispatcher
  using (true) with check (true);

grant usage  on schema blocks to blocks_dispatcher;
grant select on blocks.outbox to blocks_dispatcher;

-- COLUMN-level, and the narrowness is the whole control. A table-wide UPDATE
-- would let a compromised dispatcher rewrite tenant_id -- moving an event into
-- another tenant -- or forge event_type, event_version or payload. Measured under
-- this grant, all five are denied, and id is immutable by GENERATED ALWAYS.
grant update (dispatched_at) on blocks.outbox to blocks_dispatcher;

comment on policy outbox_dispatcher_read on blocks.outbox is
  'ADR-0005: cross-tenant visibility of the outbox, and of nothing else. Paired with a column-level UPDATE grant that bounds what the dispatcher may write.';
