"""Empty a versioned S3 bucket and delete it: the CDK bootstrap's assets bucket.

    python scripts/empty_bucket.py --profile <p> --region <r> --bucket <name>

The bootstrap template keeps that bucket when its stack is deleted (DeletionPolicy: Retain) and
turns versioning on, so a deleted object still has its versions and a delete marker. Every
version and every delete marker is deleted, 1,000 per request (the S3 limit of DeleteObjects),
then the bucket. A bucket that does not exist is not an error: there is nothing to delete.
Prints counts only, never object names (they are hashes of templates and assets).
"""

from __future__ import annotations

import argparse
import sys

BATCH = 1000  # DeleteObjects takes at most 1,000 keys per request


def versions(client, bucket: str) -> list[dict]:
    """Every version and delete marker of the bucket, as DeleteObjects wants them."""
    found = []
    for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket):
        for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
            found.append({"Key": item["Key"], "VersionId": item["VersionId"]})
    return found


def empty_and_delete(client, bucket: str) -> int:
    """Delete every version, then the bucket; returns how many versions were deleted."""
    try:
        client.head_bucket(Bucket=bucket)
    except Exception as error:  # noqa: BLE001 - only "not there" ends quietly
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        if code in ("404", "NoSuchBucket", "NotFound"):
            return 0
        raise
    objects = versions(client, bucket)
    for start in range(0, len(objects), BATCH):
        batch = objects[start : start + BATCH]
        answer = client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        if answer.get("Errors"):
            raise RuntimeError(f"S3 refused to delete {len(answer['Errors'])} object version(s)")
    client.delete_bucket(Bucket=bucket)
    return len(objects)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("profile", "region", "bucket"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    import boto3

    client = boto3.Session(profile_name=args.profile, region_name=args.region).client("s3")
    deleted = empty_and_delete(client, args.bucket)
    print(f"    {deleted} object version(s) deleted, then the bucket")
    return 0


if __name__ == "__main__":
    sys.exit(main())
