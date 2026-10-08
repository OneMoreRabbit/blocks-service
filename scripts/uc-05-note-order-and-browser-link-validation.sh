#!/bin/sh
# Service UC-05 companion — exact real-environment commands and expected output.
set -eu

cd "$(dirname -- "$0")/.."

OLD_HEAD=51587286a4e06d26dced251b21171204f3a5b77a
ATLAS_HEAD=2f656990b758709ba8aa5f27a0c28c1055673445
old_dir=$(mktemp -d /tmp/blocks-service-uc05-old.XXXXXX)
build_dir=$(mktemp -d /tmp/blocks-service-uc05-build.XXXXXX)
corpus_copy=$(mktemp /tmp/blocks-service-uc05-corpus.XXXXXX)
full_output=$(mktemp /tmp/blocks-service-uc05-full.XXXXXX)
trap 'rm -rf "$old_dir" "$build_dir" "$corpus_copy" "$full_output"' EXIT HUP INT TERM

echo 'AUTHORITY — Android and Service use the same URI corpus v0.4 bytes'
echo 'COMMAND: compare the committed Service corpus with Android corpus at exact Atlas dev 2f65699'
echo 'EXPECTED: cmp succeeds and both SHA-256 values are identical.'
git -C .atlas show \
  "$ATLAS_HEAD:components/blocks-android/docs/provides/project-resource-uri-validation-v0_4.json" \
  > "$corpus_copy"
cmp tests/project-resource-uri-validation-v0_4.json "$corpus_copy"
sha256sum tests/project-resource-uri-validation-v0_4.json "$corpus_copy"
echo 'AUTHORITY VERDICT: pass'

echo 'OLD-CODE CONTROL — Service 0.7 already accepts the exact owner HTTPS value unchanged'
echo "COMMAND: archive $OLD_HEAD and evaluate the owner value with its 0.7 validator"
echo 'EXPECTED: owner HTTPS accepted unchanged; this was an Android-side value rejection, not a Service 0.7 value rejection.'
mkdir "$old_dir/source"
git archive "$OLD_HEAD" | tar -x -C "$old_dir/source"
PYTHONPATH="$old_dir/source/src" .venv/bin/python - <<'PY'
import json
from pathlib import Path

from blocks_service.project_items import validate_uri

corpus = json.loads(Path("tests/project-resource-uri-validation-v0_4.json").read_text())
owner_value = corpus["accepted"][-1]
assert validate_uri(owner_value, api_version="0.7") == owner_value
print("Service 0.7 owner HTTPS unchanged: pass")
PY

echo 'OLD-CODE NEGATIVE — API 0.8 Note order and URI negotiation do not exist'
echo 'COMMAND: run the API 0.8 route/negotiation checks with source at exact accepted dev'
echo 'EXPECTED: two failures; Note-order is absent and explicit Link API 0.8 is refused.'
mkdir "$old_dir/source/tests/regression"
cp tests/test_note_order.py "$old_dir/source/tests/regression/test_note_order.py"
cp tests/project-resource-uri-validation-v0_4.json \
  "$old_dir/source/tests/regression/project-resource-uri-validation-v0_4.json"
if (
  cd "$old_dir/source"
  PYTHONPATH="$old_dir/source/src" /home/dev/work/blocks-service/.venv/bin/python -m pytest \
    tests/regression/test_note_order.py::test_v04_uri_corpus_has_handler_database_and_route_parity \
    tests/regression/test_note_order.py::test_note_order_requires_explicit_api_08_negotiation -q
) >"$old_dir/result.txt" 2>&1; then
  cat "$old_dir/result.txt"
  echo 'OLD-CODE NEGATIVE VERDICT: fail — accepted dev unexpectedly implements API 0.8'
  exit 1
fi
cat "$old_dir/result.txt"
grep -q '2 failed' "$old_dir/result.txt"
echo 'OLD-CODE NEGATIVE VERDICT: pass — both missing capabilities reproduced'

echo 'CASE 1 — URI corpus v0.4 handler, route and database parity'
echo 'COMMAND: .venv/bin/python -m pytest tests/test_note_order.py::test_v04_uri_corpus_has_handler_database_and_route_parity -q'
echo 'EXPECTED: 1 passed; the owner HTTPS value is stored unchanged and every refusal remains refused.'
.venv/bin/python -m pytest \
  tests/test_note_order.py::test_v04_uri_corpus_has_handler_database_and_route_parity -q
echo 'CASE 1 VERDICT: pass'

echo 'CASE 2 — exact pre-upgrade Note sequence and immutable legacy fields'
echo "COMMAND: .venv/bin/python -m pytest tests/test_migration_project_model.py -q -k '0015'"
echo 'EXPECTED: 4 passed; pinned/updated_at/ID backfill with disagreeing create/edit times, rollback/retry, RLS/grants and composite membership pass.'
.venv/bin/python -m pytest tests/test_migration_project_model.py -q -k '0015'
echo 'CASE 2 VERDICT: pass'

