#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


recipe = Path(__file__).resolve().parent
supervisor = recipe / 'analysis-supervisor.py'
bash = shutil.which('bash')
if not bash:
    raise SystemExit('fixture checks require bash')


def write(path, contents, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    if executable:
        path.chmod(0o755)


def make_recipe(root, sampler):
    write(root / 'capture-owner-buffers.py', (recipe / 'capture-owner-buffers.py').read_text())
    write(root / 'sample.sh', sampler, executable=True)
    write(root / 'cgroup-snapshot.py', "#!/usr/bin/env python3\nprint('{}')\n", executable=True)


def run_case(base, name, owner, sampler=None, cap=8):
    case = base / name
    source = case / 'source'
    recipe_root = case / 'recipe'
    output = case / 'output'
    write(source / 'bin/fm-lint.sh', owner, executable=True)
    write(case / 'analysis-step.sh', 'bin/fm-lint.sh\n')
    make_recipe(recipe_root, sampler or "#!/usr/bin/env bash\nwhile :; do sleep 1; done\n")
    output.mkdir(parents=True)
    env = os.environ.copy()
    env['SOURCE_COMMIT'] = 'fixture-source'
    result = subprocess.run(
        [sys.executable, str(supervisor), name, str(cap), str(time.time() + 30),
         str(output), str(source), str(recipe_root), str(case / 'analysis-step.sh'), bash],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    outcome = json.loads((output / 'analysis-outcome.json').read_text())
    actual_exit = int((output / 'analysis-step.exit').read_text())
    assert actual_exit == result.returncode, (name, actual_exit, result.returncode)
    assert int((output / 'owner-step.exit').read_text()) == outcome['owner_step_wait_status'] if outcome['owner_step_wait_status'] is not None else True
    return case, output, result, outcome


with tempfile.TemporaryDirectory(prefix='ci-capacity-fixtures-') as temporary:
    base = Path(temporary)
    wait_for_capture = """
    retained="${TMPDIR%/owner-tmp}/owner-output-retained/fm-lint.fixture/output/shard.0.out"
    for _ in $(seq 1 200); do
      [ -f "$retained" ] && break
      sleep 0.01
    done
    test -f "$retained" || exit 98
    """
    successful_owner = f'''#!/usr/bin/env bash
set -u
d="$TMPDIR/fm-lint.fixture"
trap 'rm -rf "$d"' EXIT
mkdir -p "$d/output"
printf 'fixture-success-diagnostic\\n' > "$d/output/shard.0.out"
{wait_for_capture}
cat "$d/output/shard.0.out"
rm -rf "$d"
exit 0
'''
    case, output, result, outcome = run_case(base, 'completed', successful_owner)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr, outcome)
    assert outcome['result'] == 'complete-success', outcome
    assert (output / 'owner-output-retained/fm-lint.fixture/output/shard.0.out').read_text() == 'fixture-success-diagnostic\n'
    assert not outcome['collection_errors'], outcome['collection_errors']

    fatal_owner = successful_owner.replace('cat "$d/output/shard.0.out"', 'exit 23').replace('exit 0', 'exit 0')
    case, output, result, outcome = run_case(base, 'fatal', fatal_owner)
    assert result.returncode == 23, (result.returncode, result.stderr)
    assert outcome['result'] == 'complete-nonzero-or-fatal', outcome
    assert (output / 'owner-step.exit').read_text() == '23\n'

    censored_owner = f'''#!/usr/bin/env bash
set -u
d="$TMPDIR/fm-lint.fixture"
mkdir -p "$d/output"
printf 'fixture-before-term\\n' > "$d/output/shard.0.out"
{wait_for_capture}
on_term() {{
  printf 'fixture-final-before-unlink\\n' >> "$d/output/shard.0.out"
  rm -rf "$d"
  exit 143
}}
trap on_term TERM
while :; do sleep 1; done
'''
    case, output, result, outcome = run_case(base, 'censored', censored_owner, cap=4)
    assert result.returncode == 124, (result.returncode, result.stderr)
    assert outcome['result'] == 'analysis-deadline', outcome
    assert outcome['owner_step_wait_status'] == 143, outcome
    captured = (output / 'owner-output-retained/fm-lint.fixture/output/shard.0.out').read_text()
    assert captured == 'fixture-before-term\nfixture-final-before-unlink\n', (
        captured, outcome, result.stdout, result.stderr,
        (output / 'capture-reader.stdout.txt').read_text(),
        (output / 'owner-output-retained/capture-summary.json').read_text(),
        (output / 'owner-step.exit').read_text(),
    )
    assert not (Path(outcome['owner_tmp']) / 'fm-lint.fixture').exists()
    censored_owner_status = outcome['owner_step_wait_status']

    marker_owner = '''#!/usr/bin/env bash
touch "$TMPDIR/owner-started"
exit 0
'''
    case, output, result, outcome = run_case(
        base, 'observer-failure', marker_owner,
        sampler='#!/usr/bin/env bash\nexit 7\n',
    )
    assert result.returncode == 125, (result.returncode, result.stderr)
    assert outcome['result'] == 'observer-failed-before-analysis', outcome
    assert not (Path(outcome['owner_tmp']) / 'owner-started').exists()

    summary = {
        'fixture_only': True,
        'host_os': os.uname().sysname,
        'completed': 'distinctive buffered output retained after owner cleanup',
        'censored': 'final diagnostic appended on TERM retained after unlink; supervisor exit 124',
        'fatal': 'owner exit 23 preserved separately from wrapper exit',
        'observer_failure': 'sampler exit 7 prevents owner start; wrapper exit 125',
        'censored_owner_step_wait_status': censored_owner_status,
        'native_capacity_evidence': False,
    }
    print(json.dumps(summary, indent=2))
