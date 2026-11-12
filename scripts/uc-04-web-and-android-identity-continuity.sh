#!/bin/sh
# UC-04 companion: run only against the estate-authorized disposable PostgreSQL runner.
# Each case prints its exact command and expected outcome before execution.
set -eu
cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-/home/dev/work/blocks-service/.venv/bin/python}
OLD_HEAD=4daa7c3b0a6127a48955a8c4f58a331279335cb3

echo 'PAUSE: estate must authorize a disposable PostgreSQL 16 runner and private test roles.'
echo 'The full suite drops/recreates blocks_service_test and blocks_migration_drill.'
: "${BLOCKS_DISPOSABLE_DB_APPROVED:?Set to 1 only on the approved disposable runner}"
test "$BLOCKS_DISPOSABLE_DB_APPROVED" = 1
: "${BLOCKS_TEST_ADMIN_DSN:?Private test-admin DSN required}"
: "${BLOCKS_DB_APP_PASSWORD:?Private test app credential required}"
: "${BLOCKS_DB_MIGRATOR_PASSWORD:?Private test migrator credential required}"

echo 'CASE 1: legacy Android scalar remains exact after verifier restart.'
echo 'COMMAND: pytest tests/test_web_audience_identity.py::test_legacy_single_audience_is_one_exact_entry -q'
echo 'EXPECTED: 1 passed; Web and third audiences refused.'
"$PYTHON" -m pytest tests/test_web_audience_identity.py::test_legacy_single_audience_is_one_exact_entry -q

echo 'CASE 2: Android and Web share retained issuer/subject, tenant, membership and Project.'
echo 'COMMAND: pytest retained-identity and concurrent-first-bootstrap tests -q'
echo 'EXPECTED: 2 passed; one user, personal tenant and owner membership.'
"$PYTHON" -m pytest \
  tests/test_web_audience_identity.py::test_android_and_web_reuse_existing_identity_tenant_and_project \
  tests/test_web_audience_identity.py::test_concurrent_first_bootstrap_creates_one_identity -q

echo 'CASE 3: different subject is isolated; invalid credentials fail bootstrap and later read.'
echo 'COMMAND: pytest same-email isolation and invalid-token request tests -q'
echo 'EXPECTED: tests pass; unauthorized reads are 401 or 403 as appropriate.'
"$PYTHON" -m pytest \
  tests/test_web_audience_identity.py::test_same_email_different_subject_stays_separate_and_cannot_read_project \
  tests/test_web_audience_identity.py::test_invalid_tokens_fail_bootstrap_and_later_reads_after_prior_success -q

echo 'CASE 4: malformed, null, ambiguous, duplicate, padded and wildcard lists fail closed.'
echo 'COMMAND: pytest tests/test_web_audience_identity.py -k config/null/list/aud/azp selection -q'
echo 'EXPECTED: selected tests pass; omission keeps the legacy scalar and valid JSON envelope padding works.'
"$PYTHON" -m pytest tests/test_web_audience_identity.py -q -k \
  'invalid_or_ambiguous or malformed_explicit or explicit_null or distinguishes or explicit_array_and_scalar or omission or json_envelope or explicit_list or audience_string_array or invalid_tokens_really or wrong_rsa_signature'

echo 'CASE 5: same candidate binary uses scalar then Android+Web list and can return to scalar.'
echo 'COMMAND: Case 2 staged integration plus Case 1 scalar check above.'
echo 'EXPECTED: Android works in both modes without an identity or data migration.'

echo "NEGATIVE CONTROL: current dev $OLD_HEAD must reject the second configured audience."
old_dir=$(mktemp -d /tmp/blocks-service-uc04-old.XXXXXX)
build_dir=$(mktemp -d /tmp/blocks-service-uc04-build.XXXXXX)
full_log=$(mktemp /tmp/blocks-service-uc04-pytest.XXXXXX)
trap 'rm -rf "$old_dir" "$build_dir" "$full_log"' EXIT HUP INT TERM
git archive "$OLD_HEAD" | tar -x -C "$old_dir"
cp tests/test_web_audience_identity.py "$old_dir/test_web_audience_identity.py"
echo 'COMMAND: run second-audience regression against archived current-dev source.'
echo 'EXPECTED: one failure containing Audience does not match; a pass invalidates this control.'
if PYTHONPATH="$old_dir/src" "$PYTHON" -m pytest \
  "$old_dir/test_web_audience_identity.py::test_regression_second_configured_audience_is_accepted" \
  -q --tb=short > "$old_dir/result.txt" 2>&1; then
  echo 'FAIL: old source unexpectedly accepted the Web audience'
  exit 1
fi
grep -q "Audience doesn't match" "$old_dir/result.txt"
echo 'NEGATIVE CONTROL: expected old-code rejection observed.'

echo 'IMMUTABLE ARTIFACT: build exactly one wheel from this committed source head.'
echo 'COMMAND: git archive HEAD; pip wheel --no-deps with isolated build dependencies; sha256sum wheel.'
mkdir "$build_dir/source" "$build_dir/wheels"
git archive HEAD | tar -x -C "$build_dir/source"
(cd "$build_dir/source" && "$PYTHON" -m pip wheel --no-deps \
  --wheel-dir "$build_dir/wheels" .)
test "$(find "$build_dir/wheels" -maxdepth 1 -name 'blocks_service-0.9.0-*.whl' | wc -l)" -eq 1
sha256sum "$build_dir"/wheels/*.whl

echo 'FULL GATE: complete repository suite against the disposable PostgreSQL runner.'
echo 'COMMAND: pytest -q'
echo 'EXPECTED: all passed, zero skipped/xfailed/xpassed; no shared or production DB touched.'
"$PYTHON" -m pytest -q | tee "$full_log"
grep -Eq '[0-9]+ passed in ' "$full_log"
if grep -Eq 'skipped|xfailed|xpassed' "$full_log"; then
  echo 'FAIL: full gate had a non-passing outcome'
  exit 1
fi
echo "SOURCE HEAD: $(git rev-parse HEAD)"
echo 'CASES 1-5: pass only if all commands above passed.'
echo 'CASE 6 PAUSE: provider-released Android/Web Google tokens and approved Web provisioning.'
echo 'EXPECTED: same exact issuer/subject resolves to retained tenant and Project, other account denied, Android retained.'
echo 'CASE 6: blocked until actual-token, same-Service observation; disposable signed tokens do not pass it.'
