-- 0008 the notes invariant: preventive AND healing, in one transaction.
--
-- 0007 added the notes relation. It did not guarantee a row per project, and two
-- separate windows can leave one missing:
--
--   1. projects that predate 0007 entirely;
--   2. projects created by the PRE-0.3 writer between 0007 and cutover. The
--      deployment contract runs migrations before switching the image and does
--      not stop the old API first, so that writer is live across the gap and its
--      project-only INSERT produces a project with no notes row.
--
-- Either way the project 404s on its page and can never take a first note,
-- because the save path UPDATEs and finds nothing. A backfill alone fixes only
-- the rows that already exist, which is the wrong half when two versions of the
-- writer are live at once. So the guarantee becomes a database invariant.
--
-- ORDER MATTERS, and it is the reason this reads trigger-then-catch-up. Creating
-- the trigger FIRST closes the door before sweeping the floor: any project
-- inserted while this transaction is in flight is covered by the trigger, and the
-- catch-up then handles everything that predates it. Catching up first and
-- creating the trigger second leaves a window -- exactly the shape of window that
-- caused this migration to be necessary.

create function blocks.ensure_project_notes() returns trigger
  language plpgsql
  as $$
begin
  -- Conflict-safe so the invariant composes with a writer that also inserts:
  -- whoever arrives first wins, the other is a no-op, and neither fails on a
  -- duplicate key. It is what lets the catch-up below run under the trigger.
  insert into blocks.project_notes (tenant_id, project_id)
  values (new.tenant_id, new.id)
  on conflict (tenant_id, project_id) do nothing;
  return new;
end
$$;

comment on function blocks.ensure_project_notes() is
  'Every project has exactly one notes row, created with it. Deliberately NOT security definer: it runs as the inserting role under that role''s own policies, so it cannot write a notes row into a tenant the caller could not already write to.';

-- 1. The door.
create trigger projects_ensure_notes
  after insert on blocks.projects
  for each row execute function blocks.ensure_project_notes();

-- 2. The floor. Everything that predates the trigger, in the same transaction.
--
-- FORCE comes off for the sweep, for the same reason as 0007's backfill: a
-- migration has no tenant context, so with FORCE on this reads zero projects and
-- silently repairs nothing. tenant_id is carried from the project row, so
-- isolation comes from the data rather than from the policy being active during
-- the copy -- and project_notes' composite foreign key makes a cross-tenant
-- pairing unwritable regardless of which policies are enabled.
alter table blocks.projects      no force row level security;
alter table blocks.project_notes no force row level security;

insert into blocks.project_notes (tenant_id, project_id)
select p.tenant_id, p.id
  from blocks.projects p
 where not exists (
   select 1 from blocks.project_notes n
    where n.tenant_id = p.tenant_id and n.project_id = p.id
 )
    on conflict (tenant_id, project_id) do nothing;

alter table blocks.project_notes force row level security;
alter table blocks.projects      force row level security;
