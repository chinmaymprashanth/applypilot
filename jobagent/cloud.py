"""Running ApplyPilot in AWS: shared state in S3, a lock, secrets in Parameter Store, the daily email.

How the pieces fit:
  - data/ (database, job descriptions, caches, your master resume and answers) travels as ONE compressed archive,
    s3://<bucket>/state/data.tar.gz. One object means an upload is all-or-nothing, and S3 versioning keeps the
    previous copies as backups.
  - Secrets never go in the archive: the Anthropic key and the Gmail token live in Parameter Store as encrypted
    SecureString values, and ECS hands them to the container as environment variables.
  - A lock object (state/lock.json), created with an S3 conditional write ("only if it doesn't exist"), makes sure
    the cloud run and `apply` on your Mac never change the data at the same time.
  - Your Mac remembers which archive version it last had (data/.cloud_sync.json), so `apply` downloads only when
    the cloud has something newer, and never overwrites work it hasn't uploaded yet.
"""

from __future__ import annotations

import json
import os
import socket
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BUCKET_ENV = "APPLYPILOT_BUCKET"
STATE_KEY = "state/data.tar.gz"
LOCK_KEY = "state/lock.json"
SYNC_FILE = ".cloud_sync.json"
# A lock older than this belongs to a run that died without cleaning up; it may be taken over.
LOCK_STALE_HOURS = 6
# Parameter Store names (encrypted SecureString values).
PARAM_ANTHROPIC = "/applypilot/anthropic_api_key"
PARAM_GMAIL = "/applypilot/gmail_token"
# Never packed into the archive: secrets (Parameter Store holds them) and this machine's own sync record.
NOT_UPLOADED = {"gmail_token.json", SYNC_FILE, ".env"}


def region() -> str:
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"


def bucket_name() -> str | None:
    return os.getenv(BUCKET_ENV) or None


class LockHeld(RuntimeError):
    def __init__(self, holder: dict):
        self.holder = holder
        super().__init__(f"{holder.get('owner', 'another run')} has been working since {holder.get('since', '?')}")


@dataclass
class Store:
    bucket: str
    s3: object = None

    def __post_init__(self):
        if self.s3 is None:
            import boto3

            self.s3 = boto3.client("s3", region_name=region())

    # ---- the data archive ----------------------------------------------------------------------------------
    def remote_version(self) -> str | None:
        """The archive's ETag (a fingerprint S3 gives every upload), or None if nothing is uploaded yet."""
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=STATE_KEY)["ETag"]
        except Exception as e:  # noqa: BLE001
            if _status(e) == 404:
                return None
            raise

    def push(self, data_dir: Path) -> str:
        """Upload data/ as one archive; returns the new version and records it as this machine's."""
        with tempfile.TemporaryFile() as f:
            with tarfile.open(fileobj=f, mode="w:gz") as tar:
                for path in sorted(data_dir.rglob("*")):
                    rel = path.relative_to(data_dir)
                    if path.is_file() and rel.parts[0] not in NOT_UPLOADED and not path.name.startswith("~$"):
                        tar.add(path, arcname=str(rel))
            f.seek(0)
            self.s3.upload_fileobj(f, self.bucket, STATE_KEY)
        version = self.remote_version()
        _write_sync(data_dir, version, dirty=False)
        return version

    def pull(self, data_dir: Path) -> str | None:
        """Download the archive into data/ (files in it replace local ones). Returns its version."""
        version = self.remote_version()
        if version is None:
            return None
        with tempfile.TemporaryFile() as f:
            self.s3.download_fileobj(self.bucket, STATE_KEY, f)
            f.seek(0)
            with tarfile.open(fileobj=f, mode="r:gz") as tar:
                tar.extractall(data_dir, filter="data")  # "data" filter: no paths outside data/, no links
        _write_sync(data_dir, version, dirty=False)
        return version

    # ---- the lock --------------------------------------------------------------------------------------------
    def lock(self, owner: str) -> None:
        """Take the lock or raise LockHeld. A lock older than LOCK_STALE_HOURS is taken over."""
        body = json.dumps({"owner": owner, "since": _now()})
        for _ in range(2):
            try:
                self.s3.put_object(Bucket=self.bucket, Key=LOCK_KEY, Body=body.encode(), IfNoneMatch="*")
                return
            except Exception as e:  # noqa: BLE001
                if _status(e) not in (409, 412):  # 412: it already exists (409: a simultaneous write)
                    raise
            holder = self.lock_holder() or {}
            since = holder.get("since")
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(since)).total_seconds() / 3600 if since else 99
            if age < LOCK_STALE_HOURS:
                raise LockHeld(holder)
            self.unlock()
        raise LockHeld(self.lock_holder() or {})

    def lock_holder(self) -> dict | None:
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=LOCK_KEY)["Body"].read())
        except Exception as e:  # noqa: BLE001
            if _status(e) == 404:
                return None
            raise

    def unlock(self) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=LOCK_KEY)

    # ---- small objects (daily summaries, "done" markers) ---------------------------------------------------
    def put_text(self, key: str, text: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=text.encode(), ContentType="text/plain; charset=utf-8")

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as e:  # noqa: BLE001
            if _status(e) == 404:
                return False
            raise


