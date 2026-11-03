# sequentialist-monitor: overview and frequently asked questions

English · [Русский](overview.ru.md)

> An advisory monitoring application that turns equipment readings into model-based change alarms, operator workflows and auditable reports.

## Who it helps

Maintenance, operations and process-engineering teams integrating condition
monitoring into existing systems. It watches shifts and drift, tracks alarm
states and records actions of people.

For a reader without a programming background: choose a reference period of
normal operation, describe the signals and alarm budget, then compare new
readings with the expected behavior. Operators receive alarms and can acknowledge
or shelve them. An engineer reviews the model assumptions and calibration.

The current system advises people. It does not control the physical process,
constitute a safety instrumented system, or establish IEC 61511/SIL compliance.

## Inputs and outputs

| Input | Purpose |
|---|---|
| Plant specification | Units, tags, windows, models, detectors and source mappings |
| Reference history | Fit/calibrate the normal baseline and thresholds |
| Readings from CSV, SQL or read-only Modbus TCP | Supply observations with source ordering and quality |
| Explicit operator commands | Acknowledge, shelve or change supported alarm-service states with actor and reason |

Outputs include alarm events, status/KPI views, JSON/Markdown reports, replay
results and audit records. Notifications can use logs, webhooks or email.

## A practical workflow

1. Describe the unit and its tags; validate with `sequentialist monitor check SPEC`.
2. Calibrate a baseline from an appropriate reference period.
3. Review model residuals and the allocated false-alarm budget.
4. Replay historical data before running the advisory service.
5. Review alarms, operator actions, reports and model suitability over time.

The [synthetic plant example](../examples/synthetic_plant.py) uses generated data
and a SQLite historian. The [plant configuration](../examples/plant.yaml) and
[README](../README.md) describe real command syntax and integration boundaries.

## Current scope and limits

The checked-in package version is 0.1.0. Check [project metadata](../pyproject.toml)
and the [README](../README.md) for current installation/release status.
The implemented pipeline is readings → windows → model → residual → detector →
alarm. It includes calibration, alarm-management states, KPIs, history replay,
CSV/SQL/Modbus inputs, a status/command HTTP service and notifications.

A detector's mathematical assumptions do not automatically hold on raw,
autocorrelated industrial readings. The pipeline's guarantee is `model` while
its normal-operation model holds; simulation-selected thresholds are
`approximate`. Fitted baseline parameters are then treated as known. False-alarm
statements concern a long-run mean, and detection time is not guaranteed.

Modbus input reads supported functions; it does not write device controls.
The data model follows stated open standards, but that does not establish
protocol certification. Current source support should be checked in the README;
planned OPC UA/MQTT, expert-law workflows, diagnostic search and messaging
extensions must not be mistaken for current features.

## Frequently asked questions

### Does an alarm prove a specific equipment fault?

It reports evidence of a change under the configured model and detector. Cause
diagnosis and choice of a repair require additional subject knowledge and checks.

### Can it stop a machine automatically?

The package is advisory. Its current alarms and commands do not implement
process-control writes or safety interlocks.

### Is a false-alarm budget a promise about each hour?

The documented guarantees concern long-run average behavior under assumptions,
not a fixed maximum count in every individual hour.

### Can it run without an internet connection?

The core processing uses the standard library and configured sources. External
notifications and deployment adapters have their own connectivity requirements;
YAML specs use the optional YAML dependency. Actual service deployment remains
an engineering integration task.

### Where do customer plant files and reports belong?

In the customer's controlled deployment. Customer configurations, historical
records and customer-specific integrations must not be published as examples.
The repository uses synthetic fixtures and public specifications.

## Sources and AI-assisted reading

Start with the [README](../README.md), [pipeline](../src/sequentialist/monitor/pipeline.py),
[alarm model](../src/sequentialist/monitor/alarms.py), [tests](../tests/) and
[curated index](../llms.txt). [AGENTS.md](../AGENTS.md) directs coding assistants
to the preserved [owner guidance](../CLAUDE.md), source map and checks.
`QWEN.md` and `GEMINI.md` are entry pointers for compatible clients. Search and
chat assistants can use these ordinary pages as sources; the index does not
promise automatic ingestion, certification or search ranking.
