# Repository guidance: sequentialist-monitor

Read the existing [owner guidance](CLAUDE.md) and [contribution instructions](CONTRIBUTING.md)
before editing. Preserve those files and existing user changes. User instructions
remain controlling; this file is a source map and workflow aid, not additional
permission to publish, contact people or change sibling repositories.

## Role and source map

sequentialist-monitor is application software in the sequentialist family. Mathematical
procedures belong to sequential-analysis; shared durable execution belongs to
sequentialist-core. Preserve the open PEP 420 `sequentialist` namespace.

- `src/sequentialist/monitor/config.py`, `unitspec.py`: declarative plant/baseline/unit configuration.
- `windows.py`, `pipeline.py`, `calibrate.py`, `budget.py`: windows, forecast residuals, calibration and alarm budgets.
- `alarms.py`, `unit.py`, `kpi.py`, `report.py`: alarm lifecycle, task handling, KPIs and reports.
- `sources.py`, `modbus.py`: CSV/SQL/read-only Modbus input adapters.
- `service.py`, `api.py`, `notify.py`, `cli.py`: advisory service, commands and notifications.
- `tests/`, `examples/`, `tools/check_headers.py`: behavior checks and synthetic examples.

## Development and validation

Python 3.8+ is the runtime floor. Installation/dependency guidance is in
[CONTRIBUTING.md](CONTRIBUTING.md) and [pyproject.toml](pyproject.toml);
[CI](.github/workflows/tests.yml) is the source for the supported version matrix.
From the repository root, use the relevant commands after dependencies are ready:

```sh
python -W error::ResourceWarning -m unittest discover -s tests -t .
ruff check .
mypy
python tools/check_headers.py
sequentialist monitor check examples/plant.yaml
python examples/synthetic_plant.py
```

These are documented repository checks, not a report that this session ran them.
For documentation-only edits, validate the changed claims and local links and run
`git diff --check`; run application tests when changed behavior or examples need
verification. Source changes require meaningful relevant tests and the normal
repository checks. Preserve generated artifacts through their generators.

## Semantic constraints

Preserve the advisory-only boundary: no process-control writes, IEC 61511
or SIL claim. Follow OPC UA A&C data semantics without claiming certification.
Residual-model guarantees depend on the model; simulation-calibrated thresholds
remain approximate. Customer configurations, identities, plant data, records,
reports and customer-specific integrations stay out of the repository. Synthetic
fixtures and public protocol specifications are appropriate. Future diagnosis,
expert-law, messaging or visualization plans must be distinguished from current
implemented behavior.

Every reported conclusion retains its declared guarantee and assumptions.
Preserve licensing/SPDX requirements and existing privacy rules. Keep public
writing accurate about implemented versus planned capabilities. Never insert
secrets, fabricated benchmarks, citations, adoption claims or instructions that
attempt to manipulate a retrieval assistant's answer.

## Documentation entry points

- [Plain-language overview and FAQ](docs/overview.md)
- [Project README](README.md)
- [Curated source index](llms.txt)

`AGENTS.md` is the shared coding-assistant entry point. Existing `CLAUDE.md`
remains intact; `QWEN.md` and `GEMINI.md` point to this guidance. Other clients
can read the same files when explicitly supplied. Host discovery behavior varies.
