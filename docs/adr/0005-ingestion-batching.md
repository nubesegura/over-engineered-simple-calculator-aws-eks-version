# ADR 0005: Save CSV rows in batches

Status: Accepted (2026-10-13)

## Context

The ingestion worker took a connection and committed once per row. A file of 5 MB (the maximum) can hold about 150,000 rows; at a few milliseconds each that approaches the 300 second visibility timeout of the queue, so a large valid file could be delivered twice. The result stays correct (row ids derive from the content and the second delivery finds the file already moved) but the work is wasted.

## Decision

The repository port has `save_many`, which inserts a sequence of rows with one `executemany` and one transaction, using `ON CONFLICT DO NOTHING`. The use case parses and validates the whole file first, then saves the valid rows in batches of `INGEST_BATCH_SIZE` (500 by default, 1 to 5000) and returns how many were created; the rest of the valid rows are counted as duplicates. The visibility timeout stays at 300 seconds and the file limit at 5 MB.

## Consequences

- A failure in a later batch leaves the file in `incoming/`; the retry skips the rows already stored, so no row is lost or duplicated.
- The first batches of a failed file are visible in the history before the file is moved.
- Rows with operands that the domain rejects (including `NaN` and out-of-range exponents) are reported as invalid lines of the file; they no longer abort the whole file.
