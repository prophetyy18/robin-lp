# P10 — Research universe, datasets, and the model laboratory

**Purpose:** turn the single-pool replay and backtest machinery into a reproducible
multi-pool research environment: versioned datasets with an explicit reporting
numeraire, point-in-time panels and labels, trained models that occupy the strategy
layer's replaceable interfaces, and a console that makes the whole path inspectable.

**Entry:** Phase 6 exit gate, Phase 8 authenticated console, and at least two pools at
`backtest` support level carrying datasets acquired by the per-pool extended-history
window. Research may span many pools; execution remains one active `PoolKey`.

**Exit gate:** the owner can see what data exists, choose pools and periods from that
evidence, build train, validation and test datasets with pool and time holdout, train
and inspect a model, and read a walk-forward evaluation whose verdict is produced by the
event-driven backtest engine — including when that verdict rejects the model. Every
dataset, feature configuration, split and model artifact is versioned and reproducible
from a saved definition.

**Phase prohibitions:** no random or shuffled split, no training or imputation on the
evaluation period, no look-ahead feature, no floating-point value inside the protocol,
replay, accounting or valuation integer paths, no model that emits a position or
bypasses the central risk gateway, and no research artifact that grants execution
authority. `RELATIVE_ONLY` results are never presented as USD results, and a favorable
prediction metric is never reported as strategy performance.

## Tasks

- [T100 — Build the research dataset registry and numeraire qualification](T100.md)
- [T101 — Build the panel labels and the training/evaluation harness](T101.md)
- [T102 — Evaluate trained models as strategy components through the event-driven engine](T102.md)
- [T103 — Build the research console](T103.md)
