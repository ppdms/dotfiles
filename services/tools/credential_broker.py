"""UID-authenticated credential custody, native encrypted storage, and offline import."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import time

from credential_client import MAX_MESSAGE, _unique, decode_bytes, request
from credential_validation import FIELDS, MUTABLE, parse_payload, payload_bytes, validate_fields
from principals import manifest, tool


class BrokerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return None if value is None else hashlib.sha256(value).hexdigest()


def private_directory(path, owner=None):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != (os.getuid() if owner is None else owner)
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise BrokerError("unavailable")


def private_read(path, owner=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != (os.getuid() if owner is None else owner)
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise BrokerError("unavailable")
        return stream.read()


def atomic_ciphertext(path, value):
    """Only encrypted bytes may pass here; fsync both inode and directory."""
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def require_host_identity():
    # Never call setup, rotate, or allow encrypt to manufacture a host identity.
    info = Path("/var/lib/systemd/credential.secret").lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise BrokerError("unavailable")


class NativeCrypto:
    def _creds(self, operation, component, value, user=True, privileged=False):
        require_host_identity()
        executable = os.environ.get("HOMELAB_SYSTEMD_CREDS", "/run/current-system/sw/bin/systemd-creds")
        arguments = [executable]
        if user:
            arguments.append("--user")
        arguments += ["--name=" + component]
        if operation == "encrypt":
            arguments.append("--with-key=host+tpm2")
        arguments += [operation, "-", "-"]
        if privileged and os.getuid() != 0:
            arguments = [tool("sudo"), "-n", "--", *arguments]
        result = subprocess.run(arguments, input=value, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=120, check=False)
        if result.returncode or not result.stdout:
            raise BrokerError("unavailable")
        return result.stdout

    def encrypt(self, component, value):
        return self._creds("encrypt", component, value)

    def decrypt(self, component, value):
        return self._creds("decrypt", component, value)

    def seed_encrypt(self, component, value):
        return self._creds("encrypt", component, value, user=False, privileged=True)

    def seed_decrypt(self, component, value):
        return self._creds("decrypt", component, value, user=False, privileged=True)

    def export(self, recipient, value):
        executable = os.environ.get("HOMELAB_AGE", "/run/current-system/sw/bin/age")
        result = subprocess.run([executable, "--encrypt", "--recipient", recipient], input=value,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60, check=False)
        if result.returncode or not result.stdout.startswith(b"age-encryption.org/v1\n"):
            raise BrokerError("unavailable")
        return result.stdout


class Policy:
    """A snapshot of the immutable enabled-role registry, never request claims."""
    def __init__(self, registry):
        self.registry = registry
        self.admin_uids = {0, registry["operator"]["uid"]}
        self.credentials_uid = registry["definitions"]["credentials"]["uid"]
        self.scopes = {}
        self.export_uids = set(self.admin_uids)
        for name, role in registry["principals"].items():
            uid = role["uid"]
            components = role["components"]
            if (type(uid) is not int or uid <= 0 or uid in self.admin_uids or uid in self.scopes
                    or not isinstance(components, list) or any(component not in FIELDS for component in components)):
                raise BrokerError("invalid")
            self.scopes[uid] = frozenset(components)
            if name == "secrets-export":
                self.export_uids.add(uid)
        if self.credentials_uid not in self.scopes:
            raise BrokerError("invalid")

    def authorize(self, uid, operation, component):
        if operation == "initialize":
            allowed = uid == 0
        elif operation == "export":
            allowed = uid in self.export_uids
        elif uid in self.admin_uids:
            allowed = True
        else:
            allowed = component in self.scopes.get(uid, ()) and operation in {"read", "mutable"}
            if operation == "mutable":
                allowed = allowed and component in MUTABLE
        if not allowed:
            raise BrokerError("denied")


class EncryptedStore:
    def __init__(self, directory, crypto):
        self.directory = Path(directory)
        self.crypto = crypto
        private_directory(self.directory)
        private_directory(self.directory / "backups")

    @contextmanager
    def lock(self, component):
        descriptor = os.open(self.directory / (component + ".lock"),
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BrokerError("unavailable")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def ciphertext(self, component):
        try:
            return private_read(self.directory / (component + ".cred"))
        except FileNotFoundError:
            return None

    def read(self, component):
        encrypted = self.ciphertext(component)
        if encrypted is None:
            return None
        value = self.crypto.decrypt(component, encrypted)
        parse_payload(component, value, complete=False)
        return value

    def commit(self, component, value, expected_digest):
        """Caller holds the store lock; encrypted pre-update backups support rollback."""
        parse_payload(component, value, complete=False)
        original_ciphertext = self.ciphertext(component)
        original = self.read(component)
        if digest(original) != expected_digest:
            raise BrokerError("conflict")
        if original == value:
            return
        encrypted = self.crypto.encrypt(component, value)
        if self.crypto.decrypt(component, encrypted) != value:
            raise BrokerError("unavailable")
        if original_ciphertext is not None:
            backup = self.directory / "backups" / (component + "." + str(time.time_ns()) + ".cred")
            atomic_ciphertext(backup, original_ciphertext)
            if self.crypto.decrypt(component, private_read(backup)) != original:
                raise BrokerError("unavailable")
        target = self.directory / (component + ".cred")
        try:
            atomic_ciphertext(target, encrypted)
            if self.read(component) != value:
                raise BrokerError("unavailable")
        except BaseException:
            try:
                if original_ciphertext is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_ciphertext(target, original_ciphertext)
                if self.read(component) != original:
                    raise BrokerError("rollback")
            except BaseException:
                raise BrokerError("rollback") from None
            raise BrokerError("unavailable") from None


class Broker:
    KEYS = {
        "read": {"operation", "component"},
        "export": {"operation", "component"},
        "store": {"operation", "component", "payload", "expected_digest"},
        "mutable": {"operation", "component", "field", "value", "expected_digest"},
        "initialize": {"operation", "component", "payload"},
    }

    def __init__(self, policy, store):
        self.policy = policy
        self.store = store

    def dispatch(self, uid, message):
        if not isinstance(message, dict):
            raise BrokerError("invalid")
        operation = message.get("operation")
        component = message.get("component")
        if not isinstance(operation, str) or operation not in self.KEYS or not isinstance(component, str) or component not in FIELDS:
            raise BrokerError("invalid")
        if operation == "read":
            # "complete" is the only optional protocol key: it asks for full
            # content validation (credential_validation) before delivery.
            if set(message) not in ({"operation", "component"}, {"operation", "component", "complete"}) \
                    or ("complete" in message and type(message["complete"]) is not bool):
                raise BrokerError("invalid")
        elif set(message) != self.KEYS[operation]:
            raise BrokerError("invalid")
        self.policy.authorize(uid, operation, component)
        with self.store.lock(component):
            original = self.store.read(component)
            if operation == "read":
                if message.get("complete") and original is not None:
                    # Raises ValueError, mapped to "invalid" by the handler.
                    validate_fields(component, parse_payload(component, original, complete=False), complete=True)
                return {"payload": None if original is None else base64.b64encode(original).decode(), "digest": digest(original)}
            if operation == "export":
                encrypted = None if original is None else self.store.crypto.export(self.policy.registry["backup"]["ageRecipient"], original)
                return {"ciphertext": None if encrypted is None else base64.b64encode(encrypted).decode()}
            if operation == "initialize":
                # A stale seed must never replay over subsequent credential changes.
                if original is not None:
                    return {"preserved": True}
                value = decode_bytes(message["payload"])
                self.store.commit(component, value, None)
                return {"preserved": False}
            expected = message["expected_digest"]
            if expected is not None and (not isinstance(expected, str) or len(expected) != 64
                                         or any(character not in "0123456789abcdef" for character in expected)):
                raise BrokerError("invalid")
            if operation == "store":
                # Only operator/root reaches this operation; services have no full-record writer.
                value = decode_bytes(message["payload"])
                self.store.commit(component, value, expected)
                return {"digest": digest(value)}
            field = message["field"]
            value = message["value"]
            if field != MUTABLE.get(component) or not isinstance(value, str) or original is None:
                raise BrokerError("denied")
            fields = parse_payload(component, original, complete=False)
            if digest(fields.get(field, "").encode()) != expected:
                raise BrokerError("conflict")
            fields[field] = value
            updated = payload_bytes(component, fields)
            self.store.commit(component, updated, digest(original))
            return {"digest": digest(value.encode())}


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.connection.settimeout(30)
            _, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            line = self.rfile.readline(MAX_MESSAGE + 1)
            if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                raise BrokerError("invalid")
            message = json.loads(line, object_pairs_hook=_unique)
            result = {"ok": True, **self.server.broker.dispatch(uid, message)}
        except BrokerError as error:
            result = {"ok": False, "error": error.code}
        except (ValueError, TypeError, UnicodeError, RecursionError):
            result = {"ok": False, "error": "invalid"}
        except Exception:
            result = {"ok": False, "error": "unavailable"}
        try:
            self.wfile.write(json.dumps(result, allow_nan=False).encode() + b"\n")
        except OSError:
            pass


class CredentialServer(socketserver.UnixStreamServer):
    # socketserver's default handler prints exception tracebacks. Never log raw
    # requests, command output, exception details, or decrypted field values.
    def handle_error(self, request, client_address):
        print("Credential request failed", file=sys.stderr)


def remove_stale_socket(path):
    try:
        original = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(original.st_mode) or original.st_uid != os.getuid():
        raise BrokerError("unavailable")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                raise BrokerError("unavailable") from None
        else:
            # A second broker must never detach a still-serving broker's socket.
            raise BrokerError("unavailable")
    current = path.lstat()
    if (not stat.S_ISSOCK(current.st_mode) or current.st_uid != os.getuid()
            or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)):
        raise BrokerError("unavailable")
    path.unlink()


def notify_ready():
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
        notification.settimeout(5)
        notification.sendto(b"READY=1\n", address)


def serve(registry):
    policy = Policy(registry)
    if os.getuid() != policy.credentials_uid:
        raise BrokerError("denied")
    require_host_identity()
    store = EncryptedStore(registry["credentialStore"], NativeCrypto())
    path = Path(registry["socket"])
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise BrokerError("unavailable")
    remove_stale_socket(path)
    server = CredentialServer(str(path), RequestHandler)
    server.broker = Broker(policy, store)
    identity = path.lstat()
    def terminate(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        path.chmod(0o666)
        # Type=notify only releases root initialization after custody and the
        # peer-authenticated listening socket are both completely established.
        notify_ready()
        server.serve_forever()
    finally:
        signal.signal(signal.SIGTERM, previous)
        server.server_close()
        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        if (current is not None and stat.S_ISSOCK(current.st_mode) and current.st_uid == os.getuid()
                and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)):
            path.unlink()


def selected_components(registry, requested):
    return requested or sorted({component for role in registry["principals"].values() for component in role["components"]})


def initialize(registry, components):
    if os.getuid() != 0:
        raise BrokerError("denied")
    crypto = NativeCrypto()
    for component in selected_components(registry, components):
        # Read the broker first; initialized components do not require seed files.
        existing = request("read", component)
        if existing["payload"] is not None:
            print("Broker credential already available: " + component)
            continue
        seed = Path(registry["credentialSeedDirectory"]) / (component + ".cred")
        encoded = private_read(seed, owner=registry["operator"]["uid"])
        value = crypto.seed_decrypt(component, encoded)
        parse_payload(component, value, complete=False)
        result = request("initialize", component, payload=base64.b64encode(value).decode())
        if not result["preserved"]:
            current = request("read", component)
            if decode_bytes(current["payload"]) != value:
                raise BrokerError("unavailable")
        print("Initialized broker credential " + component)


def original_record(component):
    result = subprocess.run([os.environ.get("HOMELAB_SECRET_TOOL", "/run/current-system/sw/bin/secret-tool"),
                             "lookup", "application", "homelab", "component", component, "field", "credentials"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise BrokerError("unavailable")
    # secret-tool appends exactly one output newline, not part of the stored secret.
    return result.stdout.removesuffix(b"\n")


def import_seed(registry, components, homeassistant_secrets):
    if os.getuid() == 0 or os.getuid() != registry["operator"]["uid"]:
        raise BrokerError("denied")
    directory = Path(registry["credentialSeedDirectory"])
    if not directory.is_absolute() or str(directory).startswith("/nix/store/"):
        raise BrokerError("invalid")
    private_directory(directory)
    crypto = NativeCrypto()
    for component in selected_components(registry, components):
        value = original_record(component)
        source_snapshot = None
        if component == "homeassistant":
            runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{registry['operator']['uid']}"))
            source = Path(homeassistant_secrets) if homeassistant_secrets else runtime / "homelab-secrets/homeassistant/secrets.yaml"
            if homeassistant_secrets or source.exists():
                try:
                    current = private_read(source).decode("utf-8")
                except (OSError, UnicodeError, BrokerError):
                    print("Home Assistant secret input must be a private UTF-8 regular file", file=sys.stderr)
                    raise BrokerError("unavailable") from None
                # The container overlays /config/secrets.yaml with this runtime
                # file; the physical config-tree copy is not its active input.
                if value is None or parse_payload(component, value, complete=False).get("secrets.yaml") != current:
                    value = payload_bytes(component, {"secrets.yaml": current})
            if value is not None:
                physical = Path(registry["physicalStateRoot"]) / "homeassistant/config/secrets.yaml"
                if physical.exists() and physical.lstat().st_size:
                    original = private_read(physical).decode("utf-8")
                    if original != parse_payload(component, value, complete=False).get("secrets.yaml"):
                        source_snapshot = payload_bytes(component, {"secrets.yaml": original})
        if value is None:
            print("Required credential input is absent: " + component, file=sys.stderr)
            raise BrokerError("unavailable")
        parse_payload(component, value, complete=False)
        seeds = [(directory / (component + ".cred"), value)]
        if source_snapshot is not None:
            seeds.append((directory / ".homeassistant-physical.cred", source_snapshot))
        for target, original in seeds:
            if target.exists():
                if crypto.seed_decrypt(component, private_read(target)) != original:
                    raise BrokerError("conflict")
                print("Verified encrypted seed for " + component + "; unchanged")
                continue
            encrypted = crypto.seed_encrypt(component, original)
            if crypto.seed_decrypt(component, encrypted) != original:
                raise BrokerError("unavailable")
            atomic_ciphertext(target, encrypted)
            if crypto.seed_decrypt(component, private_read(target)) != original:
                raise BrokerError("unavailable")
            print("Staged verified encrypted seed for " + component)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("serve")
    for operation in ("initialize", "import-seed"):
        command = commands.add_parser(operation)
        command.add_argument("components", nargs="*", metavar="COMPONENT")
        if operation == "import-seed":
            command.add_argument("--homeassistant-secrets")
    args = parser.parse_args(argv)
    if args.operation != "serve" and any(component not in FIELDS for component in args.components):
        parser.error("unknown credential component")
    os.umask(0o077)
    try:
        registry = manifest()
        # The bootstrap client must use the exact installed immutable registry socket.
        os.environ["HOMELAB_CREDENTIAL_SOCKET"] = registry["socket"]
        if args.operation == "serve":
            serve(registry)
        elif args.operation == "initialize":
            initialize(registry, args.components)
        else:
            import_seed(registry, args.components, args.homeassistant_secrets)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Credential operation failed; check required credential inputs, private files, authorization and encrypted backups", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
