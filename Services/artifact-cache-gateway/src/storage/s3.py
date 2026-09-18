from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping
from urllib.parse import quote, unquote, urlsplit

from .base import CacheEntry, StorageError
from .gpfs import FileStorage
from .inventory import CacheObject, InventoryListing
from .request_stats import RequestStatsSession

_MAX_METADATA_BYTES = 64 * 1024
_S3_MAX_POOL_CONNECTIONS = 64
_S3_READ_ATTEMPTS = 3
_S3_READ_RETRY_BASE_SECONDS = 0.05
_S3_TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_S3_TRANSIENT_ERROR_CODES = frozenset(
    {
        "InternalError",
        "RequestTimeout",
        "RequestTimeoutException",
        "ServiceUnavailable",
        "SlowDown",
        "Throttling",
        "ThrottlingException",
    }
)


@dataclass(frozen=True)
class S3Settings:
    endpoint: str
    region: str
    bucket: str
    prefix: str
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "S3Settings":
        values = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value:
                raise StorageError(f"missing required S3 setting: {name}")
            return value

        endpoint = required("ARTIFACT_CACHE_S3_ENDPOINT").rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise StorageError("ARTIFACT_CACHE_S3_ENDPOINT must be an HTTP(S) URL")
        if parsed.path or parsed.query or parsed.fragment:
            raise StorageError("ARTIFACT_CACHE_S3_ENDPOINT must not contain a path/query/fragment")

        prefix = required("ARTIFACT_CACHE_S3_PREFIX").strip("/")
        if not prefix:
            raise StorageError("ARTIFACT_CACHE_S3_PREFIX must not be empty")
        if "\\" in prefix or "\x00" in prefix or any(
            part in {".", ".."} for part in prefix.split("/")
        ):
            raise StorageError("ARTIFACT_CACHE_S3_PREFIX contains invalid path segments")

        return cls(
            endpoint=endpoint,
            region=required("ARTIFACT_CACHE_S3_REGION"),
            bucket=required("ARTIFACT_CACHE_S3_BUCKET"),
            prefix=prefix,
            access_key_id=required("ARTIFACT_CACHE_S3_ACCESS_KEY_ID"),
            secret_access_key=required("ARTIFACT_CACHE_S3_SECRET_ACCESS_KEY"),
            session_token=(values.get("ARTIFACT_CACHE_S3_SESSION_TOKEN") or "").strip() or None,
        )


