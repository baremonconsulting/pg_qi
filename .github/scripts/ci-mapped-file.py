#!/usr/bin/env python3

import os
import pathlib
import re
import sys
import hashlib


if len(sys.argv) != 3:
    raise SystemExit("usage: ci-mapped-file.py PID BASENAME_REGEX")
pid, pattern = sys.argv[1:]
matcher = re.compile(pattern + r"$")
matches = {}
for line in pathlib.Path(f"/proc/{int(pid)}/maps").read_text().splitlines():
    fields = line.split(maxsplit=5)
    if len(fields) != 6:
        continue
    candidate = fields[5]
    deleted = candidate.endswith(" (deleted)")
    match_path = candidate[:-10] if deleted else candidate
    if not matcher.search(match_path):
        continue
    if deleted:
        raise SystemExit("mapped file includes a deleted mapping")
    device, inode_text, path = fields[3], fields[4], candidate
    major_text, minor_text = device.split(":", 1)
    identity = (int(major_text, 16), int(minor_text, 16), int(inode_text), path)
    matches[identity] = path
if not matches:
    raise SystemExit("mapped file not found")
if len(matches) != 1:
    raise SystemExit("mapped file identity is ambiguous")
(mapped_major, mapped_minor, mapped_inode, path), _ = matches.popitem()
descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
stat = os.fstat(descriptor)
if (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino) != (
    mapped_major, mapped_minor, mapped_inode
):
    os.close(descriptor)
    raise SystemExit(f"mapped file no longer names the loaded inode: {path}")
digest = hashlib.sha256()
with os.fdopen(descriptor, "rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
print(f"{path}\t{digest.hexdigest()}")
