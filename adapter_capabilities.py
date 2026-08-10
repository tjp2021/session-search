"""Executable adapter capability declarations."""

from __future__ import annotations

import dataclasses
import shutil
import textwrap


@dataclasses.dataclass(frozen=True)
class AdapterCapability:
    source: str
    label: str
    discovery: bool
    user_messages: str
    assistant_messages: str
    native_reopen: bool
    packet_continuation: bool


_CAPABILITIES = (
    AdapterCapability("claude", "Claude Code", True, "full", "full", True, True),
    AdapterCapability("codex", "Codex", True, "full", "partial", True, True),
    AdapterCapability("pi", "Pi", True, "full", "full", True, True),
    AdapterCapability("vscode", "VS Code/Copilot", True, "partial", "partial", False, True),
    AdapterCapability("cursor", "Cursor", True, "partial", "partial", False, True),
)


def capability_matrix() -> dict[str, AdapterCapability]:
    return {item.source: item for item in _CAPABILITIES}


def render_capabilities() -> str:
    headings = ("Source", "Discovery", "User messages", "Assistant messages", "Native reopen", "Packet")
    rows = [
        (
            item.label,
            "yes" if item.discovery else "no",
            item.user_messages,
            item.assistant_messages,
            "yes" if item.native_reopen else "no",
            "yes" if item.packet_continuation else "no",
        )
        for item in _CAPABILITIES
    ]
    terminal_width = max(20, min(shutil.get_terminal_size(fallback=(100, 24)).columns, 200))
    widths = [max(len(str(row[i])) for row in [headings, *rows]) for i in range(len(headings))]
    wide_width = sum(widths) + (2 * (len(headings) - 1))
    if wide_width > terminal_width:
        rendered: list[str] = []
        label_width = max(len(heading) for heading in headings[1:])
        value_width = max(1, terminal_width - label_width - 2)
        for row in rows:
            if rendered:
                rendered.append("")
            rendered.extend(textwrap.wrap(row[0], width=terminal_width) or [""])
            for heading, value in zip(headings[1:], row[1:]):
                wrapped = textwrap.wrap(value, width=value_width) or [""]
                rendered.append(f"{heading.ljust(label_width)}: {wrapped[0]}")
                rendered.extend(f"{' ' * (label_width + 2)}{line}" for line in wrapped[1:])
        return "\n".join(rendered)
    rendered = [
        "  ".join(value.ljust(widths[i]) for i, value in enumerate(headings)),
        "  ".join("-" * width for width in widths),
    ]
    rendered.extend(
        "  ".join(value.ljust(widths[i]) for i, value in enumerate(row))
        for row in rows
    )
    return "\n".join(rendered)
