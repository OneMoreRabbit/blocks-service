#!/bin/sh
# Service UC-02 companion — exact real-environment commands and expected output.
set -eu

cd "$(dirname -- "$0")/.."

echo 'CASE 1 — shared URI corpus at handler and database far ends'
echo 'COMMAND: .venv/bin/python -m pytest tests/test_interaction.py::test_v03_uri_corpus_has_handler_and_database_parity -q'
echo 'EXPECTED: 1 passed; every v0.3 accepted/refused value has the same API and direct blocks_app database decision.'
.venv/bin/python -m pytest \
  tests/test_interaction.py::test_v03_uri_corpus_has_handler_and_database_parity -q

echo 'CASE 2 — complete Link and active-Project orders, replay and refusal'
echo 'COMMAND: .venv/bin/python -m pytest tests/test_interaction.py -q -k order'
echo 'EXPECTED: all selected cases pass; database positions survive reread and events contain identifiers only.'
.venv/bin/python -m pytest tests/test_interaction.py -q -k order

echo 'CASE 3 — Park tombstone, immutable authored row and synchronization read'
echo 'COMMAND: .venv/bin/python -m pytest tests/test_interaction.py -q -k park'
echo 'EXPECTED: all selected cases pass; ordinary reads hide the row, sync reads and direct SQL retain its tombstone.'
.venv/bin/python -m pytest tests/test_interaction.py -q -k park

echo 'CASE 4 — production-shaped upgrade and interrupted migration restart'
echo "COMMAND: .venv/bin/python -m pytest tests/test_migration_project_model.py -q -k '0014'"
echo 'EXPECTED: 3 passed; complete backfill, transactional rollback/restart, forced RLS and append-only receipts.'
.venv/bin/python -m pytest tests/test_migration_project_model.py -q -k '0014'

echo 'PAUSE — architecture acceptance of the unmerged code and Service-owned Atlas PRs.'
echo 'No human-only runtime step is required for Service UC-02 before that review.'
echo 'VERDICT: pass'
