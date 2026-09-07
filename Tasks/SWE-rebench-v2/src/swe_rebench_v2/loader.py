from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from swe_rebench_v2.instance_spec import Record


class LocalDatasetLoader:
    def __init__(self, dataset_path: Path) -> None:
        self.dataset_path = Path(dataset_path).resolve()
        if not self.dataset_path.exists():
            raise FileNotFoundError(f"Dataset path does not exist: {self.dataset_path}")

    @property
    def source(self) -> str:
        return str(self.dataset_path)

    def _parquet_files(self) -> list[Path]:
        if self.dataset_path.is_file() and self.dataset_path.suffix == ".parquet":
            return [self.dataset_path]
        if self.dataset_path.is_dir():
            files = sorted(self.dataset_path.glob("data/*.parquet"))
            if not files:
                files = sorted(self.dataset_path.glob("*.parquet"))
            return files
        return []

    def _iter_raw(self) -> Iterator[Record]:
        if self.dataset_path.is_file() and self.dataset_path.suffix == ".json":
            payload = json.loads(self.dataset_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("JSON dataset root must be a list")
            for index, record in enumerate(payload):
                if not isinstance(record, dict):
                    raise TypeError(f"JSON dataset row {index} is not an object")
                yield record
            return

        parquet_files = self._parquet_files()
        if not parquet_files:
            raise ValueError(
                f"Expected a JSON file, Parquet file, or dataset directory: {self.dataset_path}"
            )

        try:
            from datasets import load_dataset
        except ImportError as exc:
            raise RuntimeError(
                "Reading Parquet requires the adapter dependencies; run it with uv"
            ) from exc

        dataset = load_dataset(
            "parquet",
            data_files=[str(path) for path in parquet_files],
            split="train",
            streaming=True,
        )
        for record in dataset:
            yield dict(record)

    def select(
        self,
        *,
        instance_ids: list[str] | None = None,
        languages: set[str] | None = None,
        limit: int | None = None,
    ) -> Iterator[Record]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")

        requested = list(dict.fromkeys(instance_ids or []))
        requested_set = set(requested)
        selected_by_id: dict[str, Record] = {}
        seen_ids: set[str] = set()
        emitted = 0

        for record in self._iter_raw():
            instance_id = record.get("instance_id")
            if not isinstance(instance_id, str) or not instance_id:
                raise ValueError("Dataset contains a row without instance_id")
            if instance_id in seen_ids:
                raise ValueError(
                    f"Dataset contains duplicate instance_id: {instance_id}"
                )
            seen_ids.add(instance_id)

            if requested:
                if instance_id in requested_set:
                    selected_by_id[instance_id] = record
                continue

            language = record.get("language")
            normalized_language = (
                language.strip().lower()
                if isinstance(language, str) and language.strip()
                else None
            )
            if languages and normalized_language not in languages:
                continue

            yield record
            emitted += 1
            if limit is not None and emitted >= limit:
                return

        if requested:
            missing = [
                instance_id
                for instance_id in requested
                if instance_id not in selected_by_id
            ]
            if missing:
                raise ValueError(f"Instance IDs not found: {', '.join(missing)}")
            for instance_id in requested[:limit]:
                yield selected_by_id[instance_id]
