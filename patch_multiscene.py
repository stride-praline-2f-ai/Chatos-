#!/usr/bin/env python3
"""Enable multiple scenes in an IPA without rebuilding its ZIP archive."""

import argparse
import os
import plistlib
import re
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile


SCENE_KEY = "UIApplicationSupportsMultipleScenes"


def patch_plist(plist, compressed_size):
    before = plistlib.loads(plist)
    if before.get("UIApplicationSceneManifest", {}).get(SCENE_KEY) is not False:
        raise ValueError("Expected multiple scenes to be disabled in the original plist.")

    pattern = rb"(<key>UIApplicationSupportsMultipleScenes</key>\s*)<false/>"
    updated, count = re.subn(pattern, rb"\g<1><true />", plist)
    before["UIApplicationSceneManifest"][SCENE_KEY] = True
    if count != 1 or len(updated) != len(plist) or plistlib.loads(updated) != before:
        raise ValueError("The plist could not be updated in place.")

    for level in range(1, 10):
        for memory in (8, 9, 7, 6, 5, 4, 3, 2, 1):
            compressor = zlib.compressobj(level, zlib.DEFLATED, -15, memory)
            data = compressor.compress(updated) + compressor.flush()
            if len(data) == compressed_size:
                return data, zlib.crc32(updated)
    raise ValueError("The patched plist does not fit the original compressed ZIP entry.")


def member_locations(file, archive, entry):
    filename = entry.filename.encode("utf-8")
    file.seek(entry.header_offset)
    local_header = file.read(30)
    name_length, extra_length = struct.unpack_from("<HH", local_header, 26)
    if local_header[:4] != b"PK\x03\x04" or file.read(name_length) != filename:
        raise BadZipFile("The original plist ZIP header does not match its filename.")
    data_offset = entry.header_offset + 30 + name_length + extra_length

    file.seek(archive.start_dir)
    for _ in archive.infolist():
        central_offset = file.tell()
        header = file.read(46)
        if header[:4] != b"PK\x01\x02":
            raise BadZipFile("Invalid central directory record.")
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", header, 28)
        recorded_name = file.read(name_length)
        if recorded_name == filename and struct.unpack_from("<I", header, 42)[0] == entry.header_offset:
            return data_offset, central_offset
        file.seek(extra_length + comment_length, os.SEEK_CUR)
    raise BadZipFile("The original plist is missing from the ZIP central directory.")


def patch_ipa(source, destination):
    if source.resolve() == destination.resolve():
        raise ValueError("Choose an output path different from the input IPA.")
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        candidate = Path(tmp) / destination.name
        shutil.copyfile(source, candidate)
        with candidate.open("r+b") as file, ZipFile(file) as archive:
            plists = [name for name in archive.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)]
            if len(plists) != 1:
                raise ValueError("Expected one main app Info.plist under Payload/.")
            entry = archive.getinfo(plists[0])
            if entry.compress_type != ZIP_DEFLATED or entry.flag_bits != 0:
                raise ValueError("Expected a standard Deflate member without a data descriptor.")
            compressed, crc = patch_plist(archive.read(entry), entry.compress_size)
            data_offset, central_offset = member_locations(file, archive, entry)
            new_crc = struct.pack("<I", crc)
            file.seek(entry.header_offset + 14)
            file.write(new_crc)
            file.seek(data_offset)
            file.write(compressed)
            file.seek(central_offset + 16)
            file.write(new_crc)

        with ZipFile(candidate) as archive:
            updated = plistlib.loads(archive.read(plists[0]))
            if updated["UIApplicationSceneManifest"][SCENE_KEY] is not True or archive.testzip() is not None:
                raise ValueError("The patched IPA failed ZIP or scene manifest validation.")
        os.replace(candidate, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Original IPA with multiple scenes disabled")
    parser.add_argument("output", type=Path, help="IPA with multiple scenes enabled")
    args = parser.parse_args()
    try:
        patch_ipa(args.input, args.output)
    except (OSError, BadZipFile, ValueError) as error:
        print(f"IPA patch failed: {error}", file=sys.stderr)
        return 1
    print(f"Patched and verified {args.output}. Re-sign before installation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
