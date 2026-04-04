# Documentation Index

## Purpose

This index serves as the primary entry point for AI assistants working with the Dahua Home Assistant integration codebase. It provides metadata about each documentation file to help determine which files contain relevant information for a given question.

## How to Use This Index

1. **Start here** — Read this file first to understand what documentation is available
2. **Identify the topic** — Match your question to the relevant documentation file(s) below
3. **Read targeted files** — Only read the specific files needed for your task
4. **Cross-reference** — Use the relationships section to find related information

## Documentation Files

### [codebase_info.md](codebase_info.md)
**When to consult:** Project overview questions, technology stack, directory layout, supported platforms, CI/CD pipeline.
**Contains:** Project metadata, version, directory tree, platform table, CI workflow summary.

### [architecture.md](architecture.md)
**When to consult:** Understanding system design, component relationships, data flow, design patterns, how the coordinator works, event streaming architecture.
**Contains:** System architecture diagram, coordinator pattern, dual event streaming, entity hierarchy, API communication patterns, channel handling.
**Related to:** components.md (detailed component descriptions), workflows.md (runtime behavior)

### [components.md](components.md)
**When to consult:** Understanding what a specific module does, entity types and their responsibilities, which file to modify for a feature.
**Contains:** Detailed descriptions of every module — coordinator, client, VTO client, camera, binary sensor, switch, light, select, config flow, and supporting modules.
**Related to:** architecture.md (how components connect), interfaces.md (APIs they use)

### [interfaces.md](interfaces.md)
**When to consult:** API endpoint details, request/response formats, service definitions, event data structures, RTSP URL format.
**Contains:** Complete CGI API reference, VTO protocol methods, RPC2 API, HA service list, event bus format, RTSP stream URLs.
**Related to:** data_models.md (data structures), components.md (which components use which APIs)

### [data_models.md](data_models.md)
**When to consult:** Understanding data structures, coordinator state format, event payloads, config entry schema, binary protocol framing.
**Contains:** Coordinator data dict keys, event data structures (IP vs VTO), config entry schema, DHIP binary frame format, API response format.
**Related to:** interfaces.md (API that produces the data), workflows.md (how data flows)

### [workflows.md](workflows.md)
**When to consult:** Understanding runtime behavior, setup flow, initialization sequence, event processing pipeline, polling cycle, reconnection logic, service call flow.
**Contains:** Sequence and flow diagrams for setup, initialization, event processing (IP and VTO), periodic polling, reconnection, and service calls.
**Related to:** architecture.md (design context), components.md (which components participate)

### [dependencies.md](dependencies.md)
**When to consult:** Dependency versions, external service requirements, HA framework components used, test dependencies.
**Contains:** Runtime, dev, and test dependency tables, HA framework component usage, external service ports and protocols.

### [review_notes.md](review_notes.md)
**When to consult:** Known documentation gaps, inconsistencies, improvement recommendations.
**Contains:** Review findings, completeness gaps, consistency issues, and recommendations.

## Quick Reference: Common Questions

| Question | File(s) to Read |
|----------|----------------|
| "What does this integration do?" | codebase_info.md |
| "How do I add a new entity type?" | components.md, architecture.md |
| "What API endpoint does X use?" | interfaces.md |
| "How are events processed?" | workflows.md, architecture.md |
| "What's the coordinator state format?" | data_models.md |
| "How does VTO authentication work?" | interfaces.md, components.md (vto.py section) |
| "What services are available?" | interfaces.md |
| "How does feature detection work?" | workflows.md (initialization), architecture.md |
| "What are the dependencies?" | dependencies.md |
| "What's missing or inconsistent?" | review_notes.md |
