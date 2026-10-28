# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The configuration of a plant: what is watched, where the data come from, where alarms go.

Two files describe a plant.  ``plant.yaml`` (format ``monitor.plant/1``)
is written by a person.  ``baseline.json`` (``monitor.baseline/1``) is
written by ``sequentialist monitor calibrate`` from a reference period of
history.  The spec of one unit (``monitor.unit/1``) is built from both
(`build_unit_spec`) and stored in its task: its id fixes the baseline a
detector runs on.

Names follow ISA-95 (IEC 62264): site → area → unit → tag.  Ids are ASCII
(letters, digits, ``_`` and ``-``) because the ids of tasks are made of
them; display names in any language go in ``name``.  Units of measure are
UNECE Recommendation 20 codes, as OPC UA ``EUInformation`` uses them.
Secrets (connection strings, tokens, passwords) are never part of a spec:
the spec names an environment variable that holds them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from sequentialist.core import Spec, load_spec
from sequentialist.core.schema import (
    Array,
    Boolean,
    Duration,
    Field,
    Map,
    Nullable,
    Number,
    Object,
    OneOf,
    String,
    Timestamp,
)

ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"
ENV = r"[A-Za-z_][A-Za-z0-9_]*"
FEATURES = ("mean", "range", "max", "min")
PRIORITIES = ("urgent", "high", "medium", "low")
DIRECTIONS = ("up", "down", "both")
QUALITY_CODES = ("opc-ua", "opc-da", "text", "none")
MAX_BOCPD = 20  # Bayesian change points cost milliseconds a step: a few tags only


def _id(doc: str = "") -> String:
    return String(pattern=ID, doc=doc)


# ---------------------------------------------------------------------------------------------- models

MODELS = {
    "ar": Object({
        "order": Field(Nullable(Number(integer=True, minimum=1, maximum=5)), default=None,
                       doc="AR order; null chooses 1..5 by AIC at calibration."),
    }, doc="Autoregression with coefficients fitted on the reference period (fit_ar), then fixed."),
    "level": Object({}, doc="Exponential smoothing without trend, ETS(A,N,N): the innovations form of a local "
                            "level model, fitted by fit_ets."),
    "trend": Object({
        "damped": Field(Boolean(), default=False),
    }, doc="Exponential smoothing with an additive (or damped) trend, ETS(A,A,N), fitted by fit_ets."),
    "kalman_level": Object({
        "q": Field(Number(minimum=0), doc="Variance of the level's random walk per window."),
        "r": Field(Number(exclusive_minimum=0), doc="Variance of the measurement noise."),
    }, doc="Local level Kalman filter with q and r given by hand (sequential-analysis cannot fit them yet)."),
    "none": Object({}, doc="No model: the detector watches the standardized window feature itself. Its false "
                           "alarm guarantee does not hold on autocorrelated data; needs guarantee: none."),
}

DETECTORS = {
    "cusum": Object({
        "shift": Field(Number(exclusive_minimum=0), default=1.0,
                       doc="The shift worth detecting, in standard deviations of the residual."),
        "direction": Field(String(enum=DIRECTIONS), default="both"),
    }, doc="Page's CUSUM on the standardized residual (Gaussian family); its threshold is set by simulation."),
    "sr": Object({
        "shift": Field(Number(exclusive_minimum=0), default=1.0),
        "direction": Field(String(enum=DIRECTIONS), default="both"),
    }, doc="Shiryaev-Roberts on the standardized residual; ARL >= A as sequential-analysis states it."),
    "mean_e": Object({
        "clip": Field(Number(exclusive_minimum=0), default=4.0,
                      doc="The residual is clipped to [-clip, clip]; its mean is 0 before a change if the "
                          "residuals are symmetric."),
        "direction": Field(String(enum=DIRECTIONS), default="both"),
        "statistic": Field(String(enum=["shiryaev-roberts", "cusum"]), default="shiryaev-roberts"),
    }, doc="e-detector of the mean of the clipped residual; ARL >= arl as sequential-analysis states it."),
    "quantile_e": Object({
        "direction": Field(String(enum=DIRECTIONS), default="both"),
        "statistic": Field(String(enum=["shiryaev-roberts", "cusum"]), default="shiryaev-roberts"),
    }, doc="e-detector of the median of the residual (0 before a change); ARL >= arl."),
    "bocpd": Object({
        "hazard": Field(Number(exclusive_minimum=0, exclusive_maximum=1), default=0.001),
        "alarm_probability": Field(Number(exclusive_minimum=0, exclusive_maximum=1), default=0.9),
        "max_hypotheses": Field(Number(integer=True, minimum=2, maximum=2000), default=200),
    }, doc="Bayesian online change points (normal data, unknown mean and variance); no ARL guarantee, its "
           "false alarm rate is estimated by simulation."),
}