class S3Storage:
    """S3-authoritative storage with local files used only for fetch staging."""

    def __init__(
        self,
        settings: S3Settings,
        work_dir: Path,
        *,
        client: Any | None = None,
        transfer_config: Any | None = None,
        stale_temp_seconds: float = 24 * 60 * 60,
    ):
        self.settings = settings
        self.work_dir = work_dir
        self.tmp = work_dir / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)

        if client is None:
            try:
                import boto3
                from boto3.s3.transfer import TransferConfig
                from botocore.config import Config
            except ImportError as exc:
                raise StorageError("S3 mode requires boto3 to be installed") from exc

            botocore_config = Config(
                region_name=settings.region,
                signature_version="s3v4",
                connect_timeout=10,
                read_timeout=120,
                max_pool_connections=_S3_MAX_POOL_CONNECTIONS,
                retries={"max_attempts": 5, "mode": "standard"},
                proxies={},
                s3={"addressing_style": "path"},
            )
            client = boto3.client(
                "s3",
                endpoint_url=settings.endpoint,
                region_name=settings.region,
                aws_access_key_id=settings.access_key_id,
                aws_secret_access_key=settings.secret_access_key,
                aws_session_token=settings.session_token,
                config=botocore_config,
            )
            if transfer_config is None:
                transfer_config = TransferConfig(
                    multipart_threshold=8 * 1024 * 1024,
                    multipart_chunksize=8 * 1024 * 1024,
                    max_concurrency=4,
                    use_threads=True,
                )

        self.client = client
        self.transfer_config = transfer_config
        self.cleanup_stale_temp(stale_temp_seconds)
        self.verify_bucket()

    def verify_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.settings.bucket)
        except Exception as exc:
            raise StorageError("S3 bucket authentication or connectivity check failed") from exc

    @staticmethod
    def url_key(url: str) -> str:
        return FileStorage.url_key(url)

    def _key(self, suffix: str) -> str:
        return f"{self.settings.prefix}/{suffix.lstrip('/')}"

    def blob_key(self, digest: str) -> str:
        return self._key(f"blobs/sha256/{digest[:2]}/{digest}")

    def metadata_key(self, url: str) -> str:
        key = self.url_key(url)
        return self._key(f"metadata/url/{key[:2]}/{key}.json")

    @staticmethod
    def _inventory_component(value: str) -> str:
        return quote(value, safe="")

    def inventory_prefix(self, components: tuple[str, ...] = ()) -> str:
        suffix = "inventory/v1/"
        if components:
            suffix += "/".join(
                self._inventory_component(value) for value in components
            ) + "/"
        return self._key(suffix)

    def inventory_key(self, record: "CacheObject") -> str:
        groups = (
            record.ecosystem,
            record.source,
            record.package,
            record.version,
        )
        return f"{self.inventory_prefix(groups)}{record.object_id}.json"

    def request_stats_prefix(self) -> str:
        return self._key("metrics/request-sessions/v1/")

    def request_stats_key(self, session_id: str) -> str:
        return f"{self.request_stats_prefix()}{session_id}.json"

    @staticmethod
    def _is_not_found(exc: BaseException) -> bool:
        response = getattr(exc, "response", {})
        error = response.get("Error", {}) if isinstance(response, dict) else {}
        metadata = (
            response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
        )
        code = str(error.get("Code", ""))
        status = metadata.get("HTTPStatusCode")
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

    @classmethod
    def _is_transient_read_error(cls, exc: BaseException) -> bool:
        response = getattr(exc, "response", {})
        if not isinstance(response, dict) or not response:
            return True
        error = response.get("Error", {})
        metadata = response.get("ResponseMetadata", {})
        code = str(error.get("Code", "")) if isinstance(error, dict) else ""
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        return (
            code in _S3_TRANSIENT_ERROR_CODES
            or status in _S3_TRANSIENT_STATUS_CODES
        )

    def _get_object_with_retry(
        self,
        *,
        key: str,
        byte_range: str | None = None,
    ) -> dict[str, Any]:
        kwargs = {"Bucket": self.settings.bucket, "Key": key}
        if byte_range is not None:
            kwargs["Range"] = byte_range
        for attempt in range(_S3_READ_ATTEMPTS):
            try:
                return self.client.get_object(**kwargs)
            except Exception as exc:
                if (
                    attempt + 1 >= _S3_READ_ATTEMPTS
                    or not self._is_transient_read_error(exc)
                ):
                    raise
                time.sleep(_S3_READ_RETRY_BASE_SECONDS * (2**attempt))
        raise AssertionError("unreachable")

    def create_temp(self) -> tuple[BinaryIO, Path]:
        descriptor, name = tempfile.mkstemp(prefix="fetch-", dir=self.tmp)
        return os.fdopen(descriptor, "wb"), Path(name)

    def cleanup_stale_temp(self, max_age_seconds: float) -> int:
        cutoff = time.time() - max_age_seconds
        removed = 0
        for path in self.tmp.glob("fetch-*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def load(self, url: str) -> CacheEntry | None:
        try:
            response = self._get_object_with_retry(
                key=self.metadata_key(url),
            )
        except Exception as exc:
            if self._is_not_found(exc):
                return None
            raise StorageError("failed to read S3 metadata") from exc

        body = response["Body"]
        try:
            document = body.read(_MAX_METADATA_BYTES + 1)
        finally:
            body.close()
        if len(document) > _MAX_METADATA_BYTES:
            raise StorageError("S3 metadata exceeds the size limit")
        try:
            entry = CacheEntry(**json.loads(document))
        except (json.JSONDecodeError, TypeError) as exc:
            raise StorageError("malformed S3 metadata") from exc
        if entry.url != url:
            raise StorageError("S3 metadata URL mismatch")
        entry.validate()
        return entry

    @contextmanager
    def open_blob(
        self, entry: CacheEntry, start: int = 0, end: int | None = None
    ):
        entry.validate()
        if start < 0 or start >= entry.size:
            raise StorageError("invalid S3 Range start")
        if end is None:
            end = entry.size - 1
        if end < start or end >= entry.size:
            raise StorageError("invalid S3 Range end")
        expected = max(0, end - start + 1)
        try:
            response = self._get_object_with_retry(
                key=self.blob_key(entry.digest),
                byte_range=f"bytes={start}-{end}",
            )
        except Exception as exc:
            raise StorageError("failed to read S3 blob") from exc
        if int(response.get("ContentLength", expected)) != expected:
            response["Body"].close()
            raise StorageError("S3 Range response length mismatch")
        body = response["Body"]
        try:
            yield body
        finally:
            body.close()

    def publish(self, entry: CacheEntry, temp_path: Path) -> CacheEntry:
        entry.validate()
        blob_key = self.blob_key(entry.digest)
        try:
            should_upload = False
            try:
                existing = self.client.head_object(
                    Bucket=self.settings.bucket,
                    Key=blob_key,
                )
            except Exception as exc:
                if self._is_not_found(exc):
                    should_upload = True
                else:
                    raise StorageError("failed to check S3 blob") from exc

            if should_upload:
                kwargs = {
                    "Filename": str(temp_path),
                    "Bucket": self.settings.bucket,
                    "Key": blob_key,
                    "ExtraArgs": {
                        "ContentType": entry.content_type,
                        "Metadata": {"sha256": entry.digest},
                    },
                }
                if self.transfer_config is not None:
                    kwargs["Config"] = self.transfer_config
                self.client.upload_file(**kwargs)
                existing = self.client.head_object(
                    Bucket=self.settings.bucket,
                    Key=blob_key,
                )

            if int(existing["ContentLength"]) != entry.size:
                raise StorageError("S3 blob size does not match the recorded content digest")
            self.write_metadata(entry)
            return entry
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("failed to publish S3 blob") from exc
        finally:
            temp_path.unlink(missing_ok=True)

    def touch(self, entry: CacheEntry, fetched_at: float) -> CacheEntry:
        updated = replace(entry, fetched_at=fetched_at)
        self.write_metadata(updated)
        return updated

    def write_metadata(self, entry: CacheEntry) -> None:
        document = json.dumps(
            asdict(entry), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(document) > _MAX_METADATA_BYTES:
            raise StorageError("S3 metadata exceeds the size limit")
        try:
            self.client.put_object(
                Bucket=self.settings.bucket,
                Key=self.metadata_key(entry.url),
                Body=document,
                ContentType="application/json",
            )
        except Exception as exc:
            raise StorageError("failed to write S3 metadata") from exc

    def ensure_inventory(
        self, record: "CacheObject", *, replace_existing: bool = False
    ) -> None:
        if not isinstance(record, CacheObject):
            raise StorageError("invalid inventory record type")
        document = json.dumps(
            record.document(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(document) > _MAX_METADATA_BYTES:
            raise StorageError("S3 inventory document exceeds the size limit")
        key = self.inventory_key(record)
        if not replace_existing:
            try:
                self.client.head_object(Bucket=self.settings.bucket, Key=key)
                return
            except Exception as exc:
                if not self._is_not_found(exc):
                    raise StorageError("failed to check S3 inventory") from exc
        try:
            self.client.put_object(
                Bucket=self.settings.bucket,
                Key=key,
                Body=document,
                ContentType="application/json",
            )
        except Exception as exc:
            raise StorageError("failed to write S3 inventory") from exc

    def _read_inventory(self, key: str) -> "CacheObject":
        try:
            response = self.client.get_object(
                Bucket=self.settings.bucket,
                Key=key,
            )
        except Exception as exc:
            raise StorageError("failed to read S3 inventory") from exc
        body = response["Body"]
        try:
            document = body.read(_MAX_METADATA_BYTES + 1)
        finally:
            body.close()
        if len(document) > _MAX_METADATA_BYTES:
            raise StorageError("S3 inventory document exceeds the size limit")
        try:
            return CacheObject.from_document(json.loads(document))
        except (json.JSONDecodeError, StorageError) as exc:
            raise StorageError("corrupted S3 inventory document") from exc

    def list_inventory(
        self, components: tuple[str, ...], cursor: str | None, limit: int
    ) -> "InventoryListing":
        if len(components) > 4:
            raise StorageError("invalid inventory hierarchy")
        prefix = self.inventory_prefix(components)
        request: dict[str, object] = {
            "Bucket": self.settings.bucket,
            "Prefix": prefix,
            "MaxKeys": limit,
        }
        if len(components) < 4:
            request["Delimiter"] = "/"
        if cursor:
            request["ContinuationToken"] = cursor
        try:
            response = self.client.list_objects_v2(**request)
        except Exception as exc:
            raise StorageError("failed to list S3 inventory") from exc
        next_cursor = response.get("NextContinuationToken")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise StorageError("malformed S3 inventory cursor")
        if len(components) < 4:
            children = []
            for item in response.get("CommonPrefixes", []):
                child_prefix = item.get("Prefix") if isinstance(item, dict) else None
                if not isinstance(child_prefix, str) or not child_prefix.startswith(
                    prefix
                ):
                    raise StorageError("malformed S3 inventory prefix")
                encoded = child_prefix[len(prefix) :].rstrip("/")
                if not encoded or "/" in encoded:
                    raise StorageError("malformed S3 inventory hierarchy")
                children.append(unquote(encoded))
            return InventoryListing(
                children=tuple(children), next_cursor=next_cursor
            )
        objects = []
        for item in response.get("Contents", []):
            key = item.get("Key") if isinstance(item, dict) else None
            if not isinstance(key, str) or not key.startswith(prefix):
                raise StorageError("malformed S3 inventory object key")
            objects.append(self._read_inventory(key))
        return InventoryListing(objects=tuple(objects), next_cursor=next_cursor)

    def iter_entries(self) -> Iterator[CacheEntry]:
        prefix = self._key("metadata/url/")
        cursor: str | None = None
        while True:
            request: dict[str, object] = {
                "Bucket": self.settings.bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if cursor:
                request["ContinuationToken"] = cursor
            try:
                response = self.client.list_objects_v2(**request)
            except Exception as exc:
                raise StorageError("failed to list S3 metadata") from exc
            for item in response.get("Contents", []):
                key = item.get("Key") if isinstance(item, dict) else None
                if not isinstance(key, str):
                    continue
                try:
                    result = self.client.get_object(
                        Bucket=self.settings.bucket,
                        Key=key,
                    )
                    body = result["Body"]
                    try:
                        document = body.read(_MAX_METADATA_BYTES + 1)
                    finally:
                        body.close()
                    if len(document) > _MAX_METADATA_BYTES:
                        continue
                    entry = CacheEntry(**json.loads(document))
                    entry.validate()
                except Exception:
                    continue
                yield entry
            cursor = response.get("NextContinuationToken")
            if not isinstance(cursor, str) or not cursor:
                break

    def load_request_stats(self) -> tuple["RequestStatsSession", ...]:
        prefix = self.request_stats_prefix()
        cursor: str | None = None
        sessions = []
        while True:
            request: dict[str, object] = {
                "Bucket": self.settings.bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if cursor:
                request["ContinuationToken"] = cursor
            try:
                response = self.client.list_objects_v2(**request)
                for item in response.get("Contents", []):
                    key = item.get("Key") if isinstance(item, dict) else None
                    if not isinstance(key, str) or not key.startswith(prefix):
                        raise StorageError("invalid S3 request stats key")
                    result = self.client.get_object(
                        Bucket=self.settings.bucket, Key=key
                    )
                    body = result["Body"]
                    try:
                        document = body.read(_MAX_METADATA_BYTES + 1)
                    finally:
                        body.close()
                    if len(document) > _MAX_METADATA_BYTES:
                        raise StorageError("S3 request stats document exceeds the size limit")
                    sessions.append(
                        RequestStatsSession.from_document(json.loads(document))
                    )
            except StorageError:
                raise
            except Exception as exc:
                raise StorageError("failed to read S3 request stats") from exc
            cursor = response.get("NextContinuationToken")
            if not isinstance(cursor, str) or not cursor:
                break
        return tuple(sessions)

    def save_request_stats(self, session: "RequestStatsSession") -> None:
        document = json.dumps(
            session.document(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(document) > _MAX_METADATA_BYTES:
            raise StorageError("S3 request stats document exceeds the size limit")
        try:
            self.client.put_object(
                Bucket=self.settings.bucket,
                Key=self.request_stats_key(session.session_id),
                Body=document,
                ContentType="application/json",
            )
        except Exception as exc:
            raise StorageError("failed to write S3 request stats") from exc
