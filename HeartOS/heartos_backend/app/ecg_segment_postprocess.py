#!/usr/bin/env python3
"""ECG segmentation postprocessing shared by the HeartOS proxy.

This script directly calls the ECG segmentation model endpoint. It does not
depend on HeartOS backend, HeartOS frontend, or any HeartOS proxy route.

Default endpoint:
    http://219.147.100.43:18018/api/ecg-segment

It sends one ECG lead crop to the model endpoint, saves the raw model response,
reconstructs common intermediate paths, applies a conservative postprocess, and
writes a self-contained HTML report.

The public integration entry point is :func:`postprocess_segment_response`.
The CLI at the bottom is intentionally retained so the production path and
the standalone debug harness execute the same algorithm.
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import math
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def parse_crop(text: str | None) -> tuple[int, int, int, int] | None:
    if not text:
        return None
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError("--crop must be x1,y1,x2,y2")
    x1, y1, x2, y2 = [int(round(float(p))) for p in parts]
    if x2 <= x1 or y2 <= y1:
        raise ValueError("--crop requires x2>x1 and y2>y1")
    return x1, y1, x2, y2


def load_crop(path: Path, crop: tuple[int, int, int, int] | None) -> Image.Image:
    img = Image.open(path).convert("RGB")
    if crop:
        w, h = img.size
        x1, y1, x2, y2 = crop
        x1 = max(0, min(w - 1, x1))
        y1 = max(0, min(h - 1, y1))
        x2 = max(x1 + 1, min(w, x2))
        y2 = max(y1 + 1, min(h, y2))
        img = img.crop((x1, y1, x2, y2))
    return img


def encode_multipart(field_name: str, filename: str, content: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = "----heartos-ecg-debug-%d" % int(time.time() * 1000)
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        (
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    body.write(content)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    return body.getvalue(), boundary


def call_model(endpoint: str, image_path: Path, threshold: float, include_images: bool) -> dict[str, Any]:
    content = image_path.read_bytes()
    content_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    body, boundary = encode_multipart("file", image_path.name, content, content_type)
    query = urllib.parse.urlencode(
        {
            "threshold": clamp(float(threshold), 0.0, 1.0),
            "include_images": "true" if include_images else "false",
        }
    )
    sep = "&" if "?" in endpoint else "?"
    url = endpoint + sep + query
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"model HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"model request failed: {e}") from e
    try:
        out = json.loads(payload.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("model returned non-JSON response") from e
    if not isinstance(out, dict):
        return {"result": out}
    return out


def compact_json(value: Any, max_string: int = 200) -> Any:
    if isinstance(value, dict):
        return {str(k): compact_json(v, max_string) for k, v in value.items()}
    if isinstance(value, list):
        if value and isinstance(value[0], list):
            return {
                "type": "2d-array",
                "height": len(value),
                "width": len(value[0]) if value[0] else 0,
            }
        if len(value) > 24:
            return {"type": "array", "length": len(value), "sample": value[:24]}
        return [compact_json(v, max_string) for v in value]
    if isinstance(value, str) and len(value) > max_string:
        return {"type": "string", "length": len(value), "preview": value[:max_string]}
    return value


def rows_from_image_string(src: str | None) -> np.ndarray | None:
    if not src or not isinstance(src, str):
        return None
    if src.startswith("data:"):
        _, _, encoded = src.partition(",")
    else:
        encoded = src
    try:
        raw = base64.b64decode(encoded)
        img = Image.open(io.BytesIO(raw)).convert("L")
        return np.asarray(img, dtype=np.float32)
    except Exception:
        return None


def mask_rows_from_data(data: dict[str, Any]) -> np.ndarray | None:
    fields = ["mask", "mask_array", "maskData", "prob", "probability", "prob_map", "probability_map"]
    for key in fields:
        value = data.get(key)
        if isinstance(value, list) and value and isinstance(value[0], list):
            return np.asarray(value, dtype=np.float32)
        if isinstance(value, str):
            rows = rows_from_image_string(value)
            if rows is not None:
                return rows
        if isinstance(value, dict) and isinstance(value.get("data"), list):
            width = int(value.get("width") or 0)
            height = int(value.get("height") or 0)
            if width > 0 and height > 0 and len(value["data"]) >= width * height:
                return np.asarray(value["data"][: width * height], dtype=np.float32).reshape((height, width))
    for key in [
        "mask_png",
        "mask_png_base64",
        "mask_image",
        "mask_image_base64",
        "mask_base64",
        "prob_png",
        "prob_png_base64",
        "prob_image",
        "prob_image_base64",
        "probability_image",
        "probability_image_base64",
    ]:
        rows = rows_from_image_string(data.get(key))
        if rows is not None:
            return rows
    for key in ["images", "outputs", "debug", "result"]:
        value = data.get(key)
        if isinstance(value, dict):
            rows = mask_rows_from_data(value)
            if rows is not None:
                return rows
    return None


def normalize_y_by_x(data: dict[str, Any], target_w: int, target_h: int) -> np.ndarray | None:
    src = data.get("y_by_x")
    if not isinstance(src, list) or not src:
        return None
    src_h = float(data.get("height") or target_h)
    out = np.zeros(target_w, dtype=np.float32)
    for x in range(target_w):
        sx = 0.0 if target_w <= 1 else x / (target_w - 1) * (len(src) - 1)
        lo = int(math.floor(sx))
        hi = int(math.ceil(sx))
        t = sx - lo
        y0 = float(src[lo]) if np.isfinite(float(src[lo])) else src_h / 2
        y1 = float(src[hi]) if np.isfinite(float(src[hi])) else src_h / 2
        out[x] = (y0 * (1 - t) + y1 * t) * target_h / max(1.0, src_h)
    return out


def resize_mask_to_prob(mask: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    src_h, src_w = mask.shape[:2]
    arr = mask.astype(np.float32)
    if np.nanmax(arr) > 1.0:
        arr = arr / 255.0
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
    arr = np.clip(arr, 0.0, 1.0)
    img = Image.fromarray(np.uint8(arr * 255), mode="L").resize((target_w, target_h), Image.Resampling.BILINEAR)
    return np.asarray(img, dtype=np.float32) / 255.0


def binary_from_prob(prob: np.ndarray, threshold: float) -> np.ndarray:
    # The model heatmap is usually confident and thick. A lower threshold keeps
    # low-confidence edges that are useful for recovering QRS spike extremes.
    cutoff = max(0.08, min(0.55, float(threshold) * 0.35))
    return np.asarray(prob >= cutoff, dtype=bool)


def clean_connected_components(binary: np.ndarray, min_area: int | None = None) -> tuple[np.ndarray, list[dict[str, Any]]]:
    h, w = binary.shape
    min_area = min_area or max(18, int(h * w * 0.000015))
    visited = np.zeros_like(binary, dtype=bool)
    keep = np.zeros_like(binary, dtype=bool)
    components: list[dict[str, Any]] = []
    ys, xs = np.nonzero(binary)
    neighbors = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for seed_y, seed_x in zip(ys.tolist(), xs.tolist()):
        if visited[seed_y, seed_x] or not binary[seed_y, seed_x]:
            continue
        stack = [(seed_y, seed_x)]
        visited[seed_y, seed_x] = True
        pixels: list[tuple[int, int]] = []
        min_y = max_y = seed_y
        min_x = max_x = seed_x
        while stack:
            y, x = stack.pop()
            pixels.append((y, x))
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            for dy, dx in neighbors:
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and binary[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        area = len(pixels)
        components.append({"area": area, "bbox": [min_x, min_y, max_x, max_y]})
        if area >= min_area:
            for y, x in pixels:
                keep[y, x] = True
    if not keep.any() and components:
        largest = max(components, key=lambda c: int(c["area"]))
        x0, y0, x1, y1 = largest["bbox"]
        # Re-run only inside the largest bbox as a conservative fallback.
        keep[y0 : y1 + 1, x0 : x1 + 1] = binary[y0 : y1 + 1, x0 : x1 + 1]
    components.sort(key=lambda c: int(c["area"]), reverse=True)
    return keep, components


def skeletonize_zhang_suen(binary: np.ndarray, max_iter: int = 120) -> np.ndarray:
    img = np.pad(binary.astype(np.uint8), 1, mode="constant")
    for _ in range(max_iter):
        changed = False
        for step in (0, 1):
            p2 = img[:-2, 1:-1]
            p3 = img[:-2, 2:]
            p4 = img[1:-1, 2:]
            p5 = img[2:, 2:]
            p6 = img[2:, 1:-1]
            p7 = img[2:, :-2]
            p8 = img[1:-1, :-2]
            p9 = img[:-2, :-2]
            p1 = img[1:-1, 1:-1]
            neighbor_count = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            transitions = (
                ((p2 == 0) & (p3 == 1)).astype(np.uint8)
                + ((p3 == 0) & (p4 == 1)).astype(np.uint8)
                + ((p4 == 0) & (p5 == 1)).astype(np.uint8)
                + ((p5 == 0) & (p6 == 1)).astype(np.uint8)
                + ((p6 == 0) & (p7 == 1)).astype(np.uint8)
                + ((p7 == 0) & (p8 == 1)).astype(np.uint8)
                + ((p8 == 0) & (p9 == 1)).astype(np.uint8)
                + ((p9 == 0) & (p2 == 1)).astype(np.uint8)
            )
            if step == 0:
                side_rules = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                side_rules = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            remove = (p1 == 1) & (neighbor_count >= 2) & (neighbor_count <= 6) & (transitions == 1) & side_rules
            if remove.any():
                img[1:-1, 1:-1][remove] = 0
                changed = True
        if not changed:
            break
    return img[1:-1, 1:-1].astype(bool)


def save_binary(binary: np.ndarray, path: Path) -> None:
    Image.fromarray(np.uint8(binary) * 255, mode="L").save(path)


def interpolate_missing(line: np.ndarray) -> np.ndarray:
    out = line.astype(np.float32).copy()
    good = np.flatnonzero(np.isfinite(out))
    if len(good) == 0:
        return out
    first, last = int(good[0]), int(good[-1])
    out[:first] = out[first]
    out[last + 1 :] = out[last]
    missing = ~np.isfinite(out)
    if missing.any():
        xs = np.arange(len(out))
        out[missing] = np.interp(xs[missing], xs[good], out[good])
    return out


def skeleton_trace_by_x(skeleton: np.ndarray, prob: np.ndarray, seed: np.ndarray | None = None) -> np.ndarray:
    h, w = skeleton.shape
    out = np.full(w, np.nan, dtype=np.float32)
    prev = float(seed[0]) if seed is not None and len(seed) else h / 2
    for x in range(w):
        ys = np.flatnonzero(skeleton[:, x])
        if len(ys) == 0:
            continue
        if len(ys) == 1:
            chosen = float(ys[0])
        else:
            weights = prob[ys, x].astype(np.float32) + 0.02
            center = float(np.sum(ys * weights) / max(1e-6, float(np.sum(weights))))
            closest = float(ys[int(np.argmin(np.abs(ys - prev)))])
            # Blend weighted skeleton center with continuity. The spike repair
            # stage later restores true extrema for tall QRS columns.
            chosen = closest * 0.70 + center * 0.30
        out[x] = chosen
        prev = chosen
    return interpolate_missing(out)


def mask_column_edges(binary: np.ndarray, target_w: int, target_h: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    src_h, src_w = binary.shape
    top = np.full(target_w, np.nan, dtype=np.float32)
    bottom = np.full(target_w, np.nan, dtype=np.float32)
    span = np.zeros(target_w, dtype=np.float32)
    for x in range(target_w):
        sx = round(x / max(1, target_w - 1) * (src_w - 1))
        ys = np.flatnonzero(binary[:, sx])
        if len(ys) == 0:
            continue
        top[x] = float(ys[0]) / src_h * target_h
        bottom[x] = float(ys[-1]) / src_h * target_h
        span[x] = bottom[x] - top[x] + 1
    return top, bottom, span


def mask_column_edges_near_line(binary: np.ndarray, trace: np.ndarray, target_h: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    src_h, src_w = binary.shape
    target_w = len(trace)
    top = np.full(target_w, np.nan, dtype=np.float32)
    bottom = np.full(target_w, np.nan, dtype=np.float32)
    span = np.zeros(target_w, dtype=np.float32)
    for x, y in enumerate(trace):
        if not np.isfinite(y):
            continue
        sx = round(x / max(1, target_w - 1) * (src_w - 1))
        seed_y = int(round(clamp(float(y) / max(1, target_h) * src_h, 0, src_h - 1)))
        col = binary[:, sx]
        best: tuple[float, int, int] | None = None
        run_start = -1
        for r in range(src_h + 1):
            on = r < src_h and bool(col[r])
            if on and run_start < 0:
                run_start = r
            if (not on or r == src_h) and run_start >= 0:
                run_end = r - 1
                contains = run_start <= seed_y <= run_end
                dist = 0 if contains else min(abs(seed_y - run_start), abs(seed_y - run_end))
                run_span = run_end - run_start + 1
                # Prefer the run that contains or is closest to the current
                # trace. This prevents another lead/artifact in the same column
                # from stretching the spike repair to the whole image height.
                score = (10000 if contains else 0) - dist * 25 + min(run_span, src_h * 0.20)
                if best is None or score > best[0]:
                    best = (score, run_start, run_end)
                run_start = -1
        if best is None:
            continue
        _, y0, y1 = best
        top[x] = y0 / src_h * target_h
        bottom[x] = y1 / src_h * target_h
        span[x] = bottom[x] - top[x] + 1
    return top, bottom, span


def groups_from_bool(flags: np.ndarray, max_gap: int = 2, min_width: int = 1) -> list[tuple[int, int]]:
    xs = np.flatnonzero(flags)
    if len(xs) == 0:
        return []
    groups: list[tuple[int, int]] = []
    start = prev = int(xs[0])
    for x in xs[1:].tolist():
        if x - prev <= max_gap + 1:
            prev = int(x)
        else:
            if prev - start + 1 >= min_width:
                groups.append((start, prev))
            start = prev = int(x)
    if prev - start + 1 >= min_width:
        groups.append((start, prev))
    return groups


def detect_qrs_windows(span: np.ndarray, target_h: int) -> tuple[list[tuple[int, int]], float]:
    valid_spans = span[np.isfinite(span) & (span > 0)]
    if len(valid_spans) < 10:
        return [], 0.0
    q50 = float(np.percentile(valid_spans, 50))
    q90 = float(np.percentile(valid_spans, 90))
    tall_threshold = max(target_h * 0.12, q90, q50 * 3.0)
    raw_groups = groups_from_bool(span >= tall_threshold, max_gap=2, min_width=1)
    groups: list[tuple[int, int]] = []
    merge_gap = max(10, int(round(target_h * 0.025)))
    for a, b in raw_groups:
        if groups and a - groups[-1][1] <= merge_gap:
            groups[-1] = (groups[-1][0], b)
        else:
            groups.append((a, b))
    return groups, tall_threshold


def qrs_windows_from_model(data: dict[str, Any], target_w: int, target_h: int) -> list[tuple[int, int]]:
    peaks = None
    for key in ["r_peaks", "rPeaks", "qrs_peaks", "qrsPeaks"]:
        value = data.get(key)
        if isinstance(value, list) and value:
            peaks = value
            break
    if not peaks:
        return []
    src_w = float(data.get("width") or target_w)
    half_width = max(8, int(round(target_h * 0.018)))
    windows: list[tuple[int, int]] = []
    for item in peaks:
        if isinstance(item, dict):
            x_val = item.get("x")
        else:
            x_val = item
        try:
            x = int(round(float(x_val) * target_w / max(1.0, src_w)))
        except Exception:
            continue
        windows.append((max(0, x - half_width), min(target_w - 1, x + half_width)))
    if not windows:
        return []
    windows.sort()
    merged: list[tuple[int, int]] = []
    for a, b in windows:
        if merged and a <= merged[-1][1] + half_width:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def expand_windows(windows: list[tuple[int, int]], width: int, pad: int) -> list[tuple[int, int]]:
    return [(max(0, a - pad), min(width - 1, b + pad)) for a, b in windows]


def in_windows(x: int, windows: list[tuple[int, int]]) -> bool:
    return any(a <= x <= b for a, b in windows)


def median_filter_line(line: np.ndarray, radius: int) -> np.ndarray:
    out = line.copy()
    for x in range(len(line)):
        lo = max(0, x - radius)
        hi = min(len(line), x + radius + 1)
        vals = line[lo:hi]
        vals = vals[np.isfinite(vals)]
        if len(vals):
            out[x] = float(np.median(vals))
    return out


def stabilize_non_qrs_trace(trace: np.ndarray, qrs_windows: list[tuple[int, int]], target_h: int) -> np.ndarray:
    out = trace.copy()
    protected = expand_windows(qrs_windows, len(trace), max(6, int(round(target_h * 0.012))))
    smoothed = median_filter_line(out, radius=2)
    max_step = max(3.0, target_h * 0.012)
    for x in range(len(out)):
        if not in_windows(x, protected):
            out[x] = smoothed[x]
    # Outside QRS, large single-column jumps are much more likely to be trace
    # hopping than real ECG morphology. Clamp them forward and backward.
    for x in range(1, len(out)):
        if in_windows(x, protected) or in_windows(x - 1, protected):
            continue
        diff = float(out[x] - out[x - 1])
        if abs(diff) > max_step:
            out[x] = out[x - 1] + math.copysign(max_step, diff)
    for x in range(len(out) - 2, -1, -1):
        if in_windows(x, protected) or in_windows(x + 1, protected):
            continue
        diff = float(out[x] - out[x + 1])
        if abs(diff) > max_step:
            out[x] = out[x + 1] + math.copysign(max_step, diff)
    return out


def image_dark_extrema(image: Image.Image, x: int, y0: float, y1: float) -> tuple[float | None, float | None]:
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    h, w = gray.shape
    x0 = max(0, min(w - 1, int(round(x))))
    lo = int(round(clamp(min(y0, y1), 0, h - 1)))
    hi = int(round(clamp(max(y0, y1), 0, h - 1)))
    if hi <= lo:
        return None, None
    col = gray[lo : hi + 1, x0]
    dark = col <= max(35, int(np.percentile(col, 22)))
    ys = np.flatnonzero(dark)
    if len(ys) == 0:
        return None, None
    return float(lo + ys[0]), float(lo + ys[-1])


def repair_spikes_from_mask(
    trace: np.ndarray,
    top: np.ndarray,
    bottom: np.ndarray,
    span: np.ndarray,
    target_h: int,
    qrs_windows: list[tuple[int, int]] | None = None,
    image: Image.Image | None = None,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    out = trace.copy()
    anchors: list[dict[str, Any]] = []
    if qrs_windows is None:
        qrs_windows, tall_threshold = detect_qrs_windows(span, target_h)
    else:
        _, tall_threshold = detect_qrs_windows(span, target_h)
    if not qrs_windows:
        return out, anchors

    min_dev = max(8.0, target_h * 0.035)
    for a, b in qrs_windows:
        pad = max(14, (b - a + 1) * 4)
        base = local_median(trace, a - pad, b + pad, (max(0, a - 3), min(len(trace) - 1, b + 3)))
        if not math.isfinite(base):
            continue
        candidates_by_polarity: dict[str, tuple[float, int, float, str]] = {}
        for x in range(a, b + 1):
            if np.isfinite(top[x]):
                y_top = float(top[x])
                if image is not None and np.isfinite(bottom[x]):
                    dark_top, _ = image_dark_extrema(image, x, y_top, float(bottom[x]))
                    if dark_top is not None:
                        y_top = dark_top
                item = (abs(y_top - base), x, y_top, "top")
                if "top" not in candidates_by_polarity or item[0] > candidates_by_polarity["top"][0]:
                    candidates_by_polarity["top"] = item
            if np.isfinite(bottom[x]):
                y_bottom = float(bottom[x])
                if image is not None and np.isfinite(top[x]):
                    _, dark_bottom = image_dark_extrema(image, x, float(top[x]), y_bottom)
                    if dark_bottom is not None:
                        y_bottom = dark_bottom
                item = (abs(y_bottom - base), x, y_bottom, "bottom")
                if "bottom" not in candidates_by_polarity or item[0] > candidates_by_polarity["bottom"][0]:
                    candidates_by_polarity["bottom"] = item

        candidates = [item for item in candidates_by_polarity.values() if item[0] >= min_dev]
        candidates.sort(key=lambda item: item[1])
        for dev, peak_x, peak_y, polarity in candidates:
            # Keep the repair tight. The surrounding trace already follows the
            # waveform well; only the narrow spike tip needs help.
            half_width = max(3, min(11, int(round((b - a + 1) * 0.45 + 4))))
            left = max(0, peak_x - half_width)
            right = min(len(out) - 1, peak_x + half_width)
            left_y = float(trace[left]) if np.isfinite(trace[left]) else base
            right_y = float(trace[right]) if np.isfinite(trace[right]) else base
            for x in range(left, right + 1):
                if x <= peak_x:
                    t = (x - left) / max(1, peak_x - left)
                    target = left_y + (peak_y - left_y) * t
                else:
                    t = (x - peak_x) / max(1, right - peak_x)
                    target = peak_y + (right_y - peak_y) * t
                edge_fade = min((x - left) / max(1, peak_x - left), (right - x) / max(1, right - peak_x))
                weight = clamp(edge_fade, 0.0, 1.0)
                out[x] = clamp(float(trace[x]) * (1 - weight) + target * weight, 0, target_h - 1)
            anchors.append(
                {
                    "left": left,
                    "peak_x": int(peak_x),
                    "right": right,
                    "peak_y": float(peak_y),
                    "polarity": polarity,
                    "window": [int(a), int(b)],
                    "span_threshold": round(tall_threshold, 3),
                }
            )
    return out, anchors


def mask_run_center_near(binary: np.ndarray, x: int, seed_y: float) -> float | None:
    h, w = binary.shape
    x = max(0, min(w - 1, int(round(x))))
    seed = int(round(clamp(float(seed_y), 0, h - 1)))
    col = binary[:, x]
    best: tuple[float, int, int] | None = None
    run_start = -1
    for r in range(h + 1):
        on = r < h and bool(col[r])
        if on and run_start < 0:
            run_start = r
        if (not on or r == h) and run_start >= 0:
            run_end = r - 1
            contains = run_start <= seed <= run_end
            dist = 0 if contains else min(abs(seed - run_start), abs(seed - run_end))
            run_span = run_end - run_start + 1
            score = (10000 if contains else 0) - dist * 30 + min(run_span, h * 0.12)
            if best is None or score > best[0]:
                best = (score, run_start, run_end)
            run_start = -1
    if best is None:
        return None
    _, y0, y1 = best
    return (y0 + y1) / 2.0


def dedupe_anchor_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not points:
        return []
    points = sorted(points, key=lambda p: (float(p["x"]), int(p.get("priority", 0))))
    out: list[dict[str, Any]] = []
    for p in points:
        if out and abs(float(p["x"]) - float(out[-1]["x"])) < 1e-6:
            if int(p.get("priority", 0)) >= int(out[-1].get("priority", 0)):
                out[-1] = p
        else:
            out.append(p)
    return out


def anchor_trace_from_mask(
    binary: np.ndarray,
    stable_trace: np.ndarray,
    top: np.ndarray,
    bottom: np.ndarray,
    qrs_windows: list[tuple[int, int]],
    image: Image.Image | None = None,
    sample_step: int = 4,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    h, w = binary.shape
    protected = expand_windows(qrs_windows, w, max(6, int(round(h * 0.012))))
    points: list[dict[str, Any]] = []

    for x in range(0, w, sample_step):
        if in_windows(x, protected):
            continue
        y = mask_run_center_near(binary, x, stable_trace[x])
        if y is None:
            y = float(stable_trace[x])
        points.append({"x": float(x), "y": float(y), "kind": "mask_center", "priority": 1})

    for a, b in qrs_windows:
        left = max(0, a - 2)
        right = min(w - 1, b + 2)
        points.append({"x": float(left), "y": float(stable_trace[left]), "kind": "qrs_entry", "priority": 2})
        points.append({"x": float(right), "y": float(stable_trace[right]), "kind": "qrs_exit", "priority": 2})
        base = local_median(stable_trace, a - max(10, b - a + 1), b + max(10, b - a + 1), (a, b))
        if not math.isfinite(base):
            base = float(stable_trace[(a + b) // 2])
        candidates: list[tuple[float, int, float, str]] = []
        for x in range(a, b + 1):
            if np.isfinite(top[x]):
                y = float(top[x])
                if image is not None and np.isfinite(bottom[x]):
                    dark_top, _ = image_dark_extrema(image, x, y, float(bottom[x]))
                    if dark_top is not None:
                        y = dark_top
                candidates.append((abs(y - base), x, y, "qrs_top"))
            if np.isfinite(bottom[x]):
                y = float(bottom[x])
                if image is not None and np.isfinite(top[x]):
                    _, dark_bottom = image_dark_extrema(image, x, float(top[x]), y)
                    if dark_bottom is not None:
                        y = dark_bottom
                candidates.append((abs(y - base), x, y, "qrs_bottom"))
        if candidates:
            # Add at most two extrema in x order. This follows biphasic QRS
            # morphology while avoiding dense back-and-forth hopping.
            candidates.sort(key=lambda item: item[0], reverse=True)
            chosen: list[tuple[float, int, float, str]] = []
            for item in candidates:
                if all(abs(item[1] - prev[1]) > 3 for prev in chosen):
                    chosen.append(item)
                if len(chosen) >= 2:
                    break
            for _, x, y, kind in sorted(chosen, key=lambda item: item[1]):
                points.append({"x": float(x), "y": float(y), "kind": kind, "priority": 5})

    if not points:
        return stable_trace.copy(), []
    points = dedupe_anchor_points(points)
    xs = np.asarray([float(p["x"]) for p in points], dtype=np.float32)
    ys = np.asarray([float(p["y"]) for p in points], dtype=np.float32)
    out_x = np.arange(w, dtype=np.float32)
    out = np.interp(out_x, xs, ys).astype(np.float32)
    return out, [{"peak_x": int(round(p["x"])), "peak_y": float(p["y"]), "kind": p["kind"]} for p in points]


def build_beat_windows(qrs_windows: list[tuple[int, int]], width: int) -> list[dict[str, int]]:
    if not qrs_windows:
        return [{"start": 0, "end": width - 1, "qrs_start": -1, "qrs_end": -1, "qrs_center": width // 2}]
    centers = [(a + b) // 2 for a, b in qrs_windows]
    beats: list[dict[str, int]] = []
    for i, (a, b) in enumerate(qrs_windows):
        left_boundary = 0 if i == 0 else (centers[i - 1] + centers[i]) // 2
        right_boundary = width - 1 if i == len(qrs_windows) - 1 else (centers[i] + centers[i + 1]) // 2
        beats.append(
            {
                "start": int(left_boundary),
                "end": int(right_boundary),
                "qrs_start": int(a),
                "qrs_end": int(b),
                "qrs_center": int(centers[i]),
            }
        )
    return beats


def line_from_mask_centers(binary: np.ndarray, seed: np.ndarray) -> np.ndarray:
    out = np.full(len(seed), np.nan, dtype=np.float32)
    for x in range(len(seed)):
        y = mask_run_center_near(binary, x, seed[x])
        out[x] = float(seed[x] if y is None else y)
    return interpolate_missing(out)


def add_point(points: list[dict[str, Any]], x: int, y: float, kind: str, priority: int) -> None:
    if math.isfinite(float(y)):
        points.append({"x": float(x), "y": float(y), "kind": kind, "priority": priority})


def add_region_extreme(
    points: list[dict[str, Any]],
    line: np.ndarray,
    start: int,
    end: int,
    baseline: float,
    kind: str,
    min_dev: float,
) -> None:
    if end <= start:
        return
    start = max(0, start)
    end = min(len(line) - 1, end)
    vals = line[start : end + 1]
    if len(vals) < 4:
        return
    dev = np.abs(vals - baseline)
    idx = int(np.nanargmax(dev))
    peak_dev = float(dev[idx])
    if not math.isfinite(peak_dev) or peak_dev < min_dev:
        return
    x = start + idx
    add_point(points, x, float(line[x]), kind, 3)


def ecg_structured_anchor_trace(
    binary: np.ndarray,
    stable_trace: np.ndarray,
    top: np.ndarray,
    bottom: np.ndarray,
    qrs_windows: list[tuple[int, int]],
    image: Image.Image | None = None,
) -> tuple[np.ndarray, list[dict[str, Any]], list[dict[str, int]]]:
    h, w = binary.shape
    mask_center_line = line_from_mask_centers(binary, stable_trace)
    beats = build_beat_windows(qrs_windows, w)
    points: list[dict[str, Any]] = []
    qrs_pad = max(6, int(round(h * 0.012)))
    protected = expand_windows(qrs_windows, w, qrs_pad)
    min_wave_dev = max(3.0, h * 0.008)

    # Adaptive non-QRS sampling: use sparse points on smooth regions and add
    # points where curvature changes, which preserves P/ST/T morphology without
    # recreating column-to-column jitter.
    prev_slope = 0.0
    for x in range(0, w, 6):
        if in_windows(x, protected):
            continue
        add_point(points, x, float(mask_center_line[x]), "non_qrs_mask_center", 1)
        if 2 <= x < w - 2:
            slope = float(mask_center_line[x + 2] - mask_center_line[x - 2])
            if abs(slope - prev_slope) > max(3.0, h * 0.009):
                add_point(points, x, float(mask_center_line[x]), "non_qrs_curvature", 2)
            prev_slope = slope

    for beat in beats:
        bs, be = beat["start"], beat["end"]
        qs, qe = beat["qrs_start"], beat["qrs_end"]
        if qs < 0 or qe < 0:
            continue
        add_point(points, bs, float(mask_center_line[bs]), "beat_start", 1)
        add_point(points, be, float(mask_center_line[be]), "beat_end", 1)
        entry = max(bs, qs - 2)
        exit_ = min(be, qe + 2)
        add_point(points, entry, float(mask_center_line[entry]), "qrs_entry", 4)
        add_point(points, exit_, float(mask_center_line[exit_]), "qrs_exit", 4)

        pre_len = max(0, qs - bs)
        post_len = max(0, be - qe)
        baseline = local_median(mask_center_line, max(bs, qs - max(10, pre_len // 2)), min(be, qe + max(10, post_len // 2)), (qs, qe))
        if not math.isfinite(baseline):
            baseline = float(mask_center_line[max(bs, min(be, qs))])

        # P-wave candidate before QRS and T-wave candidate after QRS.
        add_region_extreme(points, mask_center_line, bs + pre_len // 5, qs - max(4, pre_len // 8), baseline, "p_candidate", min_wave_dev)
        add_region_extreme(points, mask_center_line, qe + max(3, post_len // 10), min(be, qe + int(post_len * 0.72)), baseline, "t_candidate", min_wave_dev)

        qrs_candidates: list[tuple[float, int, float, str]] = []
        for x in range(qs, qe + 1):
            if np.isfinite(top[x]):
                y = float(top[x])
                if image is not None and np.isfinite(bottom[x]):
                    dark_top, _ = image_dark_extrema(image, x, y, float(bottom[x]))
                    if dark_top is not None:
                        y = dark_top
                qrs_candidates.append((abs(y - baseline), x, y, "qrs_top"))
            if np.isfinite(bottom[x]):
                y = float(bottom[x])
                if image is not None and np.isfinite(top[x]):
                    _, dark_bottom = image_dark_extrema(image, x, float(top[x]), y)
                    if dark_bottom is not None:
                        y = dark_bottom
                qrs_candidates.append((abs(y - baseline), x, y, "qrs_bottom"))
        qrs_candidates.sort(key=lambda item: item[0], reverse=True)
        chosen: list[tuple[float, int, float, str]] = []
        for item in qrs_candidates:
            if item[0] < max(8.0, h * 0.030):
                continue
            if all(abs(item[1] - prev[1]) > 3 for prev in chosen):
                chosen.append(item)
            if len(chosen) >= 3:
                break
        for _, x, y, kind in sorted(chosen, key=lambda item: item[1]):
            add_point(points, x, y, kind, 6)

    points = dedupe_anchor_points(points)
    if not points:
        return stable_trace.copy(), [], beats
    xs = np.asarray([float(p["x"]) for p in points], dtype=np.float32)
    ys = np.asarray([float(p["y"]) for p in points], dtype=np.float32)
    out = np.interp(np.arange(w, dtype=np.float32), xs, ys).astype(np.float32)
    anchors = [{"peak_x": int(round(p["x"])), "peak_y": float(p["y"]), "kind": p["kind"]} for p in points]
    return out, anchors, beats


def trace_quality(final_trace: np.ndarray | None, binary: np.ndarray | None, qrs_windows: list[tuple[int, int]], anchors: list[dict[str, Any]]) -> dict[str, Any]:
    if final_trace is None:
        return {"confidence": 0.0, "reason": "no final trace"}
    target_h = binary.shape[0] if binary is not None else max(1, int(np.nanmax(final_trace)) + 1)
    diagnostics = trace_mask_diagnostics(final_trace, binary, qrs_windows, target_h)
    coverage = diagnostics["mask_coverage"]
    large_jump_count = int(diagnostics["outside_qrs_large_jumps"])
    confidence = 0.55
    if coverage is not None:
        confidence += min(0.30, float(coverage) * 0.30)
    confidence += min(0.10, len(qrs_windows) * 0.025)
    confidence -= min(0.25, large_jump_count * 0.015)
    return {
        "confidence": round(clamp(confidence, 0.0, 1.0), 4),
        "mask_coverage": round(float(coverage), 4) if coverage is not None else None,
        "qrs_count": len(qrs_windows),
        "anchor_count": len(anchors),
        "large_jump_count": large_jump_count,
        "mask_distance_median": diagnostics["mask_distance_median"],
        "mask_distance_p95": diagnostics["mask_distance_p95"],
    }


def trace_mask_diagnostics(
    trace: np.ndarray | None,
    binary: np.ndarray | None,
    qrs_windows: list[tuple[int, int]],
    target_h: int,
) -> dict[str, Any]:
    """Measure mask adherence without rewarding an over-smoothed trace.

    Exact coverage is intentionally strict, while the distance percentiles
    reveal whether misses are harmless sub-pixel interpolation or a real path
    that cuts across empty mask regions.
    """
    if trace is None or len(trace) == 0:
        return {
            "mask_coverage": None,
            "mask_distance_median": None,
            "mask_distance_p95": None,
            "outside_qrs_large_jumps": 0,
        }

    protected = expand_windows(qrs_windows, len(trace), max(10, int(round(target_h * 0.02))))
    jump_threshold = max(12.0, target_h * 0.02)
    large_jumps = sum(
        1
        for x in range(1, len(trace))
        if not in_windows(x, protected)
        and not in_windows(x - 1, protected)
        and abs(float(trace[x] - trace[x - 1])) > jump_threshold
    )
    if binary is None or not binary.any():
        return {
            "mask_coverage": None,
            "mask_distance_median": None,
            "mask_distance_p95": None,
            "outside_qrs_large_jumps": large_jumps,
        }

    src_h, src_w = binary.shape
    hits = 0
    distances: list[float] = []
    for x, y in enumerate(trace):
        if not np.isfinite(y):
            continue
        sx = round(x / max(1, len(trace) - 1) * (src_w - 1))
        sy = float(y) / max(1, target_h - 1) * (src_h - 1)
        yy = int(round(clamp(sy, 0, src_h - 1)))
        if binary[yy, sx]:
            hits += 1
            distances.append(0.0)
            continue
        active_y = np.flatnonzero(binary[:, sx])
        if len(active_y):
            distances.append(float(np.min(np.abs(active_y.astype(np.float32) - sy))) * target_h / src_h)

    return {
        "mask_coverage": round(hits / max(1, len(trace)), 4),
        "mask_distance_median": round(float(np.median(distances)), 4) if distances else None,
        "mask_distance_p95": round(float(np.percentile(distances, 95)), 4) if distances else None,
        "outside_qrs_large_jumps": large_jumps,
    }


def select_postprocess_trace(
    repaired_trace: np.ndarray,
    structured_trace: np.ndarray,
    binary: np.ndarray,
    qrs_windows: list[tuple[int, int]],
    target_h: int,
) -> tuple[np.ndarray, str, dict[str, dict[str, Any]]]:
    """Keep an experimental reconstruction only when it cannot regress fit.

    Sparse anchor interpolation can look smooth while cutting across tens of
    pixels of empty mask. The repaired centerline is therefore the safe
    baseline; the structured candidate must stay close on both exact coverage
    and tail distance before it may replace that baseline.
    """
    repaired_metrics = trace_mask_diagnostics(repaired_trace, binary, qrs_windows, target_h)
    structured_metrics = trace_mask_diagnostics(structured_trace, binary, qrs_windows, target_h)
    metrics = {"qrs_repair": repaired_metrics, "structured_anchor": structured_metrics}

    base_coverage = repaired_metrics.get("mask_coverage")
    new_coverage = structured_metrics.get("mask_coverage")
    base_p95 = repaired_metrics.get("mask_distance_p95")
    new_p95 = structured_metrics.get("mask_distance_p95")
    base_jumps = int(repaired_metrics.get("outside_qrs_large_jumps") or 0)
    new_jumps = int(structured_metrics.get("outside_qrs_large_jumps") or 0)
    acceptable = (
        base_coverage is not None
        and new_coverage is not None
        and base_p95 is not None
        and new_p95 is not None
        and float(new_coverage) >= float(base_coverage) - 0.02
        and float(new_p95) <= max(3.0, float(base_p95) + 2.0)
        and new_jumps <= base_jumps + 1
    )
    if acceptable:
        return structured_trace, "structured_anchor", metrics
    return repaired_trace, "qrs_repair_guarded", metrics


def dp_trace_from_prob(prob: np.ndarray) -> np.ndarray:
    h, w = prob.shape
    eps = 1e-4
    max_step = max(8, min(20, round(h * 0.10)))
    smooth_weight = 0.08
    prev = np.zeros((w, h), dtype=np.int16)
    dp_prev = -np.log(prob[:, 0] + eps)
    dp_curr = np.zeros(h, dtype=np.float32)
    for x in range(1, w):
        for y in range(h):
            best = float("inf")
            best_y = y
            for yp in range(max(0, y - max_step), min(h - 1, y + max_step) + 1):
                dy = abs(y - yp)
                cost = dp_prev[yp] + smooth_weight * (dy * dy / 8 if dy <= 4 else dy - 2)
                if cost < best:
                    best = cost
                    best_y = yp
            dp_curr[y] = best - math.log(float(prob[y, x]) + eps)
            prev[x, y] = best_y
        dp_prev = dp_curr.copy()
    y_best = int(np.argmin(dp_prev))
    out = np.zeros(w, dtype=np.float32)
    for x in range(w - 1, -1, -1):
        out[x] = y_best
        if x > 0:
            y_best = int(prev[x, y_best])
    return out


def local_median(values: np.ndarray, lo: int, hi: int, skip: tuple[int, int] | None = None) -> float:
    lo = max(0, lo)
    hi = min(len(values) - 1, hi)
    arr: list[float] = []
    for i in range(lo, hi + 1):
        if skip and skip[0] <= i <= skip[1]:
            continue
        v = float(values[i])
        if math.isfinite(v):
            arr.append(v)
    if not arr:
        return float("nan")
    arr.sort()
    return arr[len(arr) // 2]


def mask_edges_near_trace(mask: np.ndarray, trace: np.ndarray, threshold: float, target_h: int) -> tuple[np.ndarray, np.ndarray]:
    src_h, src_w = mask.shape[:2]
    top = np.full(len(trace), np.nan, dtype=np.float32)
    bottom = np.full(len(trace), np.nan, dtype=np.float32)
    active255 = max(24.0, min(128.0, threshold * 255.0 * 0.35))
    active01 = max(0.08, min(threshold, threshold * 0.35))
    for x, y in enumerate(trace):
        if not math.isfinite(float(y)):
            continue
        sx = round(x / max(1, len(trace) - 1) * (src_w - 1))
        seed_y = round(clamp(float(y) / max(1, target_h) * src_h, 0, src_h - 1))
        best: tuple[float, int, int] | None = None
        run_start = -1
        for r in range(src_h + 1):
            value = float(mask[r, sx]) if r < src_h else 0.0
            on = r < src_h and (value >= active255 if value > 1 else value >= active01)
            if on and run_start < 0:
                run_start = r
            if (not on or r == src_h) and run_start >= 0:
                run_end = r - 1
                span = run_end - run_start + 1
                touches_both = run_start <= 1 and run_end >= src_h - 2
                over_tall = span > src_h * 0.68
                if not touches_both and not over_tall:
                    contains = run_start <= seed_y <= run_end
                    dist = 0 if contains else min(abs(seed_y - run_start), abs(seed_y - run_end))
                    moderate_span = min(span, src_h * 0.18)
                    tall_penalty = max(0.0, span - src_h * 0.34) * 8
                    score = (10000 if contains else 0) - dist * 18 + moderate_span * 3 - tall_penalty
                    if best is None or score > best[0]:
                        best = (score, run_start, run_end)
                run_start = -1
        if best:
            _, y0, y1 = best
            top[x] = y0 / src_h * target_h
            bottom[x] = y1 / src_h * target_h
    return top, bottom


def apply_spike_anchors(trace: np.ndarray, top: np.ndarray, bottom: np.ndarray, target_h: int) -> tuple[np.ndarray, list[dict[str, Any]]]:
    out = trace.copy()
    spans = [float(bottom[i] - top[i] + 1) for i in range(len(trace)) if np.isfinite(top[i]) and np.isfinite(bottom[i])]
    anchors: list[dict[str, Any]] = []
    if len(spans) < 8:
        return out, anchors
    spans_sorted = sorted(spans)
    q = lambda p: spans_sorted[int(clamp(p, 0, 1) * (len(spans_sorted) - 1))]
    q50, q75, q90 = q(0.50), q(0.75), q(0.90)
    span_t = max(5.0, q90, q50 + 1.8 * max(1.0, q75 - q50))
    cols = [i for i in range(len(trace)) if np.isfinite(top[i]) and np.isfinite(bottom[i]) and bottom[i] - top[i] + 1 >= span_t]
    if not cols:
        return out, anchors
    groups: list[tuple[int, int]] = []
    start = prev = cols[0]
    for x in cols[1:]:
        if x - prev <= 3:
            prev = x
        else:
            if prev - start + 1 >= 4:
                groups.append((start, prev))
            start = prev = x
    if prev - start + 1 >= 4:
        groups.append((start, prev))
    min_dev = max(3.0, target_h * 0.025)
    max_shift = max(5.0, target_h * 0.30)
    for a, b in groups:
        group_w = b - a + 1
        pad = max(10, round(group_w * 3.5))
        base = local_median(trace, a - pad, b + pad, (a - round(group_w * 0.5), b + round(group_w * 0.5)))
        if not math.isfinite(base):
            continue
        best: dict[str, Any] | None = None
        for x in range(max(0, a - group_w), min(len(trace) - 1, b + group_w) + 1):
            if not (np.isfinite(top[x]) and np.isfinite(bottom[x])):
                continue
            up_dev = base - top[x]
            down_dev = bottom[x] - base
            polarity = "top" if up_dev >= down_dev else "bottom"
            y_edge = float(top[x] if polarity == "top" else bottom[x])
            dev = float(max(up_dev, down_dev))
            if dev < min_dev or abs(y_edge - float(trace[x])) > max_shift:
                continue
            score = dev + (1 - min(1, abs(x - (a + b) / 2) / max(1, group_w))) * min_dev
            if best is None or score > best["score"]:
                best = {"x": x, "y": y_edge, "polarity": polarity, "dev": dev, "score": score}
        if best is None:
            continue
        foot_dev = max(1.5, best["dev"] * 0.28)
        max_foot = max(5, round(group_w * 1.4 + 3))
        left = right = int(best["x"])
        for x in range(int(best["x"]), max(-1, int(best["x"]) - max_foot) - 1, -1):
            edge = top[x] if best["polarity"] == "top" else bottom[x]
            dev = base - edge if best["polarity"] == "top" else edge - base
            if not np.isfinite(edge) or dev <= foot_dev:
                left = x
                break
            left = x
        for x in range(int(best["x"]), min(len(trace), int(best["x"]) + max_foot + 1)):
            edge = top[x] if best["polarity"] == "top" else bottom[x]
            dev = base - edge if best["polarity"] == "top" else edge - base
            if not np.isfinite(edge) or dev <= foot_dev:
                right = x
                break
            right = x
        if right - left < 3:
            continue
        left_y = float(trace[left]) if np.isfinite(trace[left]) else float(base)
        right_y = float(trace[right]) if np.isfinite(trace[right]) else float(base)
        peak_x = int(best["x"])
        peak_y = float(best["y"])
        for x in range(left, right + 1):
            if x <= peak_x:
                t = (x - left) / max(1, peak_x - left)
                y = left_y + (peak_y - left_y) * t
            else:
                t = (x - peak_x) / max(1, right - peak_x)
                y = peak_y + (right_y - peak_y) * t
            out[x] = clamp(y, 0, target_h - 1)
        anchors.append({"left": left, "peak_x": peak_x, "right": right, "peak_y": peak_y, "polarity": best["polarity"]})
    return out, anchors


def save_heatmap(rows: np.ndarray, path: Path) -> None:
    arr = rows.astype(np.float32)
    if np.nanmax(arr) > 1.0:
        arr = arr / 255.0
    arr = np.clip(np.nan_to_num(arr), 0, 1)
    rgb = np.zeros((arr.shape[0], arr.shape[1], 3), dtype=np.uint8)
    rgb[:, :, 0] = np.uint8(255 * arr)
    rgb[:, :, 1] = np.uint8(80 + 120 * arr)
    rgb[:, :, 2] = np.uint8(255 * (1 - arr))
    Image.fromarray(rgb, mode="RGB").save(path)


def draw_overlay(img: Image.Image, traces: dict[str, np.ndarray], path: Path, anchors: list[dict[str, Any]] | None = None) -> None:
    colors = {
        "raw_y_by_x": (249, 115, 22),
        "dp_path": (37, 99, 235),
        "skeleton_trace": (168, 85, 247),
        "stable_trace": (20, 184, 166),
        "post_spike": (34, 197, 94),
    }
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out, "RGBA")
    for name, line in traces.items():
        if line is None or len(line) == 0:
            continue
        pts = []
        span = max(1, len(line) - 1)
        for i, y in enumerate(line):
            if np.isfinite(y):
                pts.append((i / span * (out.width - 1), float(y)))
            elif len(pts) > 1:
                draw.line(pts, fill=colors.get(name, (239, 68, 68)) + (220,), width=2)
                pts = []
        if len(pts) > 1:
            draw.line(pts, fill=colors.get(name, (239, 68, 68)) + (220,), width=2)
    if anchors:
        for a in anchors:
            x, y = float(a["peak_x"]), float(a["peak_y"])
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), outline=(16, 185, 129, 255), width=2)
    out.save(path)


def line_to_list(line: np.ndarray | None, limit: int = 5000) -> list[float | None] | None:
    if line is None:
        return None
    step = max(1, math.ceil(len(line) / limit))
    out: list[float | None] = []
    for v in line[::step]:
        out.append(round(float(v), 4) if np.isfinite(v) else None)
    return out


def write_report(out_dir: Path, summary: dict[str, Any]) -> None:
    keys = summary.get("model_keys") or []
    rows = [
        ("endpoint", summary.get("endpoint")),
        ("input", summary.get("input")),
        ("crop_size", summary.get("crop_size")),
        ("threshold", summary.get("threshold")),
        ("model_keys", ", ".join(keys)),
        ("mask_shape", summary.get("mask_shape")),
        ("component_count", summary.get("component_count")),
        ("raw_y_by_x_points", summary.get("raw_y_by_x_points")),
        ("dp_points", summary.get("dp_points")),
        ("skeleton_points", summary.get("skeleton_points")),
        ("qrs_window_count", summary.get("qrs_window_count")),
        ("beat_count", summary.get("beat_count")),
        ("postprocess_method", summary.get("postprocess_method")),
        ("quality", summary.get("quality")),
        ("candidate_metrics", summary.get("candidate_metrics")),
        ("spike_anchor_count", summary.get("spike_anchor_count")),
    ]
    table = "\n".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    imgs = []
    for label, name in [
        ("Lead crop", "lead_crop.png"),
        ("Mask heatmap", "mask_heatmap.png"),
        ("Mask binary", "mask_binary.png"),
        ("Clean mask", "mask_clean.png"),
        ("Skeleton", "skeleton.png"),
        ("All paths overlay", "overlay_all.png"),
    ]:
        if (out_dir / name).exists():
            imgs.append(f"<section><h2>{html.escape(label)}</h2><img src='{name}'></section>")
    body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Direct ECG Segment Debug Report</title>
  <style>
    body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f8fafc;color:#0f172a}}
    h1{{font-size:22px}} h2{{font-size:16px;margin:18px 0 8px}}
    table{{border-collapse:collapse;background:#fff;border:1px solid #cbd5e1;margin-bottom:16px}}
    th,td{{font-size:12px;text-align:left;border-bottom:1px solid #e2e8f0;padding:7px 10px;vertical-align:top}}
    th{{color:#475569;background:#f1f5f9;white-space:nowrap}}
    img{{max-width:100%;background:#fff;border:1px solid #cbd5e1;border-radius:6px}}
    code{{background:#e2e8f0;padding:1px 4px;border-radius:4px}}
  </style>
</head>
<body>
  <h1>Direct ECG Segment Debug Report</h1>
  <table>{table}</table>
  <p>Colors: orange=<code>model y_by_x</code>, blue=<code>old DP path</code>, purple=<code>skeleton trace</code>, teal=<code>non-QRS stabilized trace</code>, green=<code>selected guarded postprocess trace</code>.</p>
  {''.join(imgs)}
</body>
</html>
"""
    (out_dir / "report.html").write_text(body, encoding="utf-8")


