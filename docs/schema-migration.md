# Schema migration policy

Schema 2 records are accepted only when their protocol and scoring versions equal the values in `ste.protocol`. Readers do not coerce or pool incompatible experimental semantics.

The supported legacy adapter recognizes only an unversioned JSONL object with `session`, `model`, `variant`, `depth`, and `score`. It adds labels `schema_version=1`, `protocol_version=legacy-original`, `scoring_version=legacy-original`, and `run_mode=research`; it does not alter original values. Malformed lines report their line number without echoing content. Any other historical representation requires a reviewed offline migration that retains the source data and documents its semantic equivalence.

Operational snapshots are not migrated in place because an ambiguous resume can repeat paid calls. Finish a compatible old run with its old release, or start a new schema-2 run with a new run ID. Analytical exports may be migrated only after validation, and legacy/current strata remain separate in leaderboard aggregation.
