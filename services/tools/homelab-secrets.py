import argparse
import os
from pathlib import Path
import sys

from credential_client import read_record, store_record
from credential_validation import FIELDS, MUTABLE, OPTIONAL, parse_payload, payload_bytes, required_fields, validate_fields
from homelab_client import atomic_write, component_lock, persist, private_directory, private_read, runtime_digest
from homelab_client import prepare as client_prepare, persist_locked


def load_record(component):
    value, digest = read_record(component)
    return ({} if value is None else parse_payload(component, value, complete=False), digest)


def load_fields(component):
    return load_record(component)[0]


def load_field(component, field):
    if component not in FIELDS or field not in FIELDS[component]:
        raise RuntimeError("Unknown credential field")
    value = load_fields(component).get(field)
    return None if value is None else value.encode("utf-8")


def store_fields(component, fields, *, expected_digest):
    """Admin-only complete-record CAS with broker-managed encrypted pre-update backups."""
    validate_fields(component, fields, complete=False)
    store_record(component, payload_bytes(component, fields), expected_digest)


def main():
    parser = argparse.ArgumentParser(description="Deliver scoped broker credentials to private container runtime files")
    parser.add_argument("operation", choices=("prepare", "persist", "import", "status"))
    parser.add_argument("component", choices=FIELDS)
    parser.add_argument("field", nargs="?")
    parser.add_argument("file", nargs="?")
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--replace", action="store_true", help="Explicitly replace an existing encrypted input")
    args = parser.parse_args()
    os.umask(0o077)
    # prepare/persist are the Quadlet-side client operations; they lock and
    # validate through the broker (see homelab_client).
    if args.operation == "prepare":
        client_prepare(args.component, allow_missing=args.allow_missing)
        return
    if args.operation == "persist":
        persist_locked(args.component)
        return
    with component_lock(args.component) as directory:
        if args.operation == "import":
            if args.field not in FIELDS[args.component] or not args.file:
                parser.error("import requires a supported field and a private input file")
            persist(args.component, directory)
            fields, expected_digest = load_record(args.component)
            if not args.replace and args.field in fields:
                print("Encrypted input already available; unchanged: " + args.component + "/" + args.field)
                return
            fields[args.field] = private_read(Path(args.file)).decode("utf-8")
            store_fields(args.component, fields, expected_digest=expected_digest)
            print("Stored " + args.component + "/" + args.field + " in the encrypted broker; restart its consumers to deliver it.")
            return
        # status
        fields = load_fields(args.component)
        missing = sorted(set(required_fields(args.component)) - set(fields))
        if missing:
            print("Missing encrypted inputs for " + args.component + ": " + ", ".join(missing), file=sys.stderr)
            if not args.allow_missing:
                raise RuntimeError("Required encrypted credential inputs must be available before starting this component")
        else:
            validate_fields(args.component, fields)
        if not missing:
            print(args.component + ": all required scoped encrypted inputs available")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Credential operation interrupted; check stored credentials before retrying.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        # Input parser, native backend and socket failures never reach diagnostics.
        print("Secret operation failed; check authorization, input format, broker availability and concurrent edits. Existing runtime files retained.", file=sys.stderr)
        sys.exit(1)
