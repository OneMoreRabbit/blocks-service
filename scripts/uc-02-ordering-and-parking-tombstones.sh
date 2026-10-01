#!/bin/sh
# Service UC-02 companion — exact real-environment commands and expected output.
set -eu

cd "$(dirname -- "$0")/.."

echo 'CASE 1 — shared URI corpus at handler and database far ends'
echo 'COMMAND: .venv/bin/python -m pytest tests/test_interaction.py::test_v03_uri_corpus_has_handler_and_database_parity tests/test_project_items.py::test_api_06_create_and_edit_keep_surrounding_whitespace_normalization tests/test_project_items.py::test_api_07_is_explicit_strict_and_onenote_remains_new_to_it -q'
echo 'EXPECTED: 3 passed; v0.3 has API/database parity, 0.6 normalizes create/edit, and explicit 0.7 is strict with ordinary OneNote.'
.venv/bin/python -m pytest \
  tests/test_interaction.py::test_v03_uri_corpus_has_handler_and_database_parity \
  tests/test_project_items.py::test_api_06_create_and_edit_keep_surrounding_whitespace_normalization \
  tests/test_project_items.py::test_api_07_is_explicit_strict_and_onenote_remains_new_to_it -q

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
echo 'EXPECTED: 4 passed; complete backfill, transactional rollback/restart, forced RLS, append-only receipts, and Project-scoped Link membership.'
.venv/bin/python -m pytest tests/test_migration_project_model.py -q -k '0014'

echo 'PAUSE — architecture acceptance of the unmerged code and Service-owned Atlas PRs.'
echo 'No human-only runtime step is required for Service UC-02 before that review.'
echo 'VERDICT: pass'
