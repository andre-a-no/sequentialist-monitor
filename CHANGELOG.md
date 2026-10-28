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