FEATURE = Object({
    "model": Field(OneOf("type", MODELS)),
    "detector": Field(OneOf("type", DETECTORS)),
    "priority": Field(String(enum=PRIORITIES), default="medium"),
    "arl": Field(Nullable(Number(minimum=2)), default=None,
                 doc="Average run length to a false alarm, in windows; null takes it from the plant's budget."),
    "guarantee": Field(Nullable(String(enum=["none"])), default=None,
                       doc="Set to none to accept a detector without a model (model: {type: none})."),
})

TAG = Object({
    "name": Field(Nullable(String()), default=None, doc="Display name, any language."),
    "source_tag": Field(Nullable(String(min_length=1)), default=None,
                        doc="The tag's name in the source if it differs from its id."),
    "unit_of_measure": Field(Nullable(String(pattern=r"[A-Z0-9]{2,3}")), default=None,
                             doc="UNECE Recommendation 20 code, as in OPC UA EUInformation (CEL, MMS, BAR, ...)."),
    "range": Field(Nullable(Array(Number(), min_items=2, max_items=2)), default=None,
                   doc="Physical range of the sensor; values outside are Bad."),
    "features": Field(Map(FEATURE, key_pattern="|".join(FEATURES)), default=dict,
                      doc="Window features watched: mean, range, max, min."),
})

UNIT = Object({
    "area": Field(_id("ISA-95 area.")),
    "name": Field(Nullable(String()), default=None),
    "source": Field(_id("The source the unit's tags are read from.")),
    "console": Field(_id("The operator console that receives the unit's alarms."), default="main"),
    "window": Field(Nullable(Duration()), default=None, doc="Window length; null takes the plant's."),
    "running": Field(Nullable(Object({
        "tag": Field(_id()),
        "above": Field(Number()),
    })), default=None, doc="The unit runs while this tag is above the value; when it stops, its windows are not "
                           "fed to models and its alarms are suppressed by design."),
    "reference": Field(Nullable(Object({
        "from": Field(Timestamp()),
        "to": Field(Timestamp()),
    })), default=None, doc="Reference period of normal operation for calibration."),
    "tags": Field(Map(TAG, key_pattern=ID)),
})

COLUMNS = Object({
    "tag": Field(String(min_length=1), default="tag"),
    "time": Field(String(min_length=1), default="time"),
    "value": Field(String(min_length=1), default="value"),
    "quality": Field(Nullable(String(min_length=1)), default=None),
})

SOURCES = {
    "csv": Object({
        "directory": Field(String(min_length=1)),
        "pattern": Field(String(min_length=1), default="*.csv"),
        "columns": Field(COLUMNS, default=dict),
        "delimiter": Field(String(min_length=1, max_length=1), default=","),
        "quality_codes": Field(String(enum=QUALITY_CODES), default="text"),
    }, doc="A directory of CSV files that are only appended to; a new file is a new source."),
    "sql": Object({
        "module": Field(String(pattern=r"[A-Za-z_][A-Za-z0-9_.]*"), doc="A DB-API 2.0 module, e.g. pyodbc."),
        "dsn_env": Field(String(pattern=ENV), doc="Environment variable with the connection string."),
        "query": Field(String(min_length=1), doc="Returns tag, time, value[, quality] for times in [?, ?), "
                                                 "ordered by time then tag; the module's paramstyle is used."),
        "paramstyle": Field(Nullable(String(enum=["qmark", "format", "named", "pyformat", "numeric"])),
                            default=None, doc="Overrides the module's paramstyle."),
        "period": Field(Duration(), default=5),
        "quality_codes": Field(String(enum=QUALITY_CODES), default="opc-da"),
    }, doc="A historian polled through DB-API in closed slices of time."),
}

