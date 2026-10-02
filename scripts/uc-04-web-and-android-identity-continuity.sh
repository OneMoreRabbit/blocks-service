#!/bin/sh
# Service UC-04 companion — exact real-environment commands and expected output.
set -eu

cd "$(dirname -- "$0")/.."

OLD_HEAD=51587286a4e06d26dced251b21171204f3a5b77a
ATLAS_HEAD=ac387e32ea22ed84079cc45d5c78bfef9dadce37
build_dir=$(mktemp -d /tmp/blocks-service-uc04-build.XXXXXX)
old_dir=$(mktemp -d /tmp/blocks-service-uc04-old.XXXXXX)
full_output=$(mktemp /tmp/blocks-service-uc04-full.XXXXXX)
trap 'rm -rf "$build_dir" "$old_dir" "$full_output"' EXIT HUP INT TERM

echo 'BUILD — exactly one proposed 0.8.0 package build in this run'
echo 'COMMAND: archive the exact code head, then run python3 -m pip wheel --no-deps --no-build-isolation once in that clean tree'
echo 'EXPECTED: one blocks_service-0.8.0 wheel is built locally; nothing is uploaded.'
mkdir "$build_dir/source" "$build_dir/wheels"
git archive HEAD | tar -x -C "$build_dir/source"
(
  cd "$build_dir/source"
  python3 -m pip wheel --no-deps --no-build-isolation \
    --wheel-dir "$build_dir/wheels" .
)
test "$(find "$build_dir/wheels" -maxdepth 1 -name 'blocks_service-0.8.0-*.whl' | wc -l)" -eq 1
echo 'BUILD VERDICT: pass'

echo 'REGRESSION — exact old head lacks the second audience and explicit-null repair'
echo "COMMAND: run second-audience and explicit-null regressions with source at $OLD_HEAD"
echo "EXPECTED: two failures: 'Audience doesn't match' and explicit null does not raise; a pass would invalidate either regression."
git archive "$OLD_HEAD" | tar -x -C "$old_dir"
mkdir "$old_dir/regression"
cp tests/test_web_audience_identity.py "$old_dir/regression/test_web_audience_identity.py"
if (
  cd "$old_dir"
  PYTHONPATH="$old_dir/src" /home/dev/work/blocks-service/.venv/bin/python -m pytest \
    regression/test_web_audience_identity.py::test_regression_second_configured_audience_is_accepted \
    'regression/test_web_audience_identity.py::test_process_environment_distinguishes_explicit_null_from_omission[True]' -q
) >"$old_dir/result.txt" 2>&1; then
  cat "$old_dir/result.txt"
  echo 'REGRESSION VERDICT: fail — old code unexpectedly accepted the Web audience'
  exit 1
fi
cat "$old_dir/result.txt"
grep -q "Audience doesn't match" "$old_dir/result.txt"
grep -q "DID NOT RAISE" "$old_dir/result.txt"
echo 'REGRESSION VERDICT: pass — expected failure observed'

echo 'CASE 1 — legacy Android audience remains exact across restart'
echo 'COMMAND: .venv/bin/python -m pytest tests/test_web_audience_identity.py::test_legacy_single_audience_is_one_exact_entry -q'
echo 'EXPECTED: 1 passed; Android accepted before/after verifier restart, Web and third audiences rejected.'
.venv/bin/python -m pytest \
  tests/test_web_audience_identity.py::test_legacy_single_audience_is_one_exact_entry -q
echo 'CASE 1 VERDICT: pass'

echo 'CASE 2 — Android and Web converge on retained PostgreSQL identity and data'
echo 'COMMAND: run retained-identity and concurrent-first-bootstrap tests'
echo 'EXPECTED: 2 passed; both audiences return one user/tenant, one membership, and the retained Project.'
.venv/bin/python -m pytest \
  tests/test_web_audience_identity.py::test_android_and_web_reuse_existing_identity_tenant_and_project \
  tests/test_web_audience_identity.py::test_concurrent_first_bootstrap_creates_one_identity -q
echo 'CASE 2 VERDICT: pass'

