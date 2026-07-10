# RL Rollout Tests

Run the fast in-repository suite from the Agent Fleet repository root:

```bash
python3 -m unittest discover -s Agents/utils/rl/tests -v
```

`test_async_trial_submit_smoke.py` starts the real rollout HTTP handler and uses
a real SQLite registry plus real temporary queue directories. It replaces only
zellij startup, then invokes `async_batch_submit_driver.py` in a fresh Python
process to submit and verify a 32-trial batch.

The same driver can target a running rollout service explicitly:

```bash
python3 Agents/utils/rl/tests/async_batch_submit_driver.py \
  --harbor-url http://127.0.0.1:18081 \
  --dataset-name seta \
  --ray-submission-id async-submit-smoke \
  --task-id 1 \
  --queue-root /path/to/rl-queue/jobs
```

Future long-running, high-load, real Harbor, and Proxy validation assets also
belong under this directory, but should use explicit manual entry points rather
than `test_*.py` when they are unsuitable for the default discovery command.
