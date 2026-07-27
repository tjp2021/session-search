"""Isolated, synthetic end-to-end demonstration."""

from __future__ import annotations

import pathlib
import tempfile

import session_search as ss
from adapter_capabilities import render_capabilities
from public_corpus import documents, load_corpus


def run_demo() -> str:
    lines = ["SS synthetic demo", "=================", ""]
    with tempfile.TemporaryDirectory(prefix="session-search-demo-") as tmpdir:
        root = pathlib.Path(tmpdir)
        db = root / "session-search.sqlite"
        conn = ss.connect_db(db)
        ss.init_db(conn)
        count = ss.upsert_documents(conn, documents(load_corpus()))
        lines.append(f"Indexed {count} synthetic records in an isolated database.")
        lines.append("")
        lines.append("Capabilities")
        lines.append(render_capabilities())
        lines.append("")

        query = "x402 guard replay protection"
        display, results = ss.run_search(conn, query, 5, "all", "local")
        if not results:
            raise RuntimeError("Synthetic demo query returned no results.")
        row = results[0][0]
        card = ss.session_card_for_result(conn, row, display)
        fields = ss.dashboard_summary(card, ss.session_rows(conn, row), row)
        lines.extend(
            [
                f"Search: {query}",
                f"Top session: {fields[0]}",
                f"State: {fields[1]}",
                f"Resume: {fields[2]}",
                f"Native owner: {ss.source_label(str(row['source']))}",
            ]
        )

        packet = root / "context-packet.md"
        packet.write_text(
            ss.handoff_packet_text(
                row,
                "claude",
                query,
                "1",
                ss.session_rows(conn, row),
                card,
                archived=False,
            ),
            encoding="utf-8",
        )
        lines.append(f"Context packet created: {packet.name}")

        ss.apply_archive_transition(
            conn,
            str(row["source"]),
            str(row["session_id"]),
            True,
            "demo",
            evidence="Synthetic demo archive.",
        )
        archived = ss.session_is_archived(conn, str(row["source"]), str(row["session_id"]))
        ss.apply_archive_transition(
            conn,
            str(row["source"]),
            str(row["session_id"]),
            False,
            "demo",
            evidence="Synthetic demo resume.",
        )
        active = not ss.session_is_archived(conn, str(row["source"]), str(row["session_id"]))
        lines.append(f"Archive lifecycle: archived={str(archived).lower()}, active={str(active).lower()}")
        lines.append("")
        lines.append("Demo data was temporary and has been removed.")
        conn.close()
    return "\n".join(lines)