def postprocess_segment_response(
    data: dict[str, Any],
    image_bytes: bytes,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """Apply the validated mask/skeleton/QRS pipeline to one model response.

    The upstream ``y_by_x`` is preserved as ``model_y_by_x`` for debugging.
    ``y_by_x`` and ``final_y_by_x`` both contain the guarded result so older
    and newer frontends behave consistently.
    """
    if not isinstance(data, dict):
        raise TypeError("ECG segment response must be a dictionary")
    lead_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = lead_img.size
    raw_y = normalize_y_by_x(data, w, h)
    mask = mask_rows_from_data(data)
    result = dict(data)
    result.setdefault("model_y_by_x", data.get("y_by_x"))

    if mask is None:
        if raw_y is not None:
            fallback = line_to_list(raw_y, limit=max(5000, w))
            result["y_by_x"] = fallback
            result["final_y_by_x"] = fallback
        result["width"] = w
        result["height"] = h
        result["_postprocess"] = {
            "applied": False,
            "method": "model_y_by_x_fallback",
            "reason": "model response did not contain a mask/probability image",
        }
        return result

    prob = resize_mask_to_prob(mask, w, h)
    binary = binary_from_prob(prob, threshold)
    clean_binary, components = clean_connected_components(binary)
    skeleton = skeletonize_zhang_suen(clean_binary)
    # DP is an expensive legacy/debug comparison. It is only needed as a
    # seed when the upstream model did not provide y_by_x.
    seed = raw_y if raw_y is not None else dp_trace_from_prob(prob)
    skeleton_y = skeleton_trace_by_x(skeleton, prob, seed)
    top, bottom, span = mask_column_edges_near_line(clean_binary, skeleton_y, h)
    qrs_windows = qrs_windows_from_model(data, w, h)
    if not qrs_windows:
        qrs_windows, _ = detect_qrs_windows(span, h)
    stable_y = stabilize_non_qrs_trace(skeleton_y, qrs_windows, h)
    top, bottom, span = mask_column_edges_near_line(clean_binary, stable_y, h)
    repaired_y, qrs_anchors = repair_spikes_from_mask(
        stable_y, top, bottom, span, h, qrs_windows=qrs_windows, image=lead_img
    )
    structured_y, structured_anchors, beats = ecg_structured_anchor_trace(
        clean_binary, repaired_y, top, bottom, qrs_windows, image=lead_img
    )
    final_y, method, candidate_metrics = select_postprocess_trace(
        repaired_y, structured_y, clean_binary, qrs_windows, h
    )
    selected_anchors = structured_anchors if method == "structured_anchor" else []
    anchors = qrs_anchors + selected_anchors
    quality = trace_quality(final_y, clean_binary, qrs_windows, anchors)
    final_values = line_to_list(final_y, limit=max(5000, w))

    result["width"] = w
    result["height"] = h
    result["y_by_x"] = final_values
    result["final_y_by_x"] = final_values
    result["qrs_windows"] = [[int(a), int(b)] for a, b in qrs_windows]
    result["beats"] = beats
    result["quality"] = quality
    result["_postprocess"] = {
        "applied": True,
        "method": method,
        "candidate_metrics": candidate_metrics,
        "component_count": len(components),
        "anchor_count": len(anchors),
    }
    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Directly call ECG segmentation model and generate debug artifacts.")
    ap.add_argument("image", help="Lead crop image, or a larger image when --crop is provided.")
    ap.add_argument("--endpoint", default=os.getenv("ECG_SEGMENT_ENDPOINT", "http://219.147.100.43:18018/api/ecg-segment"))
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--crop", help="Optional crop box: x1,y1,x2,y2")
    ap.add_argument("--out", default="work/ecg_segment_direct")
    ap.add_argument("--no-include-images", action="store_true", help="Do not request image artifacts from model.")
    args = ap.parse_args(argv)

    image_path = Path(args.image).expanduser().resolve()
    if not image_path.exists():
        raise SystemExit(f"image not found: {image_path}")
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    crop_box = parse_crop(args.crop)
    lead_img = load_crop(image_path, crop_box)
    lead_path = out_dir / "lead_crop.png"
    lead_img.save(lead_path)

    data = call_model(args.endpoint, lead_path, args.threshold, not args.no_include_images)
    (out_dir / "model_response.full.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "model_response.compact.json").write_text(json.dumps(compact_json(data), ensure_ascii=False, indent=2), encoding="utf-8")

    w, h = lead_img.size
    raw_y = normalize_y_by_x(data, w, h)
    mask = mask_rows_from_data(data)
    dp_y = None
    skeleton_y = None
    stable_y = None
    qrs_repair_y = None
    post_y = None
    anchors: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    qrs_windows: list[tuple[int, int]] = []
    beats: list[dict[str, int]] = []
    quality: dict[str, Any] = {}
    postprocess_method = "none"
    candidate_metrics: dict[str, dict[str, Any]] = {}
    clean_binary_for_quality: np.ndarray | None = None

    if mask is not None:
        save_heatmap(mask, out_dir / "mask_heatmap.png")
        prob = resize_mask_to_prob(mask, w, h)
        binary = binary_from_prob(prob, args.threshold)
        save_binary(binary, out_dir / "mask_binary.png")
        clean_binary, components = clean_connected_components(binary)
        clean_binary_for_quality = clean_binary
        save_binary(clean_binary, out_dir / "mask_clean.png")
        skeleton = skeletonize_zhang_suen(clean_binary)
        save_binary(skeleton, out_dir / "skeleton.png")
        dp_y = dp_trace_from_prob(prob)
        seed = raw_y if raw_y is not None else dp_y
        skeleton_y = skeleton_trace_by_x(skeleton, prob, seed)
        top, bottom, span = mask_column_edges_near_line(clean_binary, skeleton_y, h)
        qrs_windows = qrs_windows_from_model(data, w, h)
        if not qrs_windows:
            qrs_windows, _ = detect_qrs_windows(span, h)
        stable_y = stabilize_non_qrs_trace(skeleton_y, qrs_windows, h)
        top, bottom, span = mask_column_edges_near_line(clean_binary, stable_y, h)
        qrs_repair_y, qrs_anchors = repair_spikes_from_mask(stable_y, top, bottom, span, h, qrs_windows=qrs_windows, image=lead_img)
        structured_y, structured_anchors, beats = ecg_structured_anchor_trace(
            clean_binary, qrs_repair_y, top, bottom, qrs_windows, image=lead_img
        )
        post_y, postprocess_method, candidate_metrics = select_postprocess_trace(
            qrs_repair_y, structured_y, clean_binary, qrs_windows, h
        )
        selected_anchors = structured_anchors if postprocess_method == "structured_anchor" else []
        anchors = qrs_anchors + selected_anchors
        quality = trace_quality(post_y, clean_binary_for_quality, qrs_windows, anchors)
    else:
        print("warning: model response did not contain mask/prob image or array", file=sys.stderr)
        if raw_y is not None:
            post_y = raw_y.copy()
            postprocess_method = "model_y_by_x_fallback"
            quality = trace_quality(post_y, None, qrs_windows, anchors)

    traces = {}
    if raw_y is not None:
        traces["raw_y_by_x"] = raw_y
    if dp_y is not None:
        traces["dp_path"] = dp_y
    if skeleton_y is not None:
        traces["skeleton_trace"] = skeleton_y
    if stable_y is not None:
        traces["stable_trace"] = stable_y
    if post_y is not None:
        traces["post_spike"] = post_y
    draw_overlay(lead_img, traces, out_dir / "overlay_all.png", anchors)

    debug = {
        "endpoint": args.endpoint,
        "input": str(image_path),
        "crop": crop_box,
        "crop_size": [w, h],
        "threshold": args.threshold,
        "model_keys": list(data.keys()),
        "mask_shape": list(mask.shape) if mask is not None else None,
        "component_count": len(components),
        "components_top5": components[:5],
        "qrs_windows": [[int(a), int(b)] for a, b in qrs_windows],
        "beats": beats,
        "quality": quality,
        "postprocess_method": postprocess_method,
        "candidate_metrics": candidate_metrics,
        "raw_y_by_x": line_to_list(raw_y),
        "dp_path": line_to_list(dp_y),
        "skeleton_trace": line_to_list(skeleton_y),
        "stable_trace": line_to_list(stable_y),
        "qrs_repair_trace": line_to_list(qrs_repair_y),
        "post_spike": line_to_list(post_y),
        "spike_anchors": anchors,
    }
    (out_dir / "debug_artifacts.json").write_text(json.dumps(debug, ensure_ascii=False, indent=2), encoding="utf-8")
    structured_result = {
        "width": w,
        "height": h,
        "final_y_by_x": line_to_list(post_y, limit=max(5000, w)),
        "qrs_windows": [[int(a), int(b)] for a, b in qrs_windows],
        "beats": beats,
        "anchor_points": anchors,
        "quality": quality,
        "postprocess_method": postprocess_method,
        "candidate_metrics": candidate_metrics,
    }
    (out_dir / "structured_result.json").write_text(json.dumps(structured_result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        **debug,
        "raw_y_by_x_points": len(raw_y) if raw_y is not None else 0,
        "dp_points": len(dp_y) if dp_y is not None else 0,
        "skeleton_points": len(skeleton_y) if skeleton_y is not None else 0,
        "qrs_window_count": len(qrs_windows),
        "beat_count": len(beats),
        "spike_anchor_count": len(anchors),
    }
    write_report(out_dir, summary)
    print(f"debug report: {out_dir / 'report.html'}")
    print(f"artifacts: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
