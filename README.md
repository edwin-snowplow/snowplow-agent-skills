# Snowplow AI Assistant Plugin

Official Snowplow plugin for AI coding assistants. Turn your AI into a Snowplow expert — validate schemas, instrument tracking, debug data quality issues and build data models directly from your editor.

Works with Claude Code, Claude and any MCP-compatible AI assistant.

## Requirements
- MCP-compatible client – Claude Code, Cursor, Codex, or Claude

## Installation

You can install the skills from this repo using the Skills CLI. You can use the following command:

```shell
npx skills@latest add https://github.com/jrpeck1989/snowplow-agent-skills
```

You will then be walked through an installation wizard to install your chosen skills into your agent harnesses of choice (Claude Code, Cursor etc)

## What's Included

### Tracking Design
These skills help product managers and analysts translate business goals into well-structured Snowplow tracking plans — from defining requirements through to publishing schemas ready for implementation.

|Skill|What it does|
|---|---|
| skill-name | skill-description 
| skill-name | skill-description 

### Tracking Implementation and Testing
These skills help developers instrument tracking across web, mobile, and server-side surfaces — and validate it locally before it reaches production.

|Skill|What it does|
|---|---|
| `fetch-snowplow-docs` | Fetches current Snowplow tracker documentation via the docs sitemap for any platform (web, iOS, Android, server-side). Use to get accurate, up-to-date tracker API examples before instrumenting. |
| `install-snowplow-tracking` | Install and configure Snowplow tracking in web, iOS, or Android applications with out-of-the-box event tracking and user identification. |
| `setup-micro` | Sets up Snowplow Micro for local testing of tracking implementations, with support for mounting custom schemas and configuring enrichments. |
| `validate-snowplow-implementation` | Test and validate Snowplow tracking implementations against Micro, checking for good/bad events, validation errors, and common implementation issues before deploying to production. |
| `debug-failed-events` | Debug Snowplow events that fail validation in Micro or production pipelines. Systematically diagnose schema errors, field mismatches, and missing properties. |


### Data Analytics and Data Modeling
These skills help analysts run ad-hoc queries in the warehouse and help analytics engineers configure, extend, and customise Snowplow's dbt packages.

|Skill|What it does|
|---|---|
| skill-name | skill-description 
| skill-name | skill-description 

### Data Quality and Governance
These skills help data governance managers audit schema quality, review pipeline configuration and maintain compliance posture across the organisation.

|Skill|What it does|
|---|---|
| skill-name | skill-description 
| skill-name | skill-description 

### Tracking Migration Guides
These skills help teams migrating to Snowplow from other analytics platforms, with opinionated guides and automation for the most common paths.

|Skill|What it does|
|---|---|
| skill-name | skill-description 
| skill-name | skill-description 

## Documentation


## Support

If you have any issues or feedback, create a Github issue.