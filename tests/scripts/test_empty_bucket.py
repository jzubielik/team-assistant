"""Emptying the bootstrap's versioned assets bucket (make unbootstrap)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ta_empty_bucket", ROOT / "scripts" / "empty_bucket.py"
)
empty_bucket = importlib.util.module_from_spec(spec)
sys.modules["ta_empty_bucket"] = empty_bucket
spec.loader.exec_module(empty_bucket)


class Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return iter(self.pages)


class FakeS3:
    def __init__(self, pages=(), exists=True, errors=None):
        self.pages, self.exists, self.errors = list(pages), exists, errors
        self.deleted, self.bucket_deleted = [], False

    def head_bucket(self, Bucket):  # noqa: N803 - boto3's parameter name
        if not self.exists:
            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

    def get_paginator(self, name):
        assert name == "list_object_versions"
        return Paginator(self.pages)

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        self.deleted.append(Delete["Objects"])
        return {"Errors": self.errors} if self.errors else {}

    def delete_bucket(self, Bucket):  # noqa: N803
        self.bucket_deleted = True


def page(versions=0, markers=0, start=0):
    return {
        "Versions": [
            {"Key": f"k{start + i}", "VersionId": f"v{start + i}"} for i in range(versions)
        ],
        "DeleteMarkers": [
            {"Key": f"m{start + i}", "VersionId": f"d{start + i}"} for i in range(markers)
        ],
    }


def test_every_version_and_delete_marker_goes_then_the_bucket():
    s3 = FakeS3([page(versions=2, markers=1), page(versions=1, start=10)])
    assert empty_bucket.empty_and_delete(s3, "b") == 4
    assert [o["VersionId"] for batch in s3.deleted for o in batch] == ["v0", "v1", "d0", "v10"]
    assert s3.bucket_deleted


def test_deletes_go_in_batches_of_a_thousand():
    s3 = FakeS3([page(versions=2500)])
    assert empty_bucket.empty_and_delete(s3, "b") == 2500
    assert [len(batch) for batch in s3.deleted] == [1000, 1000, 500]


def test_a_missing_bucket_is_nothing_to_do():
    s3 = FakeS3(exists=False)
    assert empty_bucket.empty_and_delete(s3, "b") == 0
    assert not s3.bucket_deleted


def test_a_refused_delete_stops_before_the_bucket():
    s3 = FakeS3([page(versions=1)], errors=[{"Key": "k0", "Code": "AccessDenied"}])
    with pytest.raises(RuntimeError, match="refused to delete 1"):
        empty_bucket.empty_and_delete(s3, "b")
    assert not s3.bucket_deleted
