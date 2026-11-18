"""Cloud sync, lock and secrets, against an in-memory stand-in for S3 (no AWS needed)."""

import hashlib
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from jobagent import cloud


def _err(code: int, name: str) -> ClientError:
    return ClientError({"Error": {"Code": name}, "ResponseMetadata": {"HTTPStatusCode": code}}, "op")


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _err(404, "404")
        return {"ETag": '"' + hashlib.md5(self.objects[Key]).hexdigest() + '"'}

    def upload_fileobj(self, f, bucket, key):
        self.objects[key] = f.read()

    def download_fileobj(self, bucket, key, f):
        f.write(self.objects[key])

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, ContentType=None):
        if IfNoneMatch == "*" and Key in self.objects:
            raise _err(412, "PreconditionFailed")
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _err(404, "NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def _data(tmp_path, name="mac"):
    d = tmp_path / name / "data"
    (d / "jobs").mkdir(parents=True)
    (d / "jobagent.db").write_text("db v1")
    (d / "jobs" / "j1.json").write_text("{}")
    (d / "gmail_token.json").write_text("SECRET")
    return d


def test_push_and_pull_round_trip_without_secrets(tmp_path):
    s3 = FakeS3()
    mac, box = _data(tmp_path), tmp_path / "cloud" / "data"
    store = cloud.Store("b", s3)
    v1 = store.push(mac)
    assert store.pull(box) == v1
    assert (box / "jobagent.db").read_text() == "db v1" and (box / "jobs" / "j1.json").exists()
    assert not (box / "gmail_token.json").exists()  # secrets live in Parameter Store, never in S3
    assert cloud.read_sync(mac) == {**cloud.read_sync(mac), "version": v1, "dirty": False}


def test_lock_keeps_two_runs_apart_and_expires(tmp_path):
    s3 = FakeS3()
    store = cloud.Store("b", s3)
    store.lock("the AWS daily run")
    with pytest.raises(cloud.LockHeld) as e:
        store.lock("apply on mac")
    assert "the AWS daily run" in str(e.value)
    store.unlock()
    store.lock("apply on mac")
    # A lock left behind by a run that died long ago is taken over.
    old = (datetime.now(timezone.utc) - timedelta(hours=cloud.LOCK_STALE_HOURS + 1)).isoformat()
    s3.objects[cloud.LOCK_KEY] = json.dumps({"owner": "dead run", "since": old}).encode()
    store.lock("the AWS daily run")
    assert store.lock_holder()["owner"] == "the AWS daily run"


def test_secrets_are_encrypted_parameters():
    calls = []

    class SSM:
        def put_parameter(self, **kw):
            calls.append(kw)

    names = cloud.put_secrets({cloud.PARAM_GMAIL: "tok"}, ssm=SSM())
    assert names == [cloud.PARAM_GMAIL]
    assert calls[0]["Type"] == "SecureString" and calls[0]["Overwrite"]


def test_apply_keeps_unuploaded_work_before_downloading(tmp_path, monkeypatch):
    from jobagent import cli

    s3 = FakeS3()
    mac = _data(tmp_path)
    store = cloud.Store("b", s3)
    store.push(mac)
    cloud.mark_dirty(mac)                      # an apply session that never uploaded...
    (mac / "jobagent.db").write_text("db with my submissions")
    other = _data(tmp_path, "cloud")
    (other / "jobagent.db").write_text("db v2 from the cloud")
    store.push(other)                          # ...and the cloud ran since
    monkeypatch.setattr(cli.typer, "echo", lambda *a, **k: None)
    monkeypatch.setattr(cli.typer, "secho", lambda *a, **k: None)
    cli._sync_down(store, mac)
    assert (mac / "jobagent.db").read_text() == "db v2 from the cloud"
    backups = list(mac.parent.glob("data_backup_*.tar.gz"))
    assert len(backups) == 1                   # the unuploaded work is kept, not silently lost
