-- 0012 — client-generated block identity for local-first tag programming
-- (ADR-0014, needs/blocks-service-local-first-block-registration-v0_1.md).
--
-- Additive. No column is dropped or renamed, no existing row is rewritten, and
-- no legacy block is given a fabricated client identity: a server-generated
-- block has no truthful client id, and inventing one would assert a fact about
-- a client that never supplied it.

alter table blocks.physical_blocks
  add column client_id uuid,
  add column creation_fingerprint bytea;

comment on column blocks.physical_blocks.client_id is
  'Tenant-scoped client-generated block identity (ADR-0014). Null for blocks registered through the API 0.4 server-generated flow.';
comment on column blocks.physical_blocks.creation_fingerprint is
  'SHA-256 over client identity, label and token hash. Durable creation identity: replay correctness does not depend on idempotency-result retention.';

-- The 0.4 token was minted by the service, so a global unique index was harmless
-- and collisions were the service''s own problem. A client-supplied token makes
-- that index an oracle: tenant B registering a token tenant A already holds
-- would fail, and the failure alone discloses that the value exists somewhere.
-- Uniqueness is therefore tenant-scoped, which is what the isolation model
-- claimed all along.
alter table blocks.physical_blocks
  drop constraint physical_blocks_token_hash_key;

create unique index physical_blocks_tenant_token_hash_key
  on blocks.physical_blocks (tenant_id, token_hash)
  where token_hash is not null;

create unique index physical_blocks_tenant_client_id_key
  on blocks.physical_blocks (tenant_id, client_id)
  where client_id is not null;

-- Client identity and its fingerprint arrive together or not at all. A block
-- carrying one without the other could not be replayed correctly after the
-- idempotency result expires, which is the whole point of retaining it.
-- NOT VALID then validated: every existing row has both columns null and
-- satisfies it, but the two-step is the habit that keeps a future data-bearing
-- constraint honest rather than asserted.
alter table blocks.physical_blocks
  add constraint physical_blocks_client_identity_complete
  check ((client_id is null) = (creation_fingerprint is null)) not valid;

alter table blocks.physical_blocks
  validate constraint physical_blocks_client_identity_complete;

-- ---------------------------------------------------------------------------
-- Tenant-composite referential integrity for bindings
-- ---------------------------------------------------------------------------
--
-- `block_bindings.block_id` and `.project_id` reference globally keyed parents.
-- Row-level security checks the CHILD's tenant_id and cannot see which tenant the
-- parent belongs to, so a guessed UUID from another tenant satisfies the foreign
-- key while the child row claims the caller's tenant. That is cross-tenant
-- referential corruption, and — through the global one-current-binding index —
-- a way to end a binding in a tenant the caller cannot read.
--
-- The application now proves both parents are visible before writing. This is the
-- half that holds when application validation is bypassed: the composite keys
-- make "same tenant" part of the reference itself, so the database cannot store
-- the corrupt shape at all.

-- `projects` already carries `projects_tenant_id_key` from 0007, which introduced
-- exactly this pattern for the project-model tables. Bindings are simply the
-- table it never reached, so this is completing an established invariant rather
-- than inventing one.
alter table blocks.physical_blocks
  add constraint physical_blocks_tenant_id_key unique (tenant_id, id);

alter table blocks.block_bindings
  add constraint block_bindings_block_in_tenant
  foreign key (tenant_id, block_id)
  references blocks.physical_blocks (tenant_id, id) on delete cascade
  not valid;

alter table blocks.block_bindings
  add constraint block_bindings_project_in_tenant
  foreign key (tenant_id, project_id)
  references blocks.projects (tenant_id, id) on delete cascade
  not valid;

-- Validation reads every existing binding and both parent tables. Forced RLS
-- applies to the owner too, so under FORCE the scan sees nothing and the
-- constraint would be marked valid without having checked a single row --
-- "validated" against an empty view. FORCE is lifted for the scan, inside this
-- transaction, and restored before commit. A test asserts the restoration,
-- because a migration that leaves FORCE off is a silent, total loss of tenant
-- isolation.
alter table blocks.block_bindings   no force row level security;
alter table blocks.physical_blocks  no force row level security;
alter table blocks.projects         no force row level security;

alter table blocks.block_bindings validate constraint block_bindings_block_in_tenant;
alter table blocks.block_bindings validate constraint block_bindings_project_in_tenant;

alter table blocks.block_bindings   force row level security;
alter table blocks.physical_blocks  force row level security;
alter table blocks.projects         force row level security;