echo 'CASE 3 — complete Note order, durable replay and identifier-only event'
echo 'COMMAND: run the restart/replay test against real PostgreSQL'
echo 'EXPECTED: 1 passed; one receipt/event and no Note body or ordered list in the event.'
.venv/bin/python -m pytest \
  tests/test_note_order.py::test_complete_note_order_replays_after_restart_and_emits_identifiers_only -q
echo 'CASE 3 VERDICT: pass'

echo 'CASE 4 — invalid membership is atomic and non-disclosing'
echo 'COMMAND: run missing, duplicate, deleted, wrong-Project, foreign-tenant and stale cases'
echo 'EXPECTED: 1 passed; no refused case changes positions, version, receipt or event.'
.venv/bin/python -m pytest \
  tests/test_note_order.py::test_note_order_rejects_invalid_membership_atomically_and_non_disclosingly -q
echo 'CASE 4 VERDICT: pass'

echo 'CASE 5 — lifecycle, two-client convergence and explicit API 0.8 negotiation'
echo 'COMMAND: run append/edit/pin/delete, stale reread and negotiation tests'
echo 'EXPECTED: 3 passed; position rules hold and Note-order GET/PUT require exact 0.8.'
.venv/bin/python -m pytest \
  tests/test_note_order.py::test_note_create_appends_edit_and_pin_preserve_and_delete_removes \
  tests/test_note_order.py::test_stale_authenticated_client_rereads_one_authoritative_note_order \
  tests/test_note_order.py::test_note_order_requires_explicit_api_08_negotiation -q
echo 'CASE 5 VERDICT: pass'

echo 'CASE 6 — real backup/restore and restored privilege checks'
echo 'COMMAND: .venv/bin/python -m pytest tests/test_restore_drill.py -q'
echo 'EXPECTED: 7 passed with the read-only backup role and restored RLS/role boundaries.'
.venv/bin/python -m pytest tests/test_restore_drill.py -q
echo 'CASE 6 VERDICT: pass'

echo 'BUILD — exactly one collision-free package 0.9.0 build in this run'
echo 'COMMAND: archive exact HEAD and build one wheel in a clean directory'
echo 'EXPECTED: one blocks_service-0.9.0 wheel is built locally; nothing is uploaded.'
mkdir "$build_dir/source" "$build_dir/wheels"
git archive HEAD | tar -x -C "$build_dir/source"
(
  cd "$build_dir/source"
  python3 -m pip wheel --no-deps --no-build-isolation \
    --wheel-dir "$build_dir/wheels" .
)
test "$(find "$build_dir/wheels" -maxdepth 1 -name 'blocks_service-0.9.0-*.whl' | wc -l)" -eq 1
sha256sum "$build_dir"/wheels/blocks_service-0.9.0-*.whl
echo 'BUILD VERDICT: pass'

echo 'FULL GATE — complete real-PostgreSQL repository suite, zero skips'
echo 'COMMAND: .venv/bin/python -m pytest -q'
echo 'EXPECTED: 539 passed, with no skipped/xfailed/xpassed tests.'
.venv/bin/python -m pytest -q | tee "$full_output"
grep -Eq '^539 passed in ' "$full_output"
if grep -Eq 'skipped|xfailed|xpassed' "$full_output"; then
  echo 'FULL GATE VERDICT: fail — non-passing outcome present'
  exit 1
fi
echo 'FULL GATE VERDICT: pass'

echo 'SOURCE AND AUTHORITY HASHES'
echo "CODE HEAD: $(git rev-parse HEAD)"
sha256sum \
  migrations/0015_note_order.sql \
  src/blocks_service/interaction.py \
  src/blocks_service/project_items.py \
  tests/project-resource-uri-validation-v0_4.json \
  tests/test_note_order.py \
  scripts/uc-05-note-order-and-browser-link-validation.sh
git -C .atlas show \
  "$ATLAS_HEAD:components/arch/docs/needs/blocks-service-api-0-8-note-order-browser-links-v0_1.md" \
  | sha256sum | sed 's/  -$/  durable-need/'
git -C .atlas show \
  "$ATLAS_HEAD:components/blocks-service/tests/UC-05-note-order-and-browser-link-validation.md" \
  | sha256sum | sed 's/  -$/  UC-05-authority/'
git -C .atlas show \
  "$ATLAS_HEAD:components/blocks-android/docs/provides/blocks-android-api-0-8-note-order-boundary-v0_1.md" \
  | sha256sum | sed 's/  -$/  Android-boundary/'

echo 'PAUSE — Android far-end integration'
echo 'After separate API 0.8 acceptance, activation and Android integration authority, use two real devices.'
echo 'Observe both devices converge to one Note order after a stale conflict and reread.'
echo 'No Android, graph, release, migration or deployment action is authorized by this companion.'
echo 'FAR-END VERDICT: blocked:activation-and-android-integration'
echo 'OVERALL AUTOMATED VERDICT: pass; far end remains blocked:activation-and-android-integration'
