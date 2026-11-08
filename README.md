# sequentialist-monitor

Monitoring of equipment and processes with sequential change detection: shifts
and drift of process variables, the condition of equipment. Built on
[sequentialist-core](https://github.com/andre-a-no/sequentialist-core) (specs,
the checkpointed engine, storage, audit log, reports) and
[sequential-analysis](https://github.com/andre-a-no/sequential-analysis) (all
the statistics).

**An advisory system.** sequentialist-monitor is **not** a safety instrumented
system and makes no claim under IEC 61511 (no SIL). Its alarms advise people;
it does not act on the process.

Version 0.1.0, not released yet (sequentialist-core is not on PyPI either).

## Conceptual framework: the Sequentialist ecosystem

The ecosystem studies sequential decisions: observations arrive, evidence
changes, and a policy chooses whether to continue, act, wait or stop. It separates
three questions within the mathematical and cybernetic methods layer:

| Library | Central question | Responsibility |
|---|---|---|
| [sequential-analysis](https://github.com/andre-a-no/sequential-analysis) | What can we infer from the observations available so far? | Statistical tests, estimation, filtering, prediction and procedure-specific uncertainty guarantees. |
| [sequential-search](https://github.com/andre-a-no/sequential-search) | Which step should we explore next to reach an answer or a goal? | Search and reasoning over alternatives, experiments and rules, with explicit evidence, provenance and resource costs. |
| [sequential-games](https://github.com/andre-a-no/sequential-games) | How do participants pursue their own goals while their actions change a shared environment? | Interaction models for cooperation, competition and partially aligned interests, including asynchronous actions, local information and deliberate waiting. |

These are complementary responsibilities. A participant's policy can use search
to plan and statistical analysis to interpret observations; other participants
continue to act according to their own objectives. This describes how the
methods can be composed. Each package's implementation status is stated
separately below.

The naming convention expresses a further separation into three layers:

| Layer | Packages | Responsibility |
|---|---|---|
| Mathematical and cybernetic methods | `sequential-*`: analysis, search, games | Models, algorithms, explicit state and the assumptions under which a result holds. |
| Reusable engineering platform | `sequentialist-*`, including [core](https://github.com/andre-a-no/sequentialist-core) and [visuals](https://github.com/andre-a-no/sequentialist-visuals) | Durable application execution, storage, recovery, audit and presentation, according to each package's implemented scope. |
| Domain applications | `sequentialist-*`, including [ab](https://github.com/andre-a-no/sequentialist-ab), [quality](https://github.com/andre-a-no/sequentialist-quality), [monitor](https://github.com/andre-a-no/sequentialist-monitor), [clinical](https://github.com/andre-a-no/sequentialist-clinical) and [agentic](https://github.com/andre-a-no/sequentialist-agentic) | Subject-specific goals, workflows, adapters and user-facing decisions built from the methods and engineering components they need. |

Both the engineering platform and applications use the `sequentialist-*`
prefix. The platform is an open family of reusable packages; `core` currently
provides durable tasks built around `sequential-analysis` runs. This organization
does not imply that every combination already has an integration.

Across the ecosystem, state, information available to a decision, randomness,
costs and stopping reasons should be explicit. Evidence, resource expenditure
and real-world effects have separate meanings. Statistical validity, derivability
under supplied rules, properties of an interaction model and reliable execution
each require their own justification; a guarantee in one layer does not
automatically establish a guarantee in another.

### This package's role

**Layer: domain application.** Monitor owns equipment/process data adapters,
windowing and residual pipelines, alarm lifecycle, operator commands and reports.
Statistical models and detectors come from `sequential-analysis`; recovery and
audit come from `sequentialist-core`. Diagnostic search, participant
coordination and reusable visualization would need explicit integrations.
Current alarms advise people; the package does not control the equipment.

## What it does

```
readings ─▶ windows ─▶ model ─▶ residual z ─▶ detector ─▶ alarm (ISA-18.2) ─▶ audit log ─▶ operator, webhook, e-mail
 (every       (mean, max,   (AR, ETS,   (one-step error,  (CUSUM, Shiryaev-     (states of OPC UA     (hash-chained,
 second)      range, min)   Kalman)     standardized)     Roberts, e-detector,  Alarms & Conditions)  replayable)
                                                          BOCPD)
```

* **A task per unit** of equipment (a pump with its tags); every model and
  detector is a run of sequential-analysis inside it, checkpointed, replayed
  after a crash bit for bit.
* **Window → model → residual → detector.** Readings taken every few seconds
  are strongly autocorrelated, and change detectors keep their average run
  length to a false alarm (ARL) only on independent data. A window summarises
  the readings, a model of normal operation forecasts the next window, and the
  detector watches the standardized one-step error, independent N(0, 1) while
  the model holds. The guarantee of the pipeline is therefore **`model`**.
* **A budget of false alarms per console** (ISA-18.2: about 6 alarms per hour
  per operator in normal operation), divided into the ARL of every detector:
  `ARL_i = 3600 D / (B s W_i)` windows for D detectors, B alarms per hour, a
  share s for detectors, windows of W_i seconds.
* **Calibration** on a reference period of normal operation: models fitted by
  sequential-analysis (`fit_ar` with the order by AIC, `fit_ets`), then treated
  as known; residuals described, with warnings; CUSUM and BOCPD thresholds by
  simulation.
* **Alarm management after ISA-18.2 / IEC 62682**: acknowledgement, timed and
  one-shot shelving, suppression by design (a stopped unit), out of service,
  an off-delay before return to normal, chattering; every transition an audit
  entry with the fields of an OPC UA Alarms & Conditions event; KPIs.
* **Sources**: a directory of CSV files, a SQL historian through any DB-API
  2.0 module, a device read over Modbus TCP (DEIF controllers among them; see
  below). **Notifications**: log, JSON lines, webhook, e-mail. **A
  service** with an HTTP API for status and commands; **replay** of history
  through the same code; **reports** in Markdown and JSON.
* Python 3.8 or later, the standard library only (PyYAML for YAML specs).

## Install

```
pip install "sequentialist-core @ git+https://github.com/andre-a-no/sequentialist-core@27f71847ea39cedf0a8e41a5f05042428a533443#subdirectory=packages/core"
pip install "sequentialist-monitor[yaml] @ git+https://github.com/andre-a-no/sequentialist-monitor"
```

## Use

Describe the plant ([examples/plant.yaml](examples/plant.yaml)), then:

```
sequentialist monitor check plant.yaml                 # every problem at once, with its JSON pointer
sequentialist monitor calibrate plant.yaml -o baseline.json
sequentialist monitor budget plant.yaml --baseline baseline.json
sequentialist monitor replay plant.yaml --baseline baseline.json --from 2026-10-01T00:00:00Z --to 2026-10-08T00:00:00Z -o week.md
MONITOR_API_TOKEN=... sequentialist monitor serve plant.yaml --baseline baseline.json
sequentialist monitor report plant.yaml --from ... --to ... --baseline baseline.json
sequentialist monitor command plant.yaml P-101 ack --source TI-101.mean --actor op-17 --reason "seen"
sequentialist audit sqlite:monitor.db plant-1.pumping.P-101.<id>   # verify a unit's audit log
```

The HTTP API (127.0.0.1:8470 by default; TLS on a reverse proxy):

```
GET  /health  /units  /units/{unit}  /alarms?state=&console=&priority=  /kpi?console=&from=&to=
POST /units/{unit}/commands   {"command": "ack", "args": {"source": "TI-101.mean"}, "actor": "op-17", "reason": "seen"}
     (Authorization: Bearer <token>; commands: ack, shelve {duration: seconds | "oneshot"}, unshelve,
      remove_from_service, return_to_service; source: tag.feature, tag.quality, @source, a tag, or * for ack)
```

[examples/synthetic_plant.py](examples/synthetic_plant.py) runs the whole
chain on a synthetic pump: a historian in SQLite, calibration on ten days,
a replay of the eleventh with a step in a temperature and a drift in a
vibration.

## Guarantees

Every report begins with these statements, and so does this file:

1. An advisory system: not a safety instrumented system, no claim under IEC 61511.
2. The guarantee of a detector on a model's residuals is `model`: its ARL to a false alarm holds while the
   model of normal operation holds. Thresholds set by simulation are `approximate`. Statements about false
   alarms are about their long-run mean, not about every hour.
3. Parameters estimated on the reference period are then treated as known; the error of the estimate is not
   part of any guarantee.
4. The time to detect a change is not guaranteed.

| Detector | Threshold | What holds |
|---|---|---|
| Shiryaev–Roberts | A = ARL | ARL ≥ A (Pollak), on independent N(0, 1) residuals |
| e-detector of the mean, of the median | arl = ARL | ARL ≥ arl, on residuals with mean (median) 0 |
| CUSUM | h by simulation | `approximate` (the bound ARL ≥ e^h is not yet declared by sequential-analysis) |
| BOCPD | alarm probability by simulation | `approximate` |

With the model's guarantee, an alarm's guarantee is the weakest: `model`, or
`approximate` where the threshold came from a simulation. Quality and source
alarms are rules on counts: `none`.

## Standards

The data model follows open standards at the boundaries, without
dependencies: the alarm states and event fields of **OPC UA Alarms &
Conditions** (IEC 62541-9: ActiveState, AckedState, ShelvingState,
SuppressedState, OutOfServiceState, Severity 1–1000); quality as **OPC UA
StatusCode** severities (Good, Uncertain, Bad; OPC DA quality bytes and text
are read too); names after **ISA-95** (site, area, unit, tag); the alarm life
cycle and KPIs of **ISA-18.2 / IEC 62682**; units as **UNECE Recommendation
20** codes; times in **ISO 8601** UTC. It *follows* them; conformance to OPC
UA is established only by the OPC Foundation's certification. Of the
protocols, 0.1 reads **Modbus TCP** (functions 01–04, after the MODBUS
Application Protocol Specification V1.1b3); OPC UA and MQTT with Sparkplug B
are not part of 0.1.

## Modbus TCP devices

A source of type `modbus` reads a controller, meter or gateway every
`period`, on the standard library, and never writes to it: the package is
advisory. Each point is a tag of the plant, placed by the register table the
maker of the device publishes; DEIF, for one, documents the Modbus registers
of each controller and software version. Give the address as the request
carries it, counting from 0 (a table that counts from 1 gives one less), and
take the type, scale and word order of 32-bit values from the table:

```yaml
sources:
  controller-1:
    type: modbus
    host: 10.0.0.21            # the device, or a Modbus TCP gateway in front of a serial line
    unit: 1                    # the unit identifier (the slave address behind a gateway)
    period: 1s
    word_order: big            # the high word of a 32-bit value first, unless the table says otherwise
    points:
      P:       {table: input, address: 520, type: int32, scale: 0.001}   # an illustration: use the
      F:       {table: input, address: 507, type: uint16, scale: 0.01}   # addresses of your device's table
      RUNNING: {table: discrete, address: 12}
      ALARM-7: {table: holding, address: 1020, bit: 7}
```

A read the device refuses (exception 02, an address it does not define)
makes its points Bad and leaves the others; a connection that fails, or a
gateway that cannot reach the device, raises the source's alarm until a poll
succeeds. A device keeps no history: readings are stamped with the time of
the poll, and while the service is stopped nothing is read. To calibrate,
record a reference period first and calibrate on it as a CSV source:

```
sequentialist monitor record plant.yaml controller-1 -o recorded/ --for 1209600
```

writes `recorded/controller-1-YYYY-MM-DD.csv` (one file per UTC day) in the
columns a CSV source reads by default, with `quality` as text.

## Checked against references

* The standardized residuals of the true AR(2), ETS(A,N,N) and ETS(A,A,N)
  models are white (mean 0, variance 1, no autocorrelation).
* CUSUM against a Brook–Evans Markov chain computed in the tests, itself
  checked against the published ARL table of the two-sided CUSUM for k = 0.5
  (168 and 8.38 at h = 4; 465 and 10.4 at h = 5); Shiryaev–Roberts and the
  e-detectors keep ARL ≥ their threshold (the e-detectors on normal, t₃ and
  uniform data). Without a model, Shiryaev–Roberts on raw AR(1) data with
  φ = 0.9 alarms far sooner than A.
* Thresholds found by simulation: for an ARL of 360 the exact ARL of the
  CUSUM threshold lies between 0.97 and 1.4 times the target.
* A unit calibrated on 3000 windows raised 2.61 false alarms per hour over
  55.6 hours of normal operation, within its budget of 3.
* KPIs against counts made by hand; the alarm states against every
  transition.
* The Modbus client against the request and response examples of the MODBUS
  Application Protocol Specification V1.1b3 (functions 01–04, exceptions), and
  against the server of [pymodbus](https://github.com/pymodbus-dev/pymodbus)
  (BSD-3-Clause, an independent implementation; CI on Python 3.10 and later).
  It has not yet been run against a DEIF controller.

**On open data**: [examples/skab.py](examples/skab.py) runs the Skoltech
Anomaly Benchmark (SKAB, GPL-3.0, downloaded when run; 34 experiments of a
pump testbed, eight sensors every second, one fault each) with SKAB's
protocol: each experiment is calibrated on its first 400 seconds and the rest
is replayed. In 33 experiments (one skipped: its fault begins in the first
400 s) every fault was followed by an alarm, after 26 s at the median (1 to
56 s). But false alarms came 61 times per hour of normal operation against a
design rate of 16: models fitted on 400 seconds of short, non-stationary
experiments do not hold, and the calibration warned about the residuals of
some sensor in 29 of the 33 experiments. At that rate a chance alarm within
26 s is not rare, so the detections are weak evidence. Guarantee: none.

## Performance

`tools/benchmark.py`: 2000 tags read every second in 40 units of 50, one
feature per tag (AR(1) and a two-sided CUSUM: three runs each), a poll cycle
of 5 s, SQLite storage, each unit checkpointed in turn every 300 s. On one
core of an Intel Xeon at 2.1 GHz, 95% of cycles took 0.52 s on Python 3.13
and 0.72 s on Python 3.8 (at most 0.70 s and 0.95 s).

## Limits of 0.1

* A checkpoint writes a unit's whole state, about a millisecond per run; the
  service checkpoints units in turn so that the cost is spread.
* sequentialist-core 0.1 needs dense sequence numbers per source: each unit
  numbers its readings and keeps a cursor of each source (see `sources`).
* An alarm returns to normal after its restarted detector stays silent for
  `clear_after` windows (30). A persistent shift smaller than the detector's
  design shift leaves a residual that may refire the detector less often than
  that: the alarm then returns and activates again, and each activation is
  announced. Shelve it one-shot or lengthen `clear_after`.
* Readings that reach a historian later than `lateness` are lost; a SQL
  source is read from the service's first start, not before.
* Modbus: TCP only (a serial line through a Modbus TCP gateway), reads only,
  no history; points are read one after another, so a short peak between
  polls is not seen.
* An announcement can be lost if the process dies between a checkpoint and
  its sending; the audit entry is not.
* A new spec makes a new task: shelving and removal from service carry over,
  active alarms do not (their detectors start afresh).
* The Kalman filter's q and r are given by hand; level and trend are better
  fitted as exponential smoothing. BOCPD costs milliseconds a step: at most 20
  per plant.
* Locks hold within one host.

## License

GNU AGPL v3 or a commercial license ([COMMERCIAL.md](COMMERCIAL.md)).
Contributions need the [Contributor License Agreement](CLA.md); see
[CONTRIBUTING.md](CONTRIBUTING.md).

Contact: andrei@hobukob.ru, a.hobukob@gmail.com.

## Overview and AI-assisted reading

For a plain-language explanation and common questions, see the
[overview](docs/overview.md) or [обзор на русском](docs/overview.ru.md). The [curated source index](llms.txt) links to
implementation and examples; [AGENTS.md](AGENTS.md) gives coding-assistant
guidance alongside the preserved owner instructions.
