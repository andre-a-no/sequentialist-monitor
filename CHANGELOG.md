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
- The task of a unit (`UnitHandler`): windows on the data's clock, closed by
  the watermark; per feature an AR, exponential smoothing or Kalman model
  and a CUSUM, Shiryaev-Roberts, e-detector or BOCPD on its standardized
  one-step residual; the condition "running"; the quality of each tag's
  signal.
- Alarms after ISA-18.2 with the sub-states of OPC UA Alarms & Conditions:
  acknowledgement, timed and one-shot shelving, suppression by design, out
  of service, an off-delay before return to normal, chattering; every
  transition an audit entry carrying the fields of an A&C event.
- Checks against references: residuals of the true model are white, CUSUM
  against Brook-Evans and the published ARL table, Shiryaev-Roberts and
  e-detectors against their bounds; a detector without a model on
  autocorrelated data breaks its ARL.
- `tools/benchmark.py`: 2000 tags read every second keep up with a 5 s poll
  cycle.
- Calibration on a reference period (`calibrate`): the windows are made as a
  running unit makes them; models fitted by sequential-analysis (`fit_ar`
  with the order by AIC, `fit_ets`) and then treated as known; residuals
  described with warnings; CUSUM and BOCPD thresholds by simulation on
  N(0, 1) residuals, the smallest whose 95% lower bound of the ARL reaches
  the target (approximate). `sequentialist monitor budget`.
- KPIs of alarm management from the audit log (`kpi`): annunciated alarms per
  console per 10 minutes, floods, stale and chattering alarms, the ten most
  frequent sources, priorities, shelving and removal from service with their
  actors and reasons, detector alarms per hour against the budget.
- Sources (`sources`): a directory of CSV files (followed as they grow) and a
  SQL historian through any DB-API 2.0 module (closed slices of time).
  Quality codes of OPC UA, OPC DA or text become Good, Uncertain or Bad.
  Each unit numbers its own readings and gets a cursor record with every
  batch, so a restart reads again from the earliest cursor and every unit
  sees every reading once, under the same number.
- The service (`sequentialist monitor serve`): polls the sources, one batch
  per unit and cycle, moves the watermarks, runs the commands of people in
  its own thread, checkpoints the units in turn, raises the alarm
  "@<source>" when a source cannot be read, reopens a failed task once;
  a new spec makes a new task and people's shelving and removals from
  service carry over to it. Actors are pseudonyms when the plant names a
  key.
- The HTTP API: health, units, a unit's features and alarms, alarms, KPIs,
  and commands with a bearer token (401, 404, 409 as they apply).
- Notifications: log, JSON lines, webhook (the audit entry, its basis the
  fields of an OPC UA A&C event) and e-mail through `smtplib`, each taking
  the transitions that notify, of the priorities it is given.
- Reports in Markdown and JSON with the statements every report carries:
  advisory, not a safety instrumented system; guarantee `model`; parameters
  of the reference period treated as known; time to detect not guaranteed.
- Commands: `calibrate`, `replay`, `serve`, `report`, `command`.
- Examples: `examples/plant.yaml`; `examples/synthetic_plant.py` (a synthetic
  pump end to end, run by CI); `examples/skab.py` (validation on the
  Skoltech Anomaly Benchmark, downloaded when run). The README states the
  guarantees, the checks, the benchmark, the results on SKAB (false alarms
  above the design rate) and the limits of 0.1.
- A Modbus TCP source (`type: modbus`) on the standard library: functions 01-04
  only (it never writes), points placed by the device's register table (DEIF
  controllers among them), types bool, 16- and 32-bit integers and float32,
  word order, scale, offset, bits of a register; reads grouped within the
  protocol's limits. A refused read makes its points Bad; a failed connection
  raises the source's alarm. `sequentialist monitor record` writes a device's
  readings to CSV files for calibration. Checked against the examples of the
  Modbus specification and the server of pymodbus.
- `publish.yml`: a published GitHub release (or a tag given by hand)
  publishes on PyPI by trusted publishing, as sequentialist-core does, after
  checking that the tag is the version, that sequentialist-core is on PyPI,
  and that the tests pass on the built wheel.
