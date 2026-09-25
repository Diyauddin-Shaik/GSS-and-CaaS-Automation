#!/usr/bin/env python3

import configparser
import os
import sys
from datetime import datetime

import boto3
from botocore.exceptions import ClientError, BotoCoreError


CONFIG_FILE = "config.ini"


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print("ERROR: config.ini not found.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)

    required_sections = ["SOURCE", "DESTINATION"]

    for section in required_sections:
        if section not in config:
            print("ERROR: Missing [{}] section in config.ini".format(section))
            sys.exit(1)

    return config


def get_value(config, section, key):
    value = config.get(section, key, fallback="").strip()

    if not value:
        print("ERROR: Missing '{}' in [{}]".format(key, section))
        sys.exit(1)

    return value


def normalize_endpoint(endpoint):
    endpoint = endpoint.strip()

    if not endpoint.startswith("http://") and \
       not endpoint.startswith("https://"):
        endpoint = "https://" + endpoint

    return endpoint.rstrip("/")


def normalize_path(path):
    path = path.strip().lstrip("/")

    if path and not path.endswith("/"):
        path += "/"

    return path


# ---------------------------------------------------------
# S3 CLIENT
# ---------------------------------------------------------

def create_s3_client(section, config):
    endpoint = normalize_endpoint(
        get_value(config, section, "endpoint")
    )

    access_key = get_value(config, section, "access_key")
    secret_key = get_value(config, section, "secret_key")

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        verify=True
    )


# ---------------------------------------------------------
# CONNECTION TEST
# ---------------------------------------------------------

def test_connection(client, bucket, account_name):
    print()
    print("Testing connection to {} account...".format(account_name))

    try:
        client.head_bucket(Bucket=bucket)

        print("Connection successful.")
        print("Bucket          : {}".format(bucket))

        return True

    except ClientError as exc:
        print(
            "ERROR: Cannot access bucket '{}' for {} account.".format(
                bucket,
                account_name
            )
        )
        print("Details:", exc)
        return False

    except BotoCoreError as exc:
        print("ERROR: S3 connection failed.")
        print("Details:", exc)
        return False


# ---------------------------------------------------------
# LIST SOURCE FILES
# ---------------------------------------------------------

def list_source_objects(client, bucket, prefix):
    objects = []

    print()
    print("Fetching source files...")
    print("Bucket : {}".format(bucket))
    print("Path   : {}".format(prefix))

    paginator = client.get_paginator("list_objects_v2")

    try:
        pages = paginator.paginate(
            Bucket=bucket,
            Prefix=prefix
        )

        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]

                # Ignore folder marker objects
                if key.endswith("/"):
                    continue

                objects.append({
                    "key": key,
                    "size": obj.get("Size", 0),
                    "last_modified": obj.get("LastModified")
                })

    except (ClientError, BotoCoreError) as exc:
        print("ERROR: Unable to list source objects.")
        print("Details:", exc)
        sys.exit(1)

    return objects


# ---------------------------------------------------------
# DISPLAY FILES
# ---------------------------------------------------------

def display_source_files(objects, source_prefix):
    print()
    print("=" * 80)
    print("SOURCE FILES")
    print("=" * 80)

    if not objects:
        print("No files found in source path.")
        return

    for index, obj in enumerate(objects, start=1):
        relative_name = obj["key"]

        if relative_name.startswith(source_prefix):
            relative_name = relative_name[len(source_prefix):]

        print(
            "{:5d}. {}  ({} bytes)".format(
                index,
                relative_name,
                obj["size"]
            )
        )

    print("-" * 80)
    print("Total source files : {}".format(len(objects)))
    print("=" * 80)


# ---------------------------------------------------------
# DESTINATION EXISTENCE CHECK
# ---------------------------------------------------------

def object_exists(client, bucket, key):
    try:
        client.head_object(
            Bucket=bucket,
            Key=key
        )

        return True

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")

        if error_code in ["404", "NoSuchKey", "NotFound"]:
            return False

        # Some StorageGRID installations return 403 when
        # the object cannot be checked.
        if error_code == "403":
            raise

        return False