def owner_name() -> str:
    return "the AWS daily run" if os.getenv("ECS_CONTAINER_METADATA_URI_V4") else f"apply on {socket.gethostname()}"


# ---- this machine's sync record ------------------------------------------------------------------------------
def read_sync(data_dir: Path) -> dict:
    path = data_dir / SYNC_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def _write_sync(data_dir: Path, version: str | None, dirty: bool) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / SYNC_FILE).write_text(json.dumps({"version": version, "dirty": dirty, "at": _now()}))


def mark_dirty(data_dir: Path) -> None:
    """Local changes not uploaded yet (set when apply starts, cleared by push)."""
    _write_sync(data_dir, read_sync(data_dir).get("version"), dirty=True)


def backup_local(data_dir: Path) -> Path:
    """Keep a copy of local data before replacing it, so nothing unuploaded is ever lost silently."""
    dest = data_dir.parent / f"data_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz"
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(data_dir, arcname="data")
    return dest


# ---- secrets (Parameter Store) --------------------------------------------------------------------------------
def put_secrets(values: dict[str, str], ssm=None) -> list[str]:
    """Store each value as an encrypted SecureString. Returns the names written (never the values)."""
    if ssm is None:
        import boto3

        ssm = boto3.client("ssm", region_name=region())
    for name, value in values.items():
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True, Tier="Standard")
    return list(values)


# ---- the daily email (SES) --------------------------------------------------------------------------------------
def send_email(to: str, subject: str, text: str, ses=None) -> None:
    if ses is None:
        import boto3

        ses = boto3.client("sesv2", region_name=region())
    ses.send_email(FromEmailAddress=to, Destination={"ToAddresses": [to]},
                   Content={"Simple": {"Subject": {"Data": subject[:200]}, "Body": {"Text": {"Data": text}}}})


def wait_for_lock(store: Store, owner: str, minutes: int, poll_seconds: int = 120, echo=print) -> None:
    """The cloud run waits (rather than fails) while you're applying, up to `minutes`."""
    deadline = time.time() + minutes * 60
    while True:
        try:
            store.lock(owner)
            return
        except LockHeld as e:
            if time.time() > deadline:
                raise
            echo(f"Waiting: {e}")
            time.sleep(poll_seconds)


def _status(e: Exception) -> int | None:
    resp = getattr(e, "response", None) or {}
    code = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if code is None and resp.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
        return 404
    return code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["Store", "LockHeld", "bucket_name", "owner_name", "read_sync", "mark_dirty", "backup_local",
           "put_secrets", "send_email", "wait_for_lock", "PARAM_ANTHROPIC", "PARAM_GMAIL"]
