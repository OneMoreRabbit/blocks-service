-- 0006 outcome tiers and calibration evidence.
--
-- Constitution invariant 8: "Planned-versus-actual block outcomes, restart
-- latency, and observed delivery velocity exist to tune the system. They must
-- not be presented as moral scores, streak pressure, or evidence of personal
-- failure."
--
-- That is a constraint on what this table may contain as much as on what a
-- client may render. It stores measurements of a session -- what was committed,
-- what was reached, how long it took, why it ended -- and deliberately stores no
-- streak counter, no running total, and no derived rate. There is nothing here
-- to render as a score because there is nothing here that is one.

create type blocks.outcome_tier as enum ('core', 'full', 'polish');

-- Why a block ended. 'boundary' is the intended case; 'hijacked' is invariant 2
-- ("a hijacked block closes early, parks its state, and rolls forward") and is a
-- normal input, not a failure. The vocabulary is deliberately neutral.
create type blocks.interruption_kind as enum ('boundary', 'hijacked', 'abandoned', 'unknown');

alter table blocks.work_sessions
  add column outcome_tier blocks.outcome_tier,
  add column interruption blocks.interruption_kind;

create table blocks.calibration_events (
  id                      uuid primary key default gen_random_uuid(),
  tenant_id               uuid not null references blocks.tenants (id) on delete cascade,
  work_session_id         uuid not null references blocks.work_sessions (id) on delete cascade,
  -- The highest tier the session committed to, and the one actually reached.
  -- Both nullable: a session closed without claiming a tier is a legitimate
  -- observation, and forcing a value would manufacture evidence.
  planned_tier            blocks.outcome_tier,
  actual_tier             blocks.outcome_tier,
  elapsed_seconds         bigint,
  interruption            blocks.interruption_kind,
  -- Time from the last park to the resume that followed it. Invariant 2 makes
  -- this the number that matters: fast, low-friction return to useful work.
  restart_latency_seconds bigint,
  created_at              timestamptz not null default now(),
  constraint calibration_elapsed_not_negative check (elapsed_seconds is null or elapsed_seconds >= 0),
  constraint calibration_latency_not_negative check (restart_latency_seconds is null or restart_latency_seconds >= 0)
);

create index calibration_events_session_idx on blocks.calibration_events (work_session_id);
create index calibration_events_tenant_idx  on blocks.calibration_events (tenant_id, created_at);

alter table blocks.calibration_events enable row level security;
alter table blocks.calibration_events force  row level security;

create policy calibration_events_tenant_isolation on blocks.calibration_events
  for all
  using      (tenant_id = blocks.current_tenant())
  with check (tenant_id = blocks.current_tenant());

-- Append-only, like parking records and for a related reason: evidence that can
-- be edited after the fact is not evidence. No UPDATE, no DELETE.
grant select, insert on blocks.calibration_events to blocks_app;

comment on table blocks.calibration_events is
  'Constitution invariant 8: calibration, never judgement. Stores measurements only -- no streaks, no totals, no rates. Append-only by grant.';