# ---------------------------------------------------------
# COPY OBJECT
# ---------------------------------------------------------

def copy_object_streaming(
    source_client,
    destination_client,
    source_bucket,
    destination_bucket,
    source_key,
    destination_key,
    part_size=64 * 1024 * 1024
):
    """
    Copy large objects from source StorageGRID account
    to destination StorageGRID account using multipart upload.

    This avoids loading the complete file into memory.
    """

    response = source_client.head_object(
        Bucket=source_bucket,
        Key=source_key
    )

    file_size = response.get("ContentLength", 0)

    # -----------------------------------------------------
    # Zero-byte object
    # -----------------------------------------------------

    if file_size == 0:
        destination_client.put_object(
            Bucket=destination_bucket,
            Key=destination_key
        )

        return

    # -----------------------------------------------------
    # Create multipart upload
    # -----------------------------------------------------

    multipart = destination_client.create_multipart_upload(
        Bucket=destination_bucket,
        Key=destination_key
    )

    upload_id = multipart["UploadId"]

    parts = []

    try:
        part_number = 1
        offset = 0

        while offset < file_size:

            end = min(
                offset + part_size - 1,
                file_size - 1
            )

            print(
                "    Copying part {}: bytes {}-{}".format(
                    part_number,
                    offset,
                    end
                )
            )

            source_response = source_client.get_object(
                Bucket=source_bucket,
                Key=source_key,
                Range="bytes={}-{}".format(offset, end)
            )

            data = source_response["Body"].read()

            upload_response = destination_client.upload_part(
                Bucket=destination_bucket,
                Key=destination_key,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=data
            )

            parts.append({
                "PartNumber": part_number,
                "ETag": upload_response["ETag"]
            })

            source_response["Body"].close()

            offset = end + 1
            part_number += 1

        # -------------------------------------------------
        # Complete multipart upload
        # -------------------------------------------------

        destination_client.complete_multipart_upload(
            Bucket=destination_bucket,
            Key=destination_key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": parts
            }
        )

    except Exception:
        print("    ERROR: Copy failed. Aborting upload.")

        try:
            destination_client.abort_multipart_upload(
                Bucket=destination_bucket,
                Key=destination_key,
                UploadId=upload_id
            )
        except Exception:
            pass

        raise


# ---------------------------------------------------------
# MAIN TRANSFER
# ---------------------------------------------------------

