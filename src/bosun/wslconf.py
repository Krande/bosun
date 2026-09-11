"""Pure editing of ``/etc/wsl.conf``.

``wsl.conf`` is an INI file WSL reads at distro start. bosun needs two keys in
it: ``[boot] systemd=true`` (the engine's service unit needs systemd as PID 1)
and ``[user] default=<name>`` (so interactive shells land on the right account).

The editing is a set of string->string functions with no I/O, because this is
the step most likely to silently corrupt a working machine: the file may already
contain sections bosun knows nothing about — ``[network]``, ``[interop]``,
``[automount]`` — and clobbering them breaks DNS or drive mounts in ways that
surface much later. So every function here preserves unknown sections, comments,
blank lines and ordering, and the tests assert exactly that.
"""

from __future__ import annotations


def _is_section(line: str) -> bool:
    s = line.strip()
    return s.startswith("[") and s.endswith("]") and len(s) > 2


def _section_name(line: str) -> str:
    return line.strip()[1:-1].strip().lower()


def _key_of(line: str) -> str | None:
    """The key on an assignment line, or None for comments/blanks/sections."""
    s = line.strip()
    if not s or s.startswith("#") or s.startswith(";") or _is_section(s):
        return None
    key, sep, _ = s.partition("=")
    return key.strip().lower() if sep else None


def get(text: str, section: str, key: str) -> str | None:
    """Return the value of ``key`` in ``section``, or None if absent."""
    section, key = section.lower(), key.lower()
    current = ""
    for line in text.splitlines():
        if _is_section(line):
            current = _section_name(line)
            continue
        if current == section and _key_of(line) == key:
            return line.split("=", 1)[1].strip()
    return None


def set_key(text: str, section: str, key: str, value: str) -> str:
    """Return ``text`` with ``section.key`` set to ``value``.

    Updates the key in place when present, appends it to an existing section
    when absent, and appends a whole new section when that is absent too.
    Everything else in the file is passed through untouched.
    """
    section, key_l = section.lower(), key.lower()
    lines = text.splitlines()
    out: list[str] = []
    current = ""
    done = False
    # Index just past the last line of the target section, for appending.
    insert_at: int | None = None
    seen_section = False

    for line in lines:
        if _is_section(line):
            # Leaving the target section without having found the key: remember
            # where to insert, which is after the last non-blank line seen.
            if current == section and not done and insert_at is None:
                insert_at = len(out)
                while insert_at > 0 and not out[insert_at - 1].strip():
                    insert_at -= 1
            current = _section_name(line)
            if current == section:
                seen_section = True
            out.append(line)
            continue

        if current == section and _key_of(line) == key_l and not done:
            out.append(f"{key}={value}")
            done = True
            continue

        out.append(line)

    if done:
        return "\n".join(out) + "\n"

    if seen_section:
        if insert_at is None:  # target section ran to end of file
            insert_at = len(out)
            while insert_at > 0 and not out[insert_at - 1].strip():
                insert_at -= 1
        out.insert(insert_at, f"{key}={value}")
        return "\n".join(out) + "\n"

    # No such section anywhere — append one.
    if out and out[-1].strip():
        out.append("")
    out.append(f"[{section}]")
    out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def with_systemd(text: str, enabled: bool = True) -> str:
    """Ensure ``[boot] systemd=`` reflects ``enabled``."""
    return set_key(text, "boot", "systemd", "true" if enabled else "false")


def with_default_user(text: str, user: str) -> str:
    """Ensure ``[user] default=<user>``."""
    return set_key(text, "user", "default", user)


def systemd_enabled(text: str) -> bool:
    """True when ``[boot] systemd`` is set to a truthy value."""
    return (get(text, "boot", "systemd") or "").strip().lower() in ("true", "1", "yes", "on")


def default_user(text: str) -> str | None:
    return get(text, "user", "default")
