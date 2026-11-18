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
import sqlite3
import socket
import tarfile
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
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


# Files you edit yourself on the Mac. If one changed since the last sync, your copy wins over the cloud's.
YOUR_FILES = ("master_resume.yaml", "profile.yaml", "answers.yaml", "gmail_credentials.json")


def edited_since_sync(data_dir: Path) -> dict[str, bytes]:
    """{file name: contents} for your own files changed on this machine after the last upload or download."""
    at = read_sync(data_dir).get("at")
    if not at:
        return {}
    since = datetime.fromisoformat(at).timestamp()
    return {name: (data_dir / name).read_bytes() for name in YOUR_FILES
            if (data_dir / name).exists() and (data_dir / name).stat().st_mtime > since}


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
    def remote_head(self) -> tuple[str | None, str | None]:
        """(ETag, VersionId) of the archive: a fingerprint of the upload, and the id S3 keeps it under (versioning
        keeps old uploads, which is what lets two changed copies be merged against the one they both started from)."""
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=STATE_KEY)
            return head["ETag"], head.get("VersionId")
        except Exception as e:  # noqa: BLE001
            if _status(e) == 404:
                return None, None
            raise

    def remote_version(self) -> str | None:
        return self.remote_head()[0]

    def download_version(self, version_id: str, dest: Path) -> Path:
        """Save one earlier upload of the archive (by its VersionId) to dest; returns dest."""
        obj = self.s3.get_object(Bucket=self.bucket, Key=STATE_KEY, VersionId=version_id)
        dest.write_bytes(obj["Body"].read())
        return dest

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
        version, version_id = self.remote_head()
        _write_sync(data_dir, version, dirty=False, version_id=version_id)
        return version

    def pull(self, data_dir: Path) -> str | None:
        """Download the archive into data/ (files in it replace local ones). Returns its version."""
        version, version_id = self.remote_head()
        if version is None:
            return None
        with tempfile.TemporaryFile() as f:
            self.s3.download_fileobj(self.bucket, STATE_KEY, f)
            f.seek(0)
            with tarfile.open(fileobj=f, mode="r:gz") as tar:
                tar.extractall(data_dir, filter="data")  # "data" filter: no paths outside data/, no links
        _write_sync(data_dir, version, dirty=False, version_id=version_id)
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


def emergency_stop(store: "Store", data_dir: Path, pulled: bool, locked: bool, exit=os._exit) -> None:
    """AWS is stopping the task (Spot taking the computer back, or a manual stop): it gets SIGTERM and then, after
    the task's stop timeout, a hard kill. Waiting for the run's helper threads can take longer than that, so save
    straight away: upload the data as last saved on disk, release the lock, and exit."""
    try:
        if pulled:  # only data that started from your real copy is ever uploaded
            store.push(data_dir)
    finally:
        try:
            if locked:
                store.unlock()
        finally:
            exit(143)


def owner_name() -> str:
    return "the AWS daily run" if os.getenv("ECS_CONTAINER_METADATA_URI_V4") else f"apply on {socket.gethostname()}"


# ---- this machine's sync record ------------------------------------------------------------------------------
def read_sync(data_dir: Path) -> dict:
    path = data_dir / SYNC_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def _write_sync(data_dir: Path, version: str | None, dirty: bool, version_id: str | None = None,
                at: str | None = None) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / SYNC_FILE).write_text(json.dumps(
        {"version": version, "version_id": version_id, "dirty": dirty, "at": at or _now()}))


def mark_dirty(data_dir: Path) -> None:
    """Local changes not uploaded yet (set by every save on the Mac, cleared by push). Keeps the record of which
    upload this copy started from, so the next download can merge instead of overwrite."""
    old = read_sync(data_dir)
    if old.get("dirty"):
        return
    _write_sync(data_dir, old.get("version"), dirty=True, version_id=old.get("version_id"), at=old.get("at"))


def mark_dirty_if_synced(data_dir: Path) -> None:
    """Called after every save. Only on a machine that syncs with AWS (never inside the AWS run itself)."""
    if os.getenv(BUCKET_ENV) and not os.getenv("ECS_CONTAINER_METADATA_URI_V4") and (data_dir / SYNC_FILE).exists():
        mark_dirty(data_dir)


