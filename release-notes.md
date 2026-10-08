---
title: Release notes — Blocks Service
status: active
updated: 2026-10-08
owner: blocks.component.blocks-service
about: one short entry per Service release tag, newest first
---

# Release notes — Blocks Service

Newest first. These entries for tags through v0.9.0 were added when this repo adopted
Atlas 1.34.0; those already-published tag commits cannot be changed. Future release
entries belong in the commit that receives the tag.

## v0.9.0 — 2026-10-08

**TL;DR:** Adds synchronized Note ordering and accepts practical browser links under API 0.8.

- A Project has one complete, versioned order for its live Notes.
- Note reorders have durable operation receipts and identifier-only change events.
- Link validation shares URI corpus v0.4 with Android, including the owner's Microsoft browser link.
- Migration 0015 initializes Note order from the prior pinned and recently edited sequence.

**Action:** Deployment needs a separate migration and estate rollout; consumers stay on their current API pin until separately approved.

## v0.7.0 — 2026-10-01

**TL;DR:** Adds Project and Link ordering and Park deletion for synchronization under API 0.7.

- Active Projects and live Links have complete, versioned orders.
- Park deletion leaves a synchronization tombstone instead of removing authored history.
- Interaction operations have durable receipts for exact replay and conflict handling.
- The general safe URI rule accepts ordinary OneNote links.

**Action:** Apply migration 0014 before serving this version, under a separate deployment act.

## v0.6.0 — 2026-09-16

**TL;DR:** Adds discrete Project Notes and Links under API 0.6.

- Notes and Links have client-generated identities, edits, and tombstone deletion.
- Durable operation receipts support retry after an uncertain response.
- Tenant-scoped navigation keys identify Projects without replacing creation identity.

**Action:** Apply migration 0013 before serving this version, under a separate deployment act.

## v0.5.0 — 2026-09-16

**TL;DR:** Adds local-first block registration under API 0.5.

- Clients can supply a block identity and the opaque token already programmed on a tag.
- Registration and binding keep token checks tenant-scoped and fail closed on conflicts.
- A preflight rejects corrupt block history before the schema expands.

**Action:** Apply migration 0012 before serving this version, under a separate deployment act.

## v0.1.0 — 2026-09-08

**TL;DR:** Establishes the first tagged Service package and its Project-centered model.

- Projects, Notes, and Project parking replace the earlier work-session workflow.
- Migration 0008 guarantees a Notes row for every Project, including migrated Projects.
- The service includes tenant authorization, forced row-level security, and a real PostgreSQL restore drill.

**Action:** Use the published contract and migration plan for the target environment; the tag alone does not deploy the service.
