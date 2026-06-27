from __future__ import annotations

import os
import tempfile
from pathlib import Path

from avarch.models.validation import ValidationReport
from avarch.serialization import canonical_json


def write_validation_report(path: Path, report: ValidationReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
        suffix=".json",
        text=True,
    )
    temporary_path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output_file:
            output_file.write(canonical_json(report))
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return path
