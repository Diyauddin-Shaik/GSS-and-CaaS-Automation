#!/usr/bin/env python3

import configparser
import logging
import os
import sys
from datetime import datetime

import boto3
from botocore.exceptions import ClientError, BotoCoreError


# ============================================================
# Configuration
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "data_transfer.log")


# ============================================================
# Logging
# ============================================================

def setup_logging():

    os.makedirs(LOG_DIR, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout)
        ]
    )


# ============================================================
# Load Configuration
# ============================================================

def load_config():

    if not os.path.exists(CONFIG_FILE):

        print("ERROR: config.ini not found.")
        print("Please create config.ini using config.example.ini.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)

    if "SOURCE" not in config:
        print("ERROR: [SOURCE] section is missing.")
        sys.exit(1)

    if "DESTINATION" not in config:
        print("ERROR: [DESTINATION] section is missing.")
        sys.exit(1)

    return config


# ============================================================
# Validate Configuration
# ============================================================

def validate_section(config, section_name):

    required_values = [
        "account_id",
        "username",
        "password",
        "endpoint",
        "access_key",
        "secret_key",
        "bucket",
        "path"
    ]

    for value in required_values:

        if not config[section_name].get(value, "").strip():

            print(
                "ERROR: '{}' is missing in [{}].".format(
                    value,
                    section_name
                )
            )

            sys.exit(1)


# ============================================================
# Create S3 Client
# ============================================================

def create_s3_client(config, section_name):

    section = config[section_name]

    endpoint = section["endpoint"]
    access_key = section["access_key"]
    secret_key = section["secret_key"]

    try:

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            verify=True
        )

        return client

    except Exception as exc:

        logging.error(
            "Unable to create S3 client for %s: %s",
            section_name,
            exc
        )

        raise


# ============================================================
# Check Bucket Access
# ============================================================

def check_bucket_access(client, bucket, account_name):

    print()
    print("Checking {} bucket access...".format(account_name))

    try:

        client.head_bucket(
            Bucket=bucket
        )

        print("Bucket access: SUCCESS")

        logging.info(
            "%s bucket access successful: %s",
            account_name,
            bucket
        )

        return True

    except (ClientError, BotoCoreError) as exc:

        print("Bucket access: FAILED")
        print(exc)

        logging.error(
            "%s bucket access failed: %s",
            account_name,
            exc
        )

        return False


# ============================================================
# List Source Files
# ============================================================

def list_source_files(client, bucket, path):

    objects = []

    print()
    print("=" * 80)
    print("READING SOURCE DATA")
    print("=" * 80)

    print("Bucket : {}".format(bucket))
    print("Path   : {}".format(path))
    print()

    try:

        paginator = client.get_paginator(
            "list_objects_v2"
        )

        pages = paginator.paginate(
            Bucket=bucket,
            Prefix=path
        )

        for page in pages:

            for obj in page.get("Contents", []):

                key = obj["Key"]

                # Ignore S3 folder marker
                if key.endswith("/"):
                    continue

                objects.append(obj)

    except (ClientError, BotoCoreError) as exc:

        logging.error(
            "Unable to list source files: %s",
            exc
        )

        print("ERROR: Unable to list source files.")
        print(exc)

        sys.exit(1)

    return objects


# ============================================================
# Display Source Files
# ============================================================

def display_source_files(objects):

    print()
    print("=" * 80)
    print("SOURCE FILES")
    print("=" * 80)

    if not objects:

        print("No files found.")
        return

    for number, obj in enumerate(objects, start=1):

        print(
            "{:5d}. {}".format(
                number,
                obj["Key"]
            )
        )

    print()
    print("-" * 80)

    print(
        "Total source files: {}".format(
            len(objects)
        )
    )

    print("=" * 80)


# ============================================================
# Check Destination File
# ============================================================

def object_exists(client, bucket, key):

    try:

        client.head_object(
            Bucket=bucket,
            Key=key
        )

        return True

    except ClientError as exc:

        error_code = exc.response.get(
            "Error",
            {}
        ).get(
            "Code",
            ""
        )

        if error_code in [
            "404",
            "NoSuchKey",
            "NotFound"
        ]:

            return False

        raise


# ============================================================
# Create Duplicate Filename
# ============================================================

def create_duplicate_filename(
    destination_key,
    destination_objects
):

    directory = os.path.dirname(
        destination_key
    )

    filename = os.path.basename(
        destination_key
    )

    name, extension = os.path.splitext(
        filename
    )

    date_string = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    counter = 0

    while True:

        if counter == 0:

            new_filename = "{}_{}{}".format(
                name,
                date_string,
                extension
            )

        else:

            new_filename = "{}_{}_{}{}".format(
                name,
                date_string,
                counter,
                extension
            )

        if directory:

            new_key = "{}/{}".format(
                directory,
                new_filename
            )

        else:

            new_key = new_filename

        if new_key not in destination_objects:

            return new_key

        counter += 1


# ============================================================
# Get Destination Objects
# ============================================================