# ---- merging two changed copies -------------------------------------------------------------------------------------
@dataclass
class MergeReport:
    yours: int = 0                                  # records changed on this machine and kept through the download
    conflicts: list[str] = field(default_factory=list)  # "company - title: field" where both sides changed it


def read_db(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """(jobs by id, boards by "ats|token") straight from a jobagent.db file."""
    with closing(sqlite3.connect(path)) as con:
        jobs = {jid: json.loads(d) for jid, d in con.execute("SELECT id, data FROM jobs")}
        boards = {f"{a}|{t}": json.loads(d) for a, t, d in con.execute("SELECT ats, token, data FROM boards")}
    return jobs, boards


def write_db(path: Path, jobs: dict[str, dict], boards: dict[str, dict]) -> None:
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("DELETE FROM jobs")
        con.executemany("INSERT INTO jobs VALUES (?, ?)",
                        [(jid, json.dumps(r, default=str, ensure_ascii=False)) for jid, r in jobs.items()])
        con.execute("DELETE FROM boards")
        con.executemany("INSERT INTO boards VALUES (?, ?, ?)",
                        [(*key.split("|", 1), json.dumps(b, default=str)) for key, b in boards.items()])


def three_way(base: dict[str, dict], yours: dict[str, dict], theirs: dict[str, dict],
              label=lambda key, r: key) -> tuple[dict[str, dict], list[str], list[str]]:
    """Merge records field by field against the copy both sides started from (like `git merge`).
    A field only you changed keeps your value; one only they changed keeps theirs; one both changed differently
    keeps yours (you changed it on purpose) and is reported. Returns (merged, your changed keys, conflicts)."""
    merged = dict(theirs)
    changed, conflicts = [], []
    for key, mine in yours.items():
        start = base.get(key)
        if mine == start:
            continue
        changed.append(key)
        other = theirs.get(key)
        if other is None or other == start:
            merged[key] = mine                # new here, or untouched there: yours as is
            continue
        start = start or {}
        out = dict(other)
        for f in set(mine) | set(start):
            if mine.get(f) == start.get(f):
                continue                       # you didn't change it: theirs stands
            if other.get(f) not in (start.get(f), mine.get(f)):
                conflicts.append(f"{label(key, mine)}: {f}")
            out[f] = mine.get(f)
        merged[key] = out
    return merged, changed, conflicts


def pull_merging(store: Store, data_dir: Path, base_version_id: str) -> MergeReport:
    """Download the cloud's copy without losing what changed here since the shared starting point."""
    db = data_dir / "jobagent.db"
    yours_jobs, yours_boards = read_db(db)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive = store.download_version(base_version_id, tmp / "base.tar.gz")
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp / "base", members=[m for m in tar.getmembers() if m.name == "jobagent.db"],
                           filter="data")
        base_jobs, base_boards = read_db(tmp / "base" / "jobagent.db")
    changed_ids = [jid for jid, r in yours_jobs.items() if r != base_jobs.get(jid)]
    details = {jid: (data_dir / "jobs" / f"{jid}.json").read_bytes() for jid in changed_ids
               if (data_dir / "jobs" / f"{jid}.json").exists()}
    store.pull(data_dir)
    theirs_jobs, theirs_boards = read_db(db)
    label = lambda key, r: f"{(r.get('company') or '').strip()} - {r.get('title')}"  # noqa: E731
    jobs, changed, conflicts = three_way(base_jobs, yours_jobs, theirs_jobs, label)
    boards, _, board_conflicts = three_way(base_boards, yours_boards, theirs_boards,
                                           lambda key, b: f"board {key}")
    write_db(db, jobs, boards)
    for jid, content in details.items():   # posting files of jobs you changed (e.g. the resume folder link)
        (data_dir / "jobs" / f"{jid}.json").write_bytes(content)
    return MergeReport(yours=len(changed), conflicts=conflicts + board_conflicts)


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


__all__ = ["MergeReport", "pull_merging", "three_way", "mark_dirty_if_synced", "Store", "LockHeld", "bucket_name", "owner_name", "read_sync", "mark_dirty", "backup_local",
           "put_secrets", "send_email", "wait_for_lock", "PARAM_ANTHROPIC", "PARAM_GMAIL"]
