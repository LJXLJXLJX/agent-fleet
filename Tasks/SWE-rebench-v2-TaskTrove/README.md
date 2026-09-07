# Third-party TaskTrove SWE-rebench-V2 Integration

This optional integration uses a dataset published by TaskTrove. It is not the
canonical Agent Fleet SWE-rebench-V2 source. The dataset contains a subset of
the official records and includes repository setup commands in agent-visible
instructions.

Default source:

- Harbor Hub: https://hub.harborframework.com/datasets/openthoughts/tasktrove-swe-rebench-v2-patched-oracle

The default is the oracle subset. The underlying Harbor CLI invocation uses
`--dataset openthoughts/tasktrove-swe-rebench-v2-patched-oracle`.

## Unified entry

Run through the normal Harbor zellij entrypoint:

```bash
DATASET_NAME=openthoughts/tasktrove-swe-rebench-v2-patched-oracle \
bash Agents/utils/common/Harbor/start.sh --detach
```

## QZ final-image adaptation

The registry task archive asks the agent to clone, checkout, and install the
repository. A QZ run backed by the original final task images must not expose
that setup block to the agent again. Use the generic repository-image producer
with a local materialization of the selected Harbor tasks and an explicit image
catalog exported from the benchmark metadata:

```bash
cd Agents/utils/common/Harbor

python qz_repository_environment_plan.py \
  --dataset-root /path/to/materialized/swe-rebench-v2/tasks \
  --task-list /path/to/selected-tasks.txt \
  --image-catalog /path/to/swe-rebench-v2-images.jsonl \
  --output /tmp/swe-rebench-v2-environment-plan.json

python qz_template_mapping.py \
  --dataset-root /path/to/materialized/swe-rebench-v2/tasks \
  --benchmark swe-rebench-v2 \
  --task-list /path/to/selected-tasks.txt \
  --environment-plan-file /tmp/swe-rebench-v2-environment-plan.json \
  --output /tmp/swe-rebench-v2-qz-templates.json
```

The catalog defaults to the benchmark's `instance_id`, `repo`, `base_commit`,
and `image_name` fields. The task ID selects one record; repository and commit
are then checked against the setup block, so shared base commits cannot select
the wrong image. This adapter is not keyed to the dataset name: another
repository benchmark with the same strict setup block can use it directly or
select other catalog field names. The setup block's absolute `cd` target is
preserved as the QZ task workdir. Inventory is read-only; template
materialization remains an explicit, separate operation.