def get_destination_objects(
    client,
    bucket,
    path
):

    destination_objects = {}

    print()
    print("Checking destination path...")

    try:

        paginator = client.get_paginator(
            "list_objects_v2"
        )

        pages = paginator.paginate(
            Bucket=bucket,
            Prefix=path
        )

        for page in pages:

            for obj in page.get("Contents", []):

                key = obj["Key"]

                if key.endswith("/"):
                    continue

                destination_objects[key] = obj

    except (ClientError, BotoCoreError) as exc:

        logging.error(
            "Unable to list destination objects: %s",
            exc
        )

        print("ERROR: Unable to read destination path.")
        print(exc)

        sys.exit(1)

    return destination_objects


# ============================================================
# Confirmation
# ============================================================

def ask_confirmation(message):

    while True:

        answer = input(
            "{} (y/n): ".format(message)
        ).strip().lower()

        if answer in ["y", "yes"]:
            return True

        if answer in ["n", "no"]:
            return False

        print("Please enter y or n.")


# ============================================================
# Copy Object
# ============================================================

def copy_file(
    source_client,
    destination_client,
    source_bucket,
    source_key,
    destination_bucket,
    destination_key,
    source_size
):

    print()
    print("Copying:")
    print("  SOURCE      : {}".format(source_key))
    print("  DESTINATION : {}".format(destination_key))

    logging.info(
        "Copying %s -> %s",
        source_key,
        destination_key
    )

    try:

        response = source_client.get_object(
            Bucket=source_bucket,
            Key=source_key
        )

        body = response["Body"]

        try:

            extra_args = {}

            if response.get("ContentType"):
                extra_args["ContentType"] = response[
                    "ContentType"
                ]

            if response.get("Metadata"):
                extra_args["Metadata"] = response[
                    "Metadata"
                ]

            destination_client.upload_fileobj(
                body,
                destination_bucket,
                destination_key,
                ExtraArgs=extra_args
            )

        finally:

            body.close()

        print("  SUCCESS")

        logging.info(
            "Successfully copied: %s",
            destination_key
        )

        return True

    except Exception as exc:

        print(
            "  FAILED: {}".format(
                exc
            )
        )

        logging.error(
            "Failed to copy %s: %s",
            source_key,
            exc
        )

        return False


# ============================================================
# Main Transfer
# ============================================================

def perform_transfer(
    source_client,
    destination_client,
    source,
    destination,
    source_objects,
    destination_objects
):

    copied_count = 0
    skipped_count = 0
    failed_count = 0
    duplicate_count = 0

    source_bucket = source["bucket"]
    source_path = source["path"]

    destination_bucket = destination["bucket"]
    destination_path = destination["path"]

    print()
    print("=" * 80)
    print("STARTING DATA COPY")
    print("=" * 80)

    for number, obj in enumerate(
        source_objects,
        start=1
    ):

        source_key = obj["Key"]

        # Calculate relative path
        if source_key.startswith(source_path):

            relative_key = source_key[
                len(source_path):
            ]

        else:

            relative_key = os.path.basename(
                source_key
            )

        destination_key = (
            destination_path +
            relative_key
        )

        print()
        print(
            "[{}/{}]".format(
                number,
                len(source_objects)
            )
        )

        # ----------------------------------------------------
        # Duplicate check
        # ----------------------------------------------------

        if destination_key in destination_objects:

            duplicate_count += 1

            print()
            print("=" * 80)
            print("DUPLICATE FILE FOUND")
            print("=" * 80)

            print(
                "File already exists:"
            )

            print(
                "  {}".format(
                    destination_key
                )
            )

            copy_duplicate = ask_confirmation(
                "Do you want to copy this file also"
                " with a date suffix?"
            )

            if not copy_duplicate:

                print(
                    "Skipped: {}".format(
                        destination_key
                    )
                )

                skipped_count += 1

                logging.info(
                    "Skipped duplicate: %s",
                    destination_key
                )

                continue

            destination_key = create_duplicate_filename(
                destination_key,
                destination_objects
            )

            print(
                "New destination filename:"
            )

            print(
                "  {}".format(
                    destination_key
                )
            )

        # ----------------------------------------------------
        # Copy
        # ----------------------------------------------------

        success = copy_file(
            source_client=source_client,
            destination_client=destination_client,
            source_bucket=source_bucket,
            source_key=source_key,
            destination_bucket=destination_bucket,
            destination_key=destination_key,
            source_size=obj.get("Size", 0)
        )

        if success:

            copied_count += 1

            # Add newly copied object so that if the same
            # destination key appears again, it is detected.
            destination_objects[destination_key] = {
                "Key": destination_key
            }

        else:

            failed_count += 1

    # --------------------------------------------------------
    # Final Summary
    # --------------------------------------------------------

    print()
    print("=" * 80)
    print("DATA TRANSFER COMPLETED")
    print("=" * 80)

    print(
        "Source files found       : {}".format(
            len(source_objects)
        )
    )

    print(
        "Files copied             : {}".format(
            copied_count
        )
    )

    print(
        "Duplicate files found    : {}".format(
            duplicate_count
        )
    )

    print(
        "Files skipped            : {}".format(
            skipped_count
        )
    )

    print(
        "Files failed             : {}".format(
            failed_count
        )
    )

    print()
    print(
        "Destination bucket       : {}".format(
            destination_bucket
        )
    )

    print(
        "Destination path         : {}".format(
            destination_path
        )
    )

    print("=" * 80)

    logging.info(
        "Transfer completed. "
        "Found=%d Copied=%d Duplicates=%d "
        "Skipped=%d Failed=%d",
        len(source_objects),
        copied_count,
        duplicate_count,
        skipped_count,
        failed_count
    )

    if failed_count > 0:
        return False

    return True


