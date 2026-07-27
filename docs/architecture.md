# SS architecture

SS converts local AI coding histories into a private, searchable recovery
index.

```text
Claude Code ─┐
Codex ───────┼─> source adapters ─> normalized documents ─> SQLite index
Copilot ─────┤                                               │
Cursor ──────┘                                               ├─> FTS
                                                            ├─> fuzzy search
                                                            └─> local embeddings
                                                                     │
query ─> source and time intent ─> retrieval merge ─> session ranking
                                                                     │
                                  evidence-backed cards <────────────┘
                                           │
                           ┌───────────────┼────────────────┐
                           │               │                │
                      native reopen   context packet   archive state
```

## Boundaries

### Adapters

Adapters discover each supported harness and normalize available records into
the same document type. The capability matrix records what each source can
prove. Claude Code and Codex support native reopening. VS Code/Copilot and
Cursor currently support search and packet continuation.

### Retrieval

Full-text search handles exact identifiers. Character trigrams handle common
typos. Local whole-session and per-turn embeddings recover paraphrased intent.
The merger combines those candidates before session-level ranking applies
query coverage, source constraints, recency, and adversarial suppression.

### Cards

Cards are extractive and deterministic. Each field comes from indexed evidence.
The quality gate removes malformed quotation marks, prompt labels, control
characters, truncation fragments, and repeated fields. Weak evidence produces
an explicit fallback.

### State

SS stores its database, embeddings, selectors, context packets, and archive
events in its private data directory. Native Claude Code, Codex, Copilot, and
Cursor stores are read-only inputs.

### Failure handling

Status and audit events commit together. Nested writes use savepoints.
Migrations create backups and validate the completed schema. Lock, storage,
migration, and unsupported-platform failures return distinct error codes.