NOTIFY = OneOf("type", {
    "log": Object({"kinds": Field(Array(String(), min_items=1), default=lambda: ["decision", "action"])}),
    "jsonl": Object({"path": Field(String(min_length=1))}),
    "webhook": Object({
        "url_env": Field(String(pattern=ENV)),
        "priorities": Field(Array(String(enum=PRIORITIES), min_items=1), default=lambda: list(PRIORITIES)),
        "timeout": Field(Duration(), default=5),
    }),
    "email": Object({
        "smtp": Field(String(pattern=r"[A-Za-z0-9.-]+(:\d{1,5})?"), doc="host or host:port"),
        "starttls": Field(Boolean(), default=False),
        "user_env": Field(Nullable(String(pattern=ENV)), default=None),
        "password_env": Field(Nullable(String(pattern=ENV)), default=None),
        "sender": Field(String(min_length=3)),
        "to": Field(Array(String(min_length=3), min_items=1)),
        "priorities": Field(Array(String(enum=PRIORITIES), min_items=1), default=lambda: ["urgent", "high"]),
    }),
})

ALARMS = Object({
    "clear_after": Field(Number(integer=True, minimum=1), default=10,
                         doc="Windows of silence of a restarted detector before its alarm returns to normal."),
    "warmup": Field(Number(integer=True, minimum=0), default=10,
                    doc="Windows a model takes after a start before its residuals reach a detector."),
    "rearm": Field(Duration(), default=600, doc="Quiet time after which a chattering source notifies again."),
    "chatter_count": Field(Number(integer=True, minimum=2), default=3),
    "chatter_window": Field(Duration(), default=60),
    "max_shelve": Field(Duration(), default=86400),
    "stale_after": Field(Duration(), default=86400, doc="An alarm active longer than this is stale (KPI)."),
    "flood": Field(Number(integer=True, minimum=1), default=10, doc="Alarms per console in 10 minutes that make "
                                                                     "a flood (KPI)."),
    "min_good": Field(Number(exclusive_minimum=0, maximum=1), default=0.5,
                      doc="Share of Good values a window needs; others are skipped."),
    "bad_windows": Field(Number(integer=True, minimum=1), default=5,
                         doc="Skipped windows in a row that raise a quality alarm."),
    "uncertain_is_bad": Field(Boolean(), default=True),
})

SEVERITY = Object({p: Field(Number(integer=True, minimum=1, maximum=1000), default=d)
                   for p, d in zip(PRIORITIES, (900, 700, 500, 300))},
                  doc="OPC UA Severity (1..1000) of each priority.")


