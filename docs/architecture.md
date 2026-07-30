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

Cards have two sources. The default is extractive and deterministic: each
field comes from indexed evidence, and weak evidence produces an explicit
fallback. With model summaries turned on, an About line can instead be written
by a model from redacted session text, and the card records which kind it
holds. The dashboard prefers a model summary only when the card's provenance
says so, and treats an evidence card as repairable rather than final. The
quality gate applies to both kinds: malformed quotation marks, prompt labels,
control characters, truncation fragments, and repeated fields never render.

Session text crosses the network boundary in exactly one function, behind an
explicit opt-in, with credential shapes stripped before the request exists.

### State

SS stores its database, embeddings, selectors, context packets, and archive
events in its private data directory. Native Claude Code, Codex, Copilot, and
Cursor stores are read-only inputs.

### Failure handling

Status and audit events commit together. Nested writes use savepoints.
Migrations create backups and validate the completed schema. Lock, storage,
migration, and unsupported-platform failures return distinct error codes.
