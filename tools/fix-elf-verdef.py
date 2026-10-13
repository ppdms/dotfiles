"""Repair Bun's ELF version table pointer after patchelf relocates it.

Based on the fix described in OMP's scripts/fix-dt-verdef.ts (issue #9881).
Only ELF64 little-endian binaries are supported by this package.
"""

import struct
import sys


def repair(path):
    with open(path, "r+b") as executable:
        header = executable.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            raise ValueError("expected an ELF64 little-endian executable")

        section_offset = struct.unpack_from("<Q", header, 40)[0]
        section_size, section_count = struct.unpack_from("<HH", header, 58)
        if section_size < 64 or section_count == 0:
            raise ValueError("missing or unsupported ELF section headers")

        executable.seek(section_offset)
        sections = executable.read(section_size * section_count)
        if len(sections) != section_size * section_count:
            raise ValueError("truncated ELF section headers")

        version_address = None
        dynamic = None
        for index in range(section_count):
            base = index * section_size
            section_type = struct.unpack_from("<I", sections, base + 4)[0]
            if section_type == 0x6FFFFFFD:  # SHT_GNU_verdef
                version_address = struct.unpack_from("<Q", sections, base + 16)[0]
            elif section_type == 6:  # SHT_DYNAMIC
                dynamic = struct.unpack_from("<QQ", sections, base + 24)

        if version_address is None:
            return
        if dynamic is None:
            raise ValueError("version definitions exist without a dynamic section")

        dynamic_offset, dynamic_size = dynamic
        executable.seek(dynamic_offset)
        entries = executable.read(dynamic_size)
        if len(entries) != dynamic_size:
            raise ValueError("truncated ELF dynamic section")

        for cursor in range(0, dynamic_size - 15, 16):
            tag, value = struct.unpack_from("<QQ", entries, cursor)
            if tag == 0:  # DT_NULL
                break
            if tag == 0x6FFFFFFC:  # DT_VERDEF
                if value != version_address:
                    executable.seek(dynamic_offset + cursor + 8)
                    executable.write(struct.pack("<Q", version_address))
                    print(f"Repaired DT_VERDEF: {value:#x} -> {version_address:#x}")
                return

        raise ValueError("version definitions exist without a DT_VERDEF entry")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: fix-elf-verdef.py EXECUTABLE")
    repair(sys.argv[1])