class PlantSpec(Spec):
    kind = "monitor.plant/1"
    schema = Object({
        "site": Field(_id("ISA-95 site.")),
        "name": Field(Nullable(String()), default=None),
        "storage": Field(String(pattern=r"(files|sqlite|memory):.*"), doc="files:<dir>, sqlite:<file> or memory:"),
        "window": Field(Duration(), default=60),
        "lateness": Field(Duration(allow_zero=True), default=30,
                          doc="How late a record may arrive; a window closes this long after its end."),
        "budget": Field(Object({
            "alarms_per_hour": Field(Number(exclusive_minimum=0), default=6,
                                     doc="Alarms per hour per console in normal operation (ISA-18.2: about 6)."),
            "detector_share": Field(Number(exclusive_minimum=0, maximum=1), default=0.5,
                                    doc="The share of the budget given to statistical detectors."),
        }), default=dict),
        "severity": Field(SEVERITY, default=dict),
        "alarms": Field(ALARMS, default=dict),
        "consoles": Field(Map(Object({"name": Field(Nullable(String()), default=None)}), key_pattern=ID),
                          default=lambda: {"main": {}}),
        "sources": Field(Map(OneOf("type", SOURCES), key_pattern=ID)),
        "notify": Field(Array(NOTIFY), default=list),
        "http": Field(Object({
            "host": Field(String(min_length=1), default="127.0.0.1"),
            "port": Field(Number(integer=True, minimum=0, maximum=65535), default=8470),
            "token_env": Field(String(pattern=ENV), default="MONITOR_API_TOKEN"),
        }), default=dict),
        "actor_key_env": Field(Nullable(String(pattern=ENV)), default=None,
                               doc="With a key, actors of commands are stored as pseudonyms (HMAC-SHA-256)."),
        "units": Field(Map(UNIT, key_pattern=ID)),
    }, doc="A plant monitored by sequentialist-monitor.")

    def problems(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        bocpd = 0
        for uid, unit in self.data["units"].items():
            where = f"/units/{uid}"
            if unit["source"] not in self.data["sources"]:
                out.append((f"{where}/source", f"no source {unit['source']!r}; known: "
                                                f"{sorted(self.data['sources'])}"))
            if unit["console"] not in self.data["consoles"]:
                out.append((f"{where}/console", f"no console {unit['console']!r}; known: "
                                                 f"{sorted(self.data['consoles'])}"))
            running = unit["running"]
            if running is not None and running["tag"] not in unit["tags"]:
                out.append((f"{where}/running/tag", f"{running['tag']!r} is not a tag of the unit"))
            reference = unit["reference"]
            if reference is not None and reference["from"] >= reference["to"]:
                out.append((f"{where}/reference", "from must be before to"))
            names: dict[str, str] = {}
            for tid, tag in unit["tags"].items():
                name = tag["source_tag"] or tid
                if name in names:
                    out.append((f"{where}/tags/{tid}", f"reads source tag {name!r} as {names[name]!r} does"))
                names[name] = tid
                if tag["range"] is not None and tag["range"][0] >= tag["range"][1]:
                    out.append((f"{where}/tags/{tid}/range", "the low end must be below the high end"))
                for fname, feature in tag["features"].items():
                    at = f"{where}/tags/{tid}/features/{fname}"
                    if feature["model"]["type"] == "none" and feature["guarantee"] != "none":
                        out.append((f"{at}/guarantee", "a detector without a model has no false alarm guarantee "
                                                       "on autocorrelated data; set guarantee: none to accept it"))
                    if feature["model"]["type"] != "none" and feature["guarantee"] is not None:
                        out.append((f"{at}/guarantee", "only a detector without a model takes guarantee: none"))
                    if feature["detector"]["type"] == "bocpd":
                        bocpd += 1
        if bocpd > MAX_BOCPD:
            out.append(("/units", f"{bocpd} BOCPD detectors; at most {MAX_BOCPD} per plant (each step costs "
                                  f"milliseconds)"))
        return out

    def unit_window(self, uid: str) -> float:
        window = self.data["units"][uid]["window"]
        return float(window if window is not None else self.data["window"])


def load_plant(source: str | Path | Mapping[str, Any]) -> PlantSpec:
    """The plant from a file (.yaml, .yml, .json) or a parsed mapping; SpecError lists every problem."""
    return load_spec(source, PlantSpec)


# ---------------------------------------------------------------------------------------------- baseline

FEATURE_KEY = rf"{ID}\.({'|'.join(FEATURES)})"
STREAM_KEY = "up|down|detector"
POSITIVE = Number(exclusive_minimum=0)
UNIT_INTERVAL = Number(exclusive_minimum=0, maximum=1)

# a model with its parameters fixed by calibration (or by hand)
FITTED_MODELS = {
    "ar": Object({
        "intercept": Field(Number()),
        "coefficients": Field(Array(Number(), min_items=1, max_items=5)),
        "sigma": Field(POSITIVE),
    }),
    "level": Object({"alpha": Field(UNIT_INTERVAL), "sigma": Field(POSITIVE)}),
    "trend": Object({
        "alpha": Field(UNIT_INTERVAL),
        "beta": Field(Number(minimum=0, maximum=1)),
        "phi": Field(UNIT_INTERVAL),
        "sigma": Field(POSITIVE),
    }),
    "kalman_level": Object({"q": Field(Number(minimum=0)), "r": Field(POSITIVE)}),
    "none": Object({"mean": Field(Number()), "sd": Field(POSITIVE)}),
}

THRESHOLD = Object({
    "arl": Field(Number(minimum=2), doc="The ARL the threshold was set for, in windows."),
    "threshold": Field(POSITIVE),
    "runs": Field(Number(integer=True, minimum=1)),
    "seed": Field(Number(integer=True, minimum=0)),
    "arl_interval": Field(Array(Number(), min_items=2, max_items=2),
                          doc="Monte Carlo interval of the ARL at this threshold (95%)."),
}, doc="A threshold set by simulation on the model of normal operation (guarantee approximate).")


class BaselineSpec(Spec):
    kind = "monitor.baseline/1"
    schema = Object({
        "site": Field(_id()),
        "plant_spec": Field(String(pattern=r"[0-9a-f]{64}"), doc="Id of the plant spec it was calibrated for."),
        "created": Field(Timestamp()),
        "units": Field(Map(Object({
            "reference": Field(Object({"from": Field(Timestamp()), "to": Field(Timestamp())})),
            "windows": Field(Number(integer=True, minimum=0), doc="Windows of normal operation used."),
            "features": Field(Map(Object({
                "model": Field(OneOf("type", FITTED_MODELS)),
                "residuals": Field(Object({
                    "n": Field(Number(integer=True, minimum=0)),
                    "mean": Field(Number()),
                    "sd": Field(Number(minimum=0)),
                    "median": Field(Number()),
                    "acf": Field(Array(Number()), doc="Autocorrelations at lags 1..10 (descriptive)."),
                }), doc="Standardized residuals on the reference period; descriptive, guarantee none."),
                "thresholds": Field(Map(THRESHOLD, key_pattern=STREAM_KEY), default=dict),
                "warnings": Field(Array(String()), default=list),
            }), key_pattern=FEATURE_KEY)),
        }), key_pattern=ID)),
    }, doc="Models and thresholds fitted on reference periods of normal operation.")


def load_baseline(source: str | Path | Mapping[str, Any]) -> BaselineSpec:
    return load_spec(source, BaselineSpec)


# ---------------------------------------------------------------------------------------------- unit

DETECTOR_PARAMS = {
    "cusum": Object({"shift": Field(POSITIVE)}),
    "sr": Object({"shift": Field(POSITIVE)}),
    "mean_e": Object({"clip": Field(POSITIVE), "direction": Field(String(enum=DIRECTIONS)),
                      "statistic": Field(String(enum=["shiryaev-roberts", "cusum"]))}),
    "quantile_e": Object({"direction": Field(String(enum=DIRECTIONS)),
                          "statistic": Field(String(enum=["shiryaev-roberts", "cusum"]))}),
    "bocpd": Object({"hazard": Field(Number(exclusive_minimum=0, exclusive_maximum=1)),
                     "max_hypotheses": Field(Number(integer=True, minimum=2))}),
}

STREAM = Object({
    "arl": Field(Number(minimum=2), doc="Target average run length to a false alarm, in windows."),
    "arl_from": Field(String(enum=["budget", "given"])),
    "threshold": Field(POSITIVE, doc="The procedure's threshold: h of CUSUM, A of Shiryaev-Roberts, arl of an "
                                     "e-detector, the alarm probability of BOCPD."),
    "threshold_from": Field(String(enum=["bound", "simulation"]),
                            doc="bound: ARL >= threshold as sequential-analysis states it; simulation: set on the "
                                "model of normal operation (approximate)."),
})


class UnitSpec(Spec):
    """One unit as its task runs it: every parameter resolved, nothing left to the plant or the baseline."""

    kind = "monitor.unit/1"
    schema = Object({
        "site": Field(_id()),
        "area": Field(_id()),
        "unit": Field(_id()),
        "name": Field(Nullable(String()), default=None),
        "console": Field(_id()),
        "window": Field(Duration()),
        "lateness": Field(Duration(allow_zero=True)),
        "running": Field(Nullable(Object({"tag": Field(_id()), "above": Field(Number())})), default=None),
        "reference": Field(Nullable(Object({"from": Field(Timestamp()), "to": Field(Timestamp())})), default=None),
        "baseline": Field(Nullable(String(pattern=r"[0-9a-f]{64}")), default=None),
        "alarms": Field(ALARMS),
        "severity": Field(SEVERITY),
        "tags": Field(Map(Object({
            "name": Field(Nullable(String()), default=None),
            "source_tag": Field(String(min_length=1)),
            "unit_of_measure": Field(Nullable(String()), default=None),
            "range": Field(Nullable(Array(Number(), min_items=2, max_items=2)), default=None),
            "features": Field(Map(Object({
                "priority": Field(String(enum=PRIORITIES)),
                "guarantee": Field(String(enum=["model", "none"])),
                "model": Field(OneOf("type", FITTED_MODELS)),
                "detector": Field(OneOf("type", DETECTOR_PARAMS)),
                "streams": Field(Map(STREAM, key_pattern=STREAM_KEY)),
            }), key_pattern="|".join(FEATURES)), default=dict),
        }), key_pattern=ID)),
    })

    def task_id(self) -> str:
        """``site.area.unit.<12 characters of the spec id>``: a new spec makes a new task."""
        d = self.data
        return f"{d['site']}.{d['area']}.{d['unit']}.{self.id[:12]}"
