# Changelog

## sequentialist-monitor 0.1.0 — unreleased

First version, on sequential-analysis 0.4 and sequentialist-core 0.1.

- The package, its CI (Python 3.8 to 3.14, Linux and Windows) and the
  `sequentialist monitor` command.
- Configuration: `monitor.plant/1` (written by a person), `monitor.baseline/1`
  (written by calibration), `monitor.unit/1` (the resolved spec a unit's
  task runs on, its id in the task id `site.area.unit.<spec id>`). Names
  follow ISA-95; secrets are named by environment variables, never given.
  `sequentialist monitor check` and `schema`.
- The plant's budget of false alarms divided into the ARL of every detector
  stream (`budget.allocate`).
