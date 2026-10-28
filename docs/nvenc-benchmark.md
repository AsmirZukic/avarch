# AV1 NVENC versus SVT-AV1 benchmark procedure

This procedure compares AVARCH backends without treating NVENC CQ and SVT-AV1 CRF as equivalent quality settings.

## Requirements

- A supported NVIDIA AV1 NVENC GPU and compatible driver.
- FFmpeg built with `av1_nvenc`; FFmpeg with `libvmaf` is recommended.
- `/usr/bin/time` and `nvidia-smi`.
- One source clip copied into two otherwise identical AVARCH workspaces.

Record:

```bash
avarch --version
ffmpeg -version | head -n 3
nvidia-smi --query-gpu=name,driver_version --format=csv
```

## Run both backends

Use identical source bytes, selected streams, VapourSynth settings, and output dimensions. Do not run the encodes concurrently.

```bash
# Workspace A
/usr/bin/time -v -o svt.time.txt \
  avarch workflow run --profile av1_1080p_sdr \
  --mode keep-original source.mkv

# Workspace B; expose the GPU when Docker is used
nvidia-smi dmon -s pucvmet -d 1 -o DT > nvenc.gpu.txt &
MONITOR_PID=$!
/usr/bin/time -v -o nvenc.time.txt \
  avarch workflow run --profile av1_nvenc_1080p_sdr \
  --mode keep-original source.mkv
kill "$MONITOR_PID"
```

Capture from AVARCH logs and the files above:

- encoded FPS and speed;
- wall-clock processing time;
- maximum resident memory and CPU utilization;
- output byte size;
- GPU utilization, encoder utilization, and memory usage.

## Quality measurement

Compare each output against the same preprocessed reference. If `libvmaf` is available, use a consistent scaling and pixel format for all comparisons:

```bash
ffmpeg -i encoded.mkv -i reference.mkv \
  -lavfi '[0:v]setpts=PTS-STARTPTS[dist];[1:v]setpts=PTS-STARTPTS[ref];[dist][ref]libvmaf=log_fmt=json:log_path=vmaf.json' \
  -f null -
```

If the encoded and reference dimensions differ, explicitly scale both to the final planned dimensions before `libvmaf`. Record the FFmpeg command and VMAF model so results are reproducible.

Test multiple CQ/CRF values and compare quality at similar output sizes or output size at similar VMAF. Do not infer an efficiency result from a single CQ-versus-CRF pair.

## Result table

| Backend/profile | Settings | FPS | Wall time | Output bytes | CPU % | Max RSS | GPU enc % | GPU memory | VMAF |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SVT-AV1 | preset/CRF | | | | | | n/a | n/a | |
| AV1 NVENC | preset/CQ | | | | | | | | |

No benchmark result is included in the repository because valid NVENC measurements require real compatible hardware.
