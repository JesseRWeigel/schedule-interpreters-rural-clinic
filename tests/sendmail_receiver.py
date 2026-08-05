#!/usr/bin/env python3
"""Sendmail-compatible local recipient delivery agent for verification."""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from email import policy
from email.parser import BytesParser
from pathlib import Path


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 64


def main() -> int:
    arguments = sys.argv[1:]
    if "-f" not in arguments or "--" not in arguments:
        return fail("expected sendmail -i -f SENDER -- RECIPIENT")
    sender_index = arguments.index("-f") + 1
    recipient_index = arguments.index("--") + 1
    if sender_index >= len(arguments) or recipient_index >= len(arguments):
        return fail("sender or recipient is missing")
    sender = arguments[sender_index]
    recipient = arguments[recipient_index]
    root_text = os.environ.get("CIVIC043_TEST_MAIL_ROOT")
    if not root_text:
        return fail("CIVIC043_TEST_MAIL_ROOT is required")

    content = sys.stdin.buffer.read()
    message = BytesParser(policy=policy.default).parsebytes(content)
    if message["From"] != sender or message["To"] != recipient:
        return fail("envelope and message headers do not agree")
    recipient_key = hashlib.sha256(recipient.encode("utf-8")).hexdigest()[:24]
    root = Path(root_text)
    maildir = root / recipient_key
    try:
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        for directory in (maildir, maildir / "tmp", maildir / "new", maildir / "cur"):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        content_key = hashlib.sha256(content).hexdigest()
        target = maildir / "new" / f"{content_key}.eml"
        if target.exists():
            return 0 if target.read_bytes() == content else fail("message collision")
        temporary = maildir / "tmp" / f"{content_key}.{os.getpid()}.{uuid.uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    except OSError as exc:
        return fail(f"local recipient delivery failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
