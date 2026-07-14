<!--
  WORKED EXAMPLE: SKILL.md for snowplow-schema-authoring
  =======================================================
  This is a filled-in example of the SKILL_TEMPLATE.md, showing what a real skill's
  index file looks like when properly structured. Use this for pattern-matching when
  authoring your own skills.

  Note: This example is illustrative. The actual snowplow-schema-authoring skill
  may differ in scope and structure when published.
-->
---
name: snowplow-schema-authoring
description: Helps engineers design, author, and validate Iglu-compliant Snowplow schemas for self-describing events and context entities, applying the conventions and trade-offs that Snowplow's field engineering team uses on enterprise implementations.
---

# snowplow-schema-authoring

## When To Use This Skill

Use this skill when:

- Designing a new self-describing event schema for a tracker implementation
- Authoring a context entity to enrich existing events with structured data
- Reviewing or refactoring an existing schema for better governance and warehouse ergonomics
- Translating a tracking plan or product spec into concrete schema definitions

Don't use this skill when:

- The team needs schema-on-read flexibility (Snowplow's upfront-schema model isn't the right fit)
- Schemas are genuinely throwaway or experimental (the governance overhead isn't worth it for short-lived data)
- The work is configuring an existing schema's downstream model (use `snowplow-dbt-configuration` instead)

## Prerequisites

- `igluctl` installed and on PATH
- Access to the target Iglu registry (Iglu Central for public schemas, or the customer's private registry)
- A clear understanding of what events or entities are being modelled (this skill assumes the design conversation has happened; if it hasn't, start with `snowplow-tracking-plan-design`)

## Instructions

Branch by what you are authoring, then complete the schema, validate, run publish-readiness checks, and escalate failures to troubleshooting. This section is the index that ties **Scripts**, **Reference Documentation**, **Examples**, and **Templates** together.

### Step 1: Confirm readiness

Clarify:

- **Prerequisites** above are satisfied: `igluctl` on PATH, registry access, and clarity on what events or entities are being modelled.

### Step 2: Route by scenario

Choose:

- **New event schema** → read `reference/event_vs_entity.md`, then run `scripts/generate_schema.py --type event`
- **New context entity** → read `reference/event_vs_entity.md`, then run `scripts/generate_schema.py --type entity`
- **Modifying an existing schema** → read `reference/schema_versioning.md` to decide the version bump, then edit the schema file directly
- **Unfamiliar shape or domain** → pattern-match under `examples/` before or while authoring

### Step 3: Complete authoring

Finish:

- Fill `templates/` or generated JSON (properties, types, required fields).
- When warehouse column shape is non-obvious, run `scripts/preview_warehouse_columns.py` before validation.

### Step 4: Validate

Verify:

- Run `scripts/validate.py <path/to/schema.json>` for Iglu compliance and Snowplow conventions.
- Fix errors; address warnings unless there is a documented reason not to.

### Step 5: Publish readiness

Review:

- `reference/warehouse_ergonomics.md` for downstream modelling implications.
- `reference/governance_checklist.md` for registry publish criteria.

### Step 6: Handle failures

Escalate:

- Use `troubleshooting/` for diagnostic steps; frequent cases are listed under **Common Failure Modes**.

## Scripts

- `scripts/generate_schema.py` - Scaffolds an Iglu-compliant schema with required fields and Snowplow conventions pre-applied.
  Usage: `python scripts/generate_schema.py --vendor com.example --name purchase --type event --version 1-0-0`

- `scripts/validate.py` - Runs `igluctl lint` plus Snowplow-specific convention checks. Outputs structured pass/fail with actionable messages.
  Usage: `python scripts/validate.py path/to/schema.json`

- `scripts/preview_warehouse_columns.py` - Shows what column names and types this schema will produce in the warehouse. Useful for catching ergonomic issues before publishing.
  Usage: `python scripts/preview_warehouse_columns.py path/to/schema.json --warehouse snowflake`

## Reference Documentation

- `reference/event_vs_entity.md` - Decision framework for whether something should be modelled as an event or a context entity. Read before scaffolding a new schema.
- `reference/schema_versioning.md` - When to bump major vs minor vs patch versions. Read before modifying any existing schema.
- `reference/warehouse_ergonomics.md` - How schema design choices affect downstream modelling and analyst experience. Read this before finalising any non-trivial schema.
- `reference/governance_checklist.md` - The publish-readiness criteria a schema should meet before being added to a registry.
- `reference/naming_conventions.md` - Snowplow's conventions for vendor namespaces, schema names, and property names.

## Examples

- `examples/ecommerce_purchase/` - A realistically complex purchase event schema with line items, currency handling, and discount modelling. Good pattern for transactional events.
- `examples/page_view_with_context/` - A simple page-view event paired with a content-classification context entity. Good pattern for content-heavy products.
- `examples/agent_interaction/` - A schema for tracking AI agent interactions, including tool calls and decisions. Good pattern for agent observability work.
- `examples/refactor_before_after/` - A real schema refactor showing a poorly-designed v1 and the v2 that fixed its issues. Useful for understanding common design mistakes.

## Templates

- `templates/event_schema.json` - Skeleton for a self-describing event schema. Replace `{{ VENDOR }}`, `{{ NAME }}`, `{{ DESCRIPTION }}`, and define the properties block.
- `templates/entity_schema.json` - Skeleton for a context entity schema with the same placeholders.

## Common Failure Modes

- **Validation fails with "additionalProperties not declared"**: see `troubleshooting/additional_properties.md`. Usually means the schema allows fields you didn't intend to allow, which causes warehouse loading issues.

- **Schema validates but fails to load into Iglu**: see `troubleshooting/iglu_load_failures.md`. Most common cause is a vendor namespace mismatch between the file path and the `self.vendor` field.

- **Validation passes but events get rejected at enrichment**: see `troubleshooting/enrichment_rejection.md`. Schema is structurally valid but doesn't match the event shape the tracker is sending.

- **Warehouse columns explode after schema is published**: see `troubleshooting/column_explosion.md`. Usually a flat-vs-nested design mistake. May require a v2 schema and migration.

- **Free-form fields like prompts or descriptions cause downstream issues**: see `troubleshooting/free_form_fields.md`. Common in agent observability schemas.

## Limits and Alternatives

This skill encodes Snowplow's opinionated approach to schema authoring. It's the right answer when:

- The team is using Snowplow infrastructure (BDP Cloud, Private Managed Cloud, or open source)
- Schema governance, versioning, and validation matter for downstream reliability
- Schemas need to be shareable across multiple trackers, pipelines, or teams
- Warehouse-native modelling is part of the workflow

It's probably not the right approach when:

- The team needs schema-on-read flexibility (consider a JSON-first data modelling pattern instead)
- Schemas are throwaway or experimental and the governance overhead isn't worth it
- The work is downstream of an existing schema and doesn't require authoring new ones (use `snowplow-dbt-configuration` for downstream model work)

For non-Snowplow teams who like the schema-first approach, this skill's reference docs may still be useful as design guidance even if the scripts and templates aren't directly applicable.

## Maintenance Notes

- v1 covers events and entities. Self-describing JSON nested inside entity fields is supported but lightly documented; v2 will expand this.
- The validation script uses `igluctl 1.x`. Teams on `igluctl 0.x` should upgrade or use `--legacy` mode (deprecated, will be removed in v2).
- The agent_interaction example schema is provisional; it will be replaced with a canonical version once `snowplow-agent-observability` reaches v1.