# ============================================================
# MAIN
# ============================================================

def main():

    setup_logging()

    logging.info(
        "StorageGRID data transfer started."
    )

    print()
    print("=" * 80)
    print("STORAGEGRID DATA TRANSFER")
    print("=" * 80)

    # --------------------------------------------------------
    # Load configuration
    # --------------------------------------------------------

    config = load_config()

    validate_section(
        config,
        "SOURCE"
    )

    validate_section(
        config,
        "DESTINATION"
    )

    source = config["SOURCE"]
    destination = config["DESTINATION"]

    # --------------------------------------------------------
    # Display source and destination
    # --------------------------------------------------------

    print()
    print("SOURCE ACCOUNT")
    print(
        "  Account ID : {}".format(
            source["account_id"]
        )
    )

    print(
        "  Bucket     : {}".format(
            source["bucket"]
        )
    )

    print(
        "  Path       : {}".format(
            source["path"]
        )
    )

    print()
    print("DESTINATION ACCOUNT")
    print(
        "  Account ID : {}".format(
            destination["account_id"]
        )
    )

    print(
        "  Bucket     : {}".format(
            destination["bucket"]
        )
    )

    print(
        "  Path       : {}".format(
            destination["path"]
        )
    )

    # --------------------------------------------------------
    # Create clients
    # --------------------------------------------------------

    try:

        print()
        print("Connecting to source StorageGRID...")

        source_client = create_s3_client(
            config,
            "SOURCE"
        )

        print("Source client created.")

        print()
        print("Connecting to destination StorageGRID...")

        destination_client = create_s3_client(
            config,
            "DESTINATION"
        )

        print("Destination client created.")

    except Exception as exc:

        print()
        print("ERROR: Unable to create S3 clients.")
        print(exc)

        logging.error(
            "Unable to create clients: %s",
            exc
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Check buckets
    # --------------------------------------------------------

    if not check_bucket_access(
        source_client,
        source["bucket"],
        "SOURCE"
    ):

        sys.exit(1)

    if not check_bucket_access(
        destination_client,
        destination["bucket"],
        "DESTINATION"
    ):

        sys.exit(1)

    # --------------------------------------------------------
    # Read source files
    # --------------------------------------------------------

    source_objects = list_source_files(
        source_client,
        source["bucket"],
        source["path"]
    )

    # --------------------------------------------------------
    # Display source files
    # --------------------------------------------------------

    display_source_files(
        source_objects
    )

    if not source_objects:

        print()
        print("No files found. Nothing to copy.")

        logging.info(
            "No source files found."
        )

        return

    # --------------------------------------------------------
    # Destination
    # --------------------------------------------------------

    destination_objects = get_destination_objects(
        destination_client,
        destination["bucket"],
        destination["path"]
    )

    print(
        "Destination objects currently found: {}".format(
            len(destination_objects)
        )
    )

    # --------------------------------------------------------
    # Final confirmation
    # --------------------------------------------------------

    print()
    print("=" * 80)
    print("COPY CONFIRMATION")
    print("=" * 80)

    print(
        "Source Account      : {}".format(
            source["account_id"]
        )
    )

    print(
        "Source Bucket       : {}".format(
            source["bucket"]
        )
    )

    print(
        "Source Path         : {}".format(
            source["path"]
        )
    )

    print()

    print(
        "Destination Account : {}".format(
            destination["account_id"]
        )
    )

    print(
        "Destination Bucket  : {}".format(
            destination["bucket"]
        )
    )

    print(
        "Destination Path    : {}".format(
            destination["path"]
        )
    )

    print()

    print(
        "Files to copy       : {}".format(
            len(source_objects)
        )
    )

    print("=" * 80)

    confirmed = ask_confirmation(
        "Can I copy the data to the destination account and path?"
    )

    if not confirmed:

        print()
        print("Copy cancelled.")
        print("No source data was changed.")

        logging.info(
            "User cancelled the transfer."
        )

        return

    # --------------------------------------------------------
    # Perform copy
    # --------------------------------------------------------

    success = perform_transfer(
        source_client=source_client,
        destination_client=destination_client,
        source=source,
        destination=destination,
        source_objects=source_objects,
        destination_objects=destination_objects
    )

    if not success:

        sys.exit(1)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
