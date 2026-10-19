# Extract format (`mnemoloci.extract.v1`)

Write one JSON file per packet, at the packet's `extract_path`:

```json
{
  "schema": "mnemoloci.extract.v1",
  "source_key": "<copy from packet>",
  "sha256": "<copy from packet>",
  "source_language": "en",
  "concepts": [
    {
      "title": "Transaction engine",
      "translated_title": "Motore transazionale",
      "kind": "concept",
      "aliases": ["Txn engine"],
      "summary": "Scrive nel vault con piano approvato, journal e rollback.",
      "claims": [
        {"text": "Ogni scrittura richiede un hash di approvazione.", "lines": [12, 14]}
      ],
      "confidence": 0.9,
      "provenance": "extracted",
      "related": ["Lock di vault"],
      "contradicts": []
    }
  ]
}
```

## Fields

**`source_language`**: the ISO 639-1 code of the source text (`en`, `it`, …). Run `mnemoloci config show` for the vault's `language.target`.

**`title`**: the concept's name as the source writes it.

**`translated_title`**: required when `source_language` differs from the target language. It becomes the page title and slug; the original title is kept as an alias. Use the established target-language term if one exists ("Motore transazionale"). Keep proper names and product names untranslated ("Claude Code", "Obsidian"), repeating them as the `translated_title`.

**`kind`** is one of:
- `concept`: an idea, technique, pattern or rule;
- `entity`: a specific named thing, such as a product, service, person, library or file format;
- `comparison`: X versus Y;
- `overview`: a map of an area, such as the architecture of a whole project.

**`entity_type`** is optional and applies only to entities. It is one of `person`, `organization`, `place`, `tool`, `project` or `event`. Any other value is rejected. People ranked in the app's Persone view come from `person` entities.

**`claims`** must be a non-empty list. Each `lines` value is `[start, end]` inclusive, taken from the packet line numbers. The engine rejects ranges outside the file.

**`provenance`** is one of:
- `extracted`: stated in the source;
- `inferred`: your own conclusion;
- `ambiguous`: the source is unclear or conflicting.

**`related`** lists titles of other concepts, from this file or ones you know exist. The engine resolves them to page slugs.

**`contradicts`** lists titles of concepts this one is in tension with. It sends the page to review.

## Validation

The extract is rejected with details if any of these is true:
- the schema or hash doesn't match the packet;
- a `translated_title` is missing when the source is in another language;
- a field has the wrong type;
- `confidence` is outside 0–1;
- `claims` is empty;
- a line range is outside the file.
