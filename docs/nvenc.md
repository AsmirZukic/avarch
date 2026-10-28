# NVIDIA NVENC backend

AVARCH supports NVIDIA AV1 hardware encoding through FFmpeg's `av1_nvenc` encoder. NVENC is an optional backend: CPU-only installations and existing Av1an/SVT-AV1 profiles continue to work when no NVIDIA GPU is present.

## Workflow

```text
Scan -> Probe -> Plan -> VapourSynth -> vspipe/Y4M -> FFmpeg av1_nvenc
     -> FFmpeg mux -> Validate -> Promote
```

Only the video encoding stage is backend-specific. Discovery, probing, VapourSynth processing, muxing, validation, size policy, receipts, and safe promotion remain shared.

NVENC plans use transcode plan schema version 6. Existing persisted version 5 plans remain Av1an plans and are never reinterpreted as NVENC plans.

## Built-in profile

The built-in `av1_nvenc_1080p_sdr` profile uses conservative defaults:

```toml
backend = "ffmpeg_nvenc"
container = "mkv"

[video]
max_width = 1920
hdr_to_sdr = true
source = "vapoursynth"

[nvenc]
encoder = "av1_nvenc"
preset = "p5"
tune = "hq"
cq = 28
multipass = "qres"
lookahead = 16
spatial_aq = true
temporal_aq = true
aq_strength = 8
gpu = 0
pixel_format = "p010le"
```

NVENC CQ and SVT-AV1 CRF are different rate-control scales and must not be compared as equivalent quality values. Profile options are validated fields, not arbitrary FFmpeg arguments.

Copy and customize the profile with:

```bash
avarch profiles copy av1_nvenc_1080p_sdr --name my_nvenc
```

## Capability checks

`avarch doctor` reports NVENC as an optional capability. A missing GPU does not make the overall doctor command fail.

Before an NVENC encode, AVARCH verifies:

- `nvidia-smi` can access an NVIDIA GPU;
- the selected GPU index exists;
- FFmpeg lists `av1_nvenc`;
- `p010le` is advertised by that encoder;
- a real one-frame AV1 NVENC initialization succeeds.

There is no software fallback. Driver/API mismatches, unsupported GPUs, inaccessible runtime libraries, and encoder initialization failures stop only the NVENC job with a clear preflight error.

## Docker and NVIDIA Container Toolkit

The image contains FFmpeg with the `av1_nvenc` frontend but does not contain NVIDIA kernel drivers. Install the NVIDIA Container Toolkit on the host and start AVARCH with GPU devices exposed.

With the AVARCH wrapper:

```bash
AVARCH_DOCKER_GPUS=all avarch doctor
AVARCH_DOCKER_GPUS=all avarch workflow run \
  --profile av1_nvenc_1080p_sdr --mode keep-original /media/movie.mkv
```

Direct Docker example:

```bash
docker run --rm --gpus all \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
  -v "$PWD:/workspace" -w /workspace \
  avarch:latest doctor
```

CPU-only commands do not require `--gpus` or `AVARCH_DOCKER_GPUS`.

## Scheduling

The default resource limits are independent:

```toml
[resources]
cheap_workers = 4
av1an_jobs = 1
nvenc_jobs = 1
file_ops = 1
```

One NVENC encode runs at a time by default. Av1an and NVENC jobs may run concurrently. This release uses one configured NVENC capacity pool; distributed scheduling and per-GPU load balancing are not implemented.

## Progress and recovery

FFmpeg is started with `-progress pipe:1`. AVARCH parses partial machine-readable records and persists encoded duration, FPS, speed, bitrate, and output growth through the existing progress system.

An interrupted NVENC video encode restarts from the beginning. A completed intermediate is reusable only when its backend-specific marker matches the plan hash, NVENC specification hash, output path, and output size. Invalid or unmarked intermediates are rejected. Final output still requires normal muxing and media validation before promotion.

## Hardware smoke test

Run these checks only on a supported NVIDIA GPU and record the exact GPU, driver, FFmpeg, and AVARCH versions:

1. `AVARCH_DOCKER_GPUS=all avarch doctor` reports NVENC available.
2. Encode a short 1080p SDR source with `av1_nvenc_1080p_sdr`.
3. Confirm `ffprobe` reports AV1, `yuv420p10le`, BT.709 primaries/transfer/matrix, and TV range.
4. Encode a source containing selected audio and subtitles; confirm muxed streams, chapters, and metadata.
5. Cancel an active encode and verify both `vspipe` and FFmpeg exit and no unmarked intermediate is reused.
6. Restart the job and verify it begins encoding from the start.
7. Run with GPU access removed and verify a clear NVENC capability failure while an Av1an job still runs.
8. Confirm final validation succeeds before promotion and the original remains untouched until promotion completes.

Hardware tests are optional and must not be reported as passed unless run on real supported hardware.

## Known limitations

- AV1 (`av1_nvenc`) is the only NVENC codec implemented. H.264 and HEVC are not selectable.
- NVENC encoding restarts rather than resuming chunks.
- `nvenc_jobs` is a single local capacity pool, not per-GPU load balancing.
- VapourSynth processing remains CPU-based and frame transfer is not zero-copy.
- Actual quality/performance depends on GPU generation, driver, FFmpeg build, source, and settings.
