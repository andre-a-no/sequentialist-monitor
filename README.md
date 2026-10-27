# sequentialist-monitor

**Not released yet: this repository holds the plan of version 0.1; the code
is being written.**

Monitoring of equipment, processes and energy with sequential change
detection: drift and shifts of process variables, the condition of
equipment, energy use. Built on
[sequentialist-core](https://github.com/andre-a-no/sequentialist-core) (specs,
the checkpointed engine, storage, audit log, reports) and
[sequential-analysis](https://github.com/andre-a-no/sequential-analysis) (the
statistics). An advisory system: it is **not** a safety instrumented system
and makes no claim under IEC 61511.

## Planned for 0.1

- **A task per unit of equipment** (a pump with its 10–50 tags), each tag
  and detector a stream of the task.
- **Window → model → residual → detector.** Raw values of sensors read
  every few seconds are strongly autocorrelated, and the guarantees of
  change detectors (average run length to a false alarm) do not hold on
  them. Values are first summarised in windows, a model (autoregression,
  Kalman filter) forecasts one step ahead, and the detector watches the
  residuals. The guarantee of such a pipeline is `model`: it holds if the
  model does, and reports say so.
- **Detectors** of sequential-analysis: CUSUM, Shiryaev–Roberts,
  e-detectors of the mean and of a quantile; level and trend by Kalman
  filtering; Bayesian change points for a few tags.
- **A budget of false alarms for the plant**, in the spirit of ISA-18.2,
  divided among the detectors; parameters of normal operation estimated on
  a reference period and then treated as known, which reports state.
- **Alarm management** after ISA-18.2 / IEC 62682: states, priorities,
  acknowledgement, shelving with expiry, out of service, chattering
  suppression, each action with its actor and reason in the audit log;
  alarm KPIs.
- **Sources**: CSV files (following a directory), SQL historians (polling
  through DB-API). **Notifications**: log, webhook, e-mail. An HTTP API for
  status and acknowledgement; replay of history for tuning.
- Python 3.8 or later and the standard library only.

Later (0.2): MQTT and OPC UA as optional extras, writing alarms back to
SCADA, hidden regimes (HMM), energy forecasts with seasonality and conformal
intervals.

## License

GNU AGPL v3 or a commercial license ([COMMERCIAL.md](COMMERCIAL.md)).
Contributions need the [Contributor License Agreement](CLA.md); see
[CONTRIBUTING.md](CONTRIBUTING.md).

Contact: andrei@hobukob.ru, a.hobukob@gmail.com.
