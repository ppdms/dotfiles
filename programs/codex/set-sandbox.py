"""Manage only an explicitly selected top-level Codex sandbox policy."""
import os
from pathlib import Path
import sys
import tempfile

import tomlkit


SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")


def set_sandbox(target, mode=None):
    if mode is None:
        return
    if mode not in SANDBOX_MODES:
        raise ValueError("Codex sandbox policy is not an approved choice")
    target = Path(target)
    try:
        document = tomlkit.parse(target.read_text(encoding="utf-8") if target.exists() else "")
    except (tomlkit.exceptions.ParseError, UnicodeError):
        raise ValueError("Codex configuration is not valid UTF-8 TOML") from None
    # TOML-aware assignment retains comments, unrelated settings and profiles.
    document["sandbox_mode"] = mode
    content = tomlkit.dumps(document)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".config.toml.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) > 1:
            raise ValueError("Expected one Codex sandbox policy")
        set_sandbox(Path(os.environ["HOME"]) / ".codex/config.toml",
                    arguments[0] if arguments else None)
    except (OSError, ValueError):
        print("Cannot update Codex sandbox policy; configuration left unchanged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
