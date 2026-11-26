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


def test_your_edited_resume_survives_a_download(tmp_path, monkeypatch):
    import os
    import time

    from jobagent import cli

    s3 = FakeS3()
    mac = _data(tmp_path)
    (mac / "master_resume.yaml").write_text("old resume")
    store = cloud.Store("b", s3)
    store.push(mac)
    cloud_side = tmp_path / "cloud" / "data"
    store.pull(cloud_side)
    (cloud_side / "jobagent.db").write_text("db after the cloud run")
    store.push(cloud_side)                                  # the daily run uploads newer data
    later = time.time() + 5
    (mac / "master_resume.yaml").write_text("resume with the AWS bullet")
    os.utime(mac / "master_resume.yaml", (later, later))   # edited on the Mac after its last sync
    monkeypatch.setattr(cli.typer, "echo", lambda *a, **k: None)
    cli._sync_down(store, mac)
    assert (mac / "jobagent.db").read_text() == "db after the cloud run"
    assert (mac / "master_resume.yaml").read_text() == "resume with the AWS bullet"
    assert cloud.read_sync(mac)["dirty"]                    # so apply uploads it when it finishes


def test_emergency_stop_uploads_and_releases_the_lock(tmp_path):
    s3 = FakeS3()
    data = _data(tmp_path)
    store = cloud.Store("b", s3)
    store.lock("the AWS daily run")
    (data / "jobagent.db").write_text("db saved mid-run")
    exits = []
    cloud.emergency_stop(store, data, pulled=True, locked=True, exit=exits.append)
    assert exits == [143]
    assert cloud.LOCK_KEY not in s3.objects            # apply isn't blocked for hours
    restored = tmp_path / "restored" / "data"
    store.pull(restored)
    assert (restored / "jobagent.db").read_text() == "db saved mid-run"


def test_emergency_stop_before_download_or_lock_touches_nothing(tmp_path):
    s3 = FakeS3()
    store = cloud.Store("b", s3)
    store.lock("apply on mac")                          # someone else holds it; we were still waiting
    exits = []
    cloud.emergency_stop(store, _data(tmp_path), pulled=False, locked=False, exit=exits.append)
    assert exits == [143]
    assert cloud.STATE_KEY not in s3.objects            # an empty container never uploads
    assert store.lock_holder()["owner"] == "apply on mac"  # and never frees a lock it doesn't hold


def test_apply_reads_env_before_deciding_cloud_or_local(tmp_path, monkeypatch):
    """APPLYPILOT_BUCKET set only in .env must still switch apply to cloud mode."""
    from jobagent import cli

    monkeypatch.delenv("APPLYPILOT_BUCKET", raising=False)
    calls = []

    def fake_settings(*a, **k):
        monkeypatch.setenv("APPLYPILOT_BUCKET", "b")  # what load_dotenv does
        calls.append("settings")
        return type("S", (), {"data_dir": tmp_path / "data"})()

    monkeypatch.setattr(cli, "load_settings", fake_settings)
    monkeypatch.setattr(cli, "_apply_local", lambda limit: calls.append("local"))

    class Stop(Exception):
        pass

    def store(bucket, s3=None):
        calls.append(f"cloud:{bucket}")
        raise Stop()

    monkeypatch.setattr(cloud, "Store", store)
    try:
        cli._apply(None)
    except Stop:
        pass
    assert calls == ["settings", "cloud:b"]


# ---- merging two changed copies ------------------------------------------------------------------------------------
class VersionedS3(FakeS3):
    """Keeps every upload, like the real bucket (versioning on)."""

    def __init__(self):
        super().__init__()
        self.versions: dict[str, list[bytes]] = {}

    def upload_fileobj(self, f, bucket, key):
        super().upload_fileobj(f, bucket, key)
        self.versions.setdefault(key, []).append(self.objects[key])

    def head_object(self, Bucket, Key):
        head = super().head_object(Bucket, Key)
        return {**head, "VersionId": f"v{len(self.versions.get(Key, []))}"}

    def get_object(self, Bucket, Key, VersionId=None):
        if VersionId:
            return {"Body": io.BytesIO(self.versions[Key][int(VersionId[1:]) - 1])}
        return super().get_object(Bucket, Key)


def _tracker(data):
    from jobagent.tracker import Tracker

    return Tracker(data.parent / "tracker.xlsx", data / "jobs")


def _add(t, company, title):
    t.add_job({"company": company, "title": title, "location": "Remote, US", "source": "greenhouse",
               "url": f"https://boards.greenhouse.io/{company.lower()}/jobs/1", "description": "Build things.",
               "ats_type": "greenhouse"})
    return next(j for j, r in t.jobs.items() if r["company"] == company)