echo 'CASE 3 — identity isolation and invalid signed-token paths'
echo 'COMMAND: run same-email isolation, verifier rejection matrix, and protected-request rejection'
echo 'EXPECTED: all selected checks pass; invalid tokens receive 401 at bootstrap and later reads.'
.venv/bin/python -m pytest \
  tests/test_web_audience_identity.py::test_same_email_different_subject_stays_separate_and_cannot_read_project \
  tests/test_web_audience_identity.py::test_invalid_tokens_really_fail_verification \
  tests/test_web_audience_identity.py::test_wrong_rsa_signature_really_fails_verification \
  tests/test_web_audience_identity.py::test_invalid_tokens_fail_bootstrap_and_later_reads_after_prior_success -q
echo 'CASE 3 VERDICT: pass'

echo 'CASE 4 — fail-closed configuration, aud shapes and retained azp behavior'
echo 'COMMAND: run invalid config, JSON list, aud string/array, and azp tests'
echo 'EXPECTED: all selected checks pass; explicit null, malformed/empty/wildcard/duplicate/both forms fail before serving, while omission retains the scalar.'
.venv/bin/python -m pytest \
  tests/test_web_audience_identity.py::test_invalid_or_ambiguous_audience_configuration_fails_before_serving \
  tests/test_web_audience_identity.py::test_malformed_explicit_list_does_not_fall_back_to_legacy \
  tests/test_web_audience_identity.py::test_process_environment_distinguishes_explicit_null_from_omission \
  tests/test_web_audience_identity.py::test_dotenv_distinguishes_explicit_null_from_omission \
  tests/test_web_audience_identity.py::test_process_environment_omission_preserves_legacy_scalar \
  tests/test_web_audience_identity.py::test_dotenv_omission_preserves_legacy_scalar \
  tests/test_web_audience_identity.py::test_explicit_list_is_json_and_exact \
  tests/test_web_audience_identity.py::test_audience_string_array_and_existing_azp_behavior -q
echo 'CASE 4 VERDICT: pass'

echo 'CASE 5 — staged configuration keeps Android through rollout and rollback'
echo 'COMMAND: same executable is exercised first with BLOCKS_OIDC_AUDIENCE=Android, then with BLOCKS_OIDC_AUDIENCES=[Android,Web] in Case 2.'
echo 'EXPECTED: Android passes in both; rollback restores the scalar Android setting and touches no identity/product row.'
echo 'CASE 5 VERDICT: pass'

echo 'FULL GATE — complete real-PostgreSQL repository suite, zero skips'
echo 'COMMAND: .venv/bin/python -m pytest -q'
echo 'EXPECTED: 518 passed, with no skipped/xfailed/xpassed tests.'
.venv/bin/python -m pytest -q | tee "$full_output"
grep -Eq '^518 passed in ' "$full_output"
if grep -Eq 'skipped|xfailed|xpassed' "$full_output"; then
  echo 'FULL GATE VERDICT: fail — non-passing outcome present'
  exit 1
fi
echo 'FULL GATE VERDICT: pass'

echo 'SOURCE AND AUTHORITY HASHES'
echo "CODE HEAD: $(git rev-parse HEAD)"
sha256sum \
  .env.example \
  pyproject.toml \
  src/blocks_service/config.py \
  src/blocks_service/identity.py \
  src/blocks_service/repository.py \
  tests/test_web_audience_identity.py \
  scripts/uc-04-web-and-android-identity-continuity.sh
git -C .atlas show \
  "$ATLAS_HEAD:components/arch/docs/needs/blocks-service-web-audience-support-v0_1.md" \
  | sha256sum | sed 's/  -$/  durable-need/'
git -C .atlas show \
  "$ATLAS_HEAD:components/blocks-service/tests/UC-04-web-and-android-identity-continuity.md" \
  | sha256sum | sed 's/  -$/  UC-04-authority/'

echo 'PAUSE — provider release and approved Web client provisioning'
echo 'After those estate-owned steps, use actual Android and Web Google tokens for the same account.'
echo 'Observe one retained user/tenant/Project, deny a different account, and confirm Android still works.'
echo 'Never print or store raw tokens or personal identity values.'
echo 'CASE 6 VERDICT: blocked:provider-and-estate'
echo 'OVERALL AUTOMATED VERDICT: pass; actual-token far end remains blocked:provider-and-estate'