def transfer_files(
    source_client,
    destination_client,
    source_bucket,
    destination_bucket,
    source_prefix,
    destination_prefix,
    objects
):
    copied = 0
    skipped = 0
    failed = 0

    print()
    print("=" * 80)
    print("STARTING DATA TRANSFER")
    print("=" * 80)

    for index, obj in enumerate(objects, start=1):

        source_key = obj["key"]

        # Preserve the relative path under source prefix
        if source_key.startswith(source_prefix):
            relative_key = source_key[len(source_prefix):]
        else:
            relative_key = source_key

        destination_key = destination_prefix + relative_key

        print()
        print("[{}/{}]".format(index, len(objects)))
        print("File       : {}".format(relative_key))
        print("Source     : {}".format(source_key))
        print("Destination: {}".format(destination_key))

        # -------------------------------------------------
        # Check whether destination already contains file
        # -------------------------------------------------

        try:
            if object_exists(
                destination_client,
                destination_bucket,
                destination_key
            ):
                print("STATUS     : SKIPPED - file already exists")

                skipped += 1
                continue

        except ClientError as exc:
            print("ERROR: Unable to check destination object.")
            print("Details:", exc)

            failed += 1
            continue

        # -------------------------------------------------
        # Copy new file
        # -------------------------------------------------

        try:
            print("STATUS     : COPYING")

            copy_object_streaming(
                source_client=source_client,
                destination_client=destination_client,
                source_bucket=source_bucket,
                destination_bucket=destination_bucket,
                source_key=source_key,
                destination_key=destination_key
            )

            print("STATUS     : COPIED")

            copied += 1

        except Exception as exc:
            print("STATUS     : FAILED")
            print("ERROR      :", exc)

            failed += 1

    return copied, skipped, failed


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():

    print()
    print("=" * 80)
    print("StorageGRID Data Transfer")
    print("=" * 80)

    config = load_config()

    # -----------------------------------------------------
    # SOURCE CONFIG
    # -----------------------------------------------------

    source_account = get_value(
        config,
        "SOURCE",
        "account_id"
    )

    source_bucket = get_value(
        config,
        "SOURCE",
        "bucket"
    )

    source_prefix = normalize_path(
        get_value(
            config,
            "SOURCE",
            "path"
        )
    )

    # -----------------------------------------------------
    # DESTINATION CONFIG
    # -----------------------------------------------------

    destination_account = get_value(
        config,
        "DESTINATION",
        "account_id"
    )

    destination_bucket = get_value(
        config,
        "DESTINATION",
        "bucket"
    )

    destination_prefix = normalize_path(
        get_value(
            config,
            "DESTINATION",
            "path"
        )
    )

    print()
    print("SOURCE")
    print("Account : {}".format(source_account))
    print("Bucket  : {}".format(source_bucket))
    print("Path    : {}".format(source_prefix))

    print()
    print("DESTINATION")
    print("Account : {}".format(destination_account))
    print("Bucket  : {}".format(destination_bucket))
    print("Path    : {}".format(destination_prefix))

    # -----------------------------------------------------
    # CREATE CLIENTS
    # -----------------------------------------------------

    print()
    print("Creating StorageGRID connections...")

    source_client = create_s3_client(
        "SOURCE",
        config
    )

    destination_client = create_s3_client(
        "DESTINATION",
        config
    )

    # -----------------------------------------------------
    # TEST SOURCE
    # -----------------------------------------------------

    if not test_connection(
        source_client,
        source_bucket,
        "SOURCE"
    ):
        sys.exit(1)

    # -----------------------------------------------------
    # TEST DESTINATION
    # -----------------------------------------------------

    if not test_connection(
        destination_client,
        destination_bucket,
        "DESTINATION"
    ):
        sys.exit(1)

    # -----------------------------------------------------
    # GET SOURCE FILES
    # -----------------------------------------------------

    objects = list_source_objects(
        source_client,
        source_bucket,
        source_prefix
    )

    display_source_files(
        objects,
        source_prefix
    )

    if not objects:
        print()
        print("Nothing to copy.")
        sys.exit(0)

    # -----------------------------------------------------
    # CONFIRMATION
    # -----------------------------------------------------

    print()
    print("=" * 80)
    print("TRANSFER SUMMARY")
    print("=" * 80)

    print("Source account      : {}".format(source_account))
    print("Source bucket       : {}".format(source_bucket))
    print("Source path         : {}".format(source_prefix))

    print()

    print("Destination account : {}".format(destination_account))
    print("Destination bucket  : {}".format(destination_bucket))
    print("Destination path    : {}".format(destination_prefix))

    print()
    print("Files found         : {}".format(len(objects)))

    print()
    answer = input(
        "Can I copy the data to the destination? (y/n): "
    ).strip().lower()

    if answer not in ["y", "yes"]:
        print()
        print("Transfer cancelled by user.")
        sys.exit(0)

    # -----------------------------------------------------
    # TRANSFER
    # -----------------------------------------------------

    start_time = datetime.now()

    copied, skipped, failed = transfer_files(
        source_client=source_client,
        destination_client=destination_client,
        source_bucket=source_bucket,
        destination_bucket=destination_bucket,
        source_prefix=source_prefix,
        destination_prefix=destination_prefix,
        objects=objects
    )

    end_time = datetime.now()

    # -----------------------------------------------------
    # FINAL SUMMARY
    # -----------------------------------------------------

    print()
    print("=" * 80)
    print("TRANSFER COMPLETED")
    print("=" * 80)

    print("Source files found : {}".format(len(objects)))
    print("Files copied       : {}".format(copied))
    print("Files skipped      : {}".format(skipped))
    print("Files failed       : {}".format(failed))

    print()
    print("Start time         : {}".format(start_time))
    print("End time           : {}".format(end_time))

    print("=" * 80)


if __name__ == "__main__":
    main()