def test_three_way_keeps_both_sides_and_reports_real_conflicts():
    base = {"a": {"status": "shortlisted", "notes": None}, "b": {"status": "found"}}
    yours = {"a": {"status": "applied", "notes": "phone screen"}, "b": {"status": "found"}, "n": {"status": "prepared"}}
    theirs = {"a": {"status": "shortlisted", "notes": "auto"}, "b": {"status": "shortlisted"}}
    merged, changed, conflicts = cloud.three_way(base, yours, theirs)
    assert merged["a"] == {"status": "applied", "notes": "phone screen"}  # your status; notes: both changed -> yours
    assert merged["b"] == {"status": "shortlisted"}                       # only the cloud changed it
    assert merged["n"] == {"status": "prepared"}                          # added only here
    assert sorted(changed) == ["a", "n"] and conflicts == ["a: notes"]


def test_download_merges_mac_edits_with_the_cloud_run(tmp_path, monkeypatch):
    from jobagent import cli

    monkeypatch.setenv(cloud.BUCKET_ENV, "b")
    monkeypatch.setattr(cli.typer, "echo", lambda *a, **k: None)
    monkeypatch.setattr(cli.typer, "secho", lambda *a, **k: None)
    s3 = VersionedS3()
    store = cloud.Store("b", s3)
    mac = tmp_path / "mac" / "data"
    t = _tracker(mac)
    a, b = _add(t, "Acme", "Engineer"), _add(t, "Beta", "Developer")
    t.save()
    store.push(mac)                                   # shared starting point (v1)

    box = tmp_path / "cloud" / "data"                 # the AWS run: shortlists Beta, uploads v2
    store.pull(box)
    tc = _tracker(box)
    tc.update(b, status="shortlisted")
    tc.save()
    store.push(box)

    t = _tracker(mac)                                 # meanwhile on the Mac: `status` marks Acme applied,
    t.update(a, status="applied")                     # and a job is added by hand
    n = _add(t, "Gamma", "Analyst Engineer")
    t.update(n, status="prepared")
    t.save()
    assert cloud.read_sync(mac)["dirty"]              # the save marked unsynced changes

    cli._sync_down(store, mac)
    merged = _tracker(mac).jobs
    assert merged[a]["status"] == "applied"           # yours
    assert merged[b]["status"] == "shortlisted"       # the cloud's
    assert merged[n]["status"] == "prepared"          # new here
    assert (mac / "jobs" / f"{n}.json").exists()
    sync = cloud.read_sync(mac)
    assert sync["dirty"] and sync["version_id"] == "v2"  # merged copy goes up next; it now starts from v2


def test_saves_mark_unsynced_changes_only_on_a_synced_machine(tmp_path, monkeypatch):
    mac = tmp_path / "data"
    monkeypatch.delenv(cloud.BUCKET_ENV, raising=False)
    _tracker(mac).save()
    assert not (mac / cloud.SYNC_FILE).exists()           # not using AWS: nothing to mark
    monkeypatch.setenv(cloud.BUCKET_ENV, "b")
    cloud.Store("b", VersionedS3()).push(mac)
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://x")
    _tracker(mac).save()
    assert not cloud.read_sync(mac)["dirty"]              # inside the AWS run: never
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4")
    _tracker(mac).save()
    assert cloud.read_sync(mac)["dirty"]                  # on the Mac: yes


def test_download_keeps_outreach_contacts_from_both_sides(tmp_path, monkeypatch):
    """Contacts added on the Mac and replies noticed by the AWS run both survive a download."""
    from jobagent import cli
    from jobagent.outreach.contacts import new_contact, record_sent

    monkeypatch.setenv(cloud.BUCKET_ENV, "b")
    monkeypatch.setattr(cli.typer, "echo", lambda *a, **k: None)
    monkeypatch.setattr(cli.typer, "secho", lambda *a, **k: None)
    s3 = VersionedS3()
    store = cloud.Store("b", s3)
    mac = tmp_path / "mac" / "data"
    t = _tracker(mac)
    a = _add(t, "Acme", "Engineer")
    old = t.add_contact(new_contact(t.jobs[a], "Pat Lee", "engineer", "email"))
    record_sent(old, "2026-11-20")
    t.save()
    store.push(mac)

    box = tmp_path / "cloud" / "data"                 # the AWS run notices Pat replied
    store.pull(box)
    tc = _tracker(box)
    tc.contacts[old["id"]].update(status="replied", replied_date="2026-11-24")
    tc.save()
    store.push(box)

    t = _tracker(mac)                                 # meanwhile the Mac adds a hiring manager
    new = t.add_contact(new_contact(t.jobs[a], "Sam Diaz", "hiring_manager", "email"))
    t.save()
    cli._sync_down(store, mac)
    merged = _tracker(mac).contacts
    assert merged[old["id"]]["status"] == "replied" and new["id"] in merged
