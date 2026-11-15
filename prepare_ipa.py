#!/usr/bin/env python3
"""Repack a locally supplied iOS IPA as input to a third-party signer."""

import argparse
import os
import plistlib
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, ZipInfo


FINANCE_BACKGROUND = "Extensions/FinanceKitBackgroundDeliveryExtension.appex"
CHUNK_SIZE = 1024 * 1024


def selected_entries(archive, omit_finance_background):
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("The IPA contains duplicate ZIP entry names.")

    apps = [name for name in names if len(name.split("/")) == 3
            and name.startswith("Payload/") and name.endswith(".app/Info.plist")]
    if len(apps) != 1:
        raise ValueError("Expected exactly one app with an Info.plist under Payload/.")

    app_root = apps[0].removesuffix("Info.plist")
    app_info = plistlib.loads(archive.read(apps[0]))
    executable = app_info.get("CFBundleExecutable")
    if not isinstance(executable, str) or "/" in executable or app_root + executable not in names:
        raise ValueError("The app executable declared in Info.plist is missing.")

    finance_root = app_root + FINANCE_BACKGROUND + "/"
    if omit_finance_background:
        extension_info = plistlib.loads(archive.read(finance_root + "Info.plist"))
        point = extension_info.get("EXAppExtensionAttributes", {}).get("EXExtensionPointIdentifier")
        if point != "com.apple.financekit.background-delivery":
            raise ValueError("The expected FinanceKit background extension could not be verified.")

    entries = []
    for entry in archive.infolist():
        name = entry.filename
        if name != "Payload/" and not name.startswith(app_root):
            continue
        if omit_finance_background and name.startswith(finance_root):
            continue
        if ".." in PurePosixPath(name).parts or "\\" in name:
            raise ValueError(f"Unsafe ZIP entry name: {name}")
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise ValueError(f"Unexpected symlink in iOS app bundle: {name}")
        entries.append(entry)
    return entries


def prepare_ipa(source, destination, omit_finance_background=False):
    if source.resolve() == destination.resolve():
        raise ValueError("Choose an output path different from the input IPA.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        candidate = Path(tmp) / destination.name
        with ZipFile(source) as original, ZipFile(candidate, "w") as output:
            entries = selected_entries(original, omit_finance_background)
            for entry in entries:
                clean = ZipInfo(entry.filename, entry.date_time)
                clean.create_system = entry.create_system
                clean.external_attr = entry.external_attr
                clean.compress_type = entry.compress_type
                clean.file_size = entry.file_size
                with original.open(entry) as payload, output.open(clean, "w") as target:
                    shutil.copyfileobj(payload, target, CHUNK_SIZE)

        with ZipFile(candidate) as output:
            expected = [(entry.filename, entry.CRC, entry.file_size) for entry in entries]
            actual = [(entry.filename, entry.CRC, entry.file_size) for entry in output.infolist()]
            if actual != expected or output.testzip() is not None:
                raise ValueError("The repacked IPA failed ZIP payload verification.")

        os.replace(candidate, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Local IPA supplied by the user")
    parser.add_argument("output", type=Path, help="Destination IPA for re-signing")
    parser.add_argument("--without-finance-background", action="store_true",
                        help="Omit only the FinanceKit background extension for an isolated test")
    args = parser.parse_args()
    try:
        prepare_ipa(args.input, args.output, args.without_finance_background)
    except (OSError, BadZipFile, KeyError, ValueError) as error:
        print(f"IPA preparation failed: {error}", file=sys.stderr)
        return 1
    print(f"Verified ZIP payload and wrote {args.output}. Re-sign before installation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
