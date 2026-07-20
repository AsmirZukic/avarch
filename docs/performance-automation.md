# Performance Automation

Avarch treats `workers = "auto"` as a product policy. For a first or low-confidence
encode, Avarch delegates to Av1an native automatic workers and records that fallback.
When compatible history is available, Avarch may choose explicit `workers` and SVT
`--lp` values for the attempt. The profile's output-quality settings are not changed.

Manual overrides remain available for expert profiles:

```toml
[av1an]
workers = "auto"
svt_lp = "native"
video_args = "--preset 6 --crf 28"

[resources]
cpu_reserve = 1.0
memory_reserve = "10%"
reservation_allocator = true

[performance]
calibration_enabled = true
calibration_max_seconds = 120.0
calibration_max_predicted_fraction = 0.01
calibration_min_predicted_seconds = 300.0
calibration_sample_seconds = 10.0
calibration_warmup_seconds = 1.0
calibration_max_candidates = 8
calibration_min_gain_fraction = 0.03
```

Automatic calibration first measures a bounded sweep of safe worker/`lp`
topologies on identical source frames. Candidate ranking uses post-warmup encode
throughput, excluding one-time source indexing and process startup. Unsafe or slow
candidates are eliminated, and remaining time is spent remeasuring close finalists
in reversed order. High-confidence selection requires repeated winner evidence;
uncertain gains retain the native baseline.

A single raw `--lp` in `video_args` is still accepted for compatibility, but new
profiles should prefer structured `svt_lp`. Duplicate raw `--lp` values, or structured
`svt_lp` combined with raw `--lp`, fail validation with migration guidance.

Use `avarch system resources` to inspect the detected CPU and memory envelope. Use
`avarch jobs inspect <id>` to see the planned intent, resolved attempt decision,
confidence, evidence count, fallback status and completed performance metrics.
