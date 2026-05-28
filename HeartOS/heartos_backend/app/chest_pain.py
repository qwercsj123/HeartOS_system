from __future__ import annotations

import re
from typing import Any

import httpx
from fastapi import HTTPException


PREDICT_FIELD_NAME = "file"

CLASS_NAME_ZH: dict[int, str] = {
    0: "不稳定性心绞痛",
    1: "非ST段抬高型心肌梗死",
    2: "ST段抬高型心肌梗死",
    3: "主动脉夹层",
    4: "肺栓塞",
    5: "其他类型",
}

CLASS_NAME_TO_ID = {v: k for k, v in CLASS_NAME_ZH.items()}

THRESHOLDS: dict[int, dict[str, Any]] = {
    2: {
        "class_name": "STEMI",
        "low_threshold": -2.550016697179391,
        "high_threshold": 2.7442480987923137,
    },
    3: {
        "class_name": "AD",
        "low_threshold": 0.588042290842758,
        "high_threshold": 8.174535874920526,
    },
    4: {
        "class_name": "PE",
        "low_threshold": 2.8199825027504763,
        "high_threshold": 10.796660571970655,
    },
}

HIGH_RISK_NAME_ZH = {
    2: "ST段抬高型心肌梗死",
    3: "主动脉夹层",
    4: "肺栓塞",
}


async def call_predict_text_api(
    *,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    url: str,
    timeout_seconds: int,
) -> str:
    if not url:
        raise HTTPException(status_code=500, detail="未配置胸痛模型地址 APP_CHEST_PAIN_PREDICT_URL")
    timeout = httpx.Timeout(timeout_seconds)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    try:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            resp = await client.post(
                url,
                files={PREDICT_FIELD_NAME: (filename, file_bytes, content_type or "application/octet-stream")},
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"胸痛模型调用失败: {exc!r}") from exc

    if resp.status_code >= 400:
        detail = (resp.text or f"HTTP {resp.status_code}")[:1200]
        raise HTTPException(status_code=resp.status_code, detail=f"胸痛模型返回错误: {detail}")

    txt = (resp.text or "").strip()
    if not txt:
        raise HTTPException(status_code=502, detail="胸痛模型调用成功，但返回内容为空")
    return txt


def parse_predict_text(txt: str) -> dict[int, float]:
    scores: dict[int, float] = {}
    for zh_name, class_id in CLASS_NAME_TO_ID.items():
        pattern = rf"{re.escape(zh_name)}[:：]\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
        match = re.search(pattern, txt)
        if match:
            scores[class_id] = float(match.group(1))

    missing = [k for k in range(6) if k not in scores]
    if missing:
        raise HTTPException(status_code=502, detail=f"胸痛模型解析失败，缺少类别分数: {missing}")
    return scores


def classify_high_low_risk(scores: dict[int, float]) -> tuple[list[str], list[str], dict[int, str]]:
    high_risk: list[str] = []
    low_risk: list[str] = []
    risk_map: dict[int, str] = {}

    for cid in (2, 3, 4):
        score = scores[cid]
        low_thr = float(THRESHOLDS[cid]["low_threshold"])
        high_thr = float(THRESHOLDS[cid]["high_threshold"])
        if score >= high_thr:
            risk_map[cid] = "high"
            high_risk.append(HIGH_RISK_NAME_ZH[cid])
        elif score < low_thr:
            risk_map[cid] = "low"
            low_risk.append(HIGH_RISK_NAME_ZH[cid])
        else:
            risk_map[cid] = "mid"

    return high_risk, low_risk, risk_map


def rank_diseases_by_score(scores: dict[int, float]) -> list[tuple[int, float]]:
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def rank_diseases_optimized(scores: dict[int, float], risk_map: dict[int, str]) -> list[tuple[int, float]]:
    highrisk_high: list[tuple[int, float]] = []
    middle: list[tuple[int, float]] = []
    highrisk_low: list[tuple[int, float]] = []

    for cid, score in scores.items():
        if cid in (2, 3, 4):
            risk = risk_map.get(cid)
            if risk == "high":
                highrisk_high.append((cid, score))
            elif risk == "low":
                highrisk_low.append((cid, score))
            else:
                middle.append((cid, score))
        else:
            middle.append((cid, score))

    highrisk_high.sort(key=lambda item: item[1], reverse=True)
    middle.sort(key=lambda item: item[1], reverse=True)
    highrisk_low.sort(key=lambda item: item[1], reverse=True)
    return highrisk_high + middle + highrisk_low


def format_result(
    *,
    high_risk: list[str],
    low_risk: list[str],
    ranking: list[tuple[int, float]],
    show_score: bool = True,
) -> str:
    high_text = "、".join(high_risk) if high_risk else "无"
    low_text = "、".join(low_risk) if low_risk else "无"
    if show_score:
        rank_text = " > ".join(f"{CLASS_NAME_ZH[cid]}({score:.4f})" for cid, score in ranking)
    else:
        rank_text = " > ".join(CLASS_NAME_ZH[cid] for cid, _ in ranking)
    return (
        "###\n"
        f"高风险：{high_text}\n"
        f"低风险：{low_text}\n"
        f"疾病可能性排序：{rank_text}\n"
        "###"
    )


async def predict_image_and_report(
    *,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    url: str,
    timeout_seconds: int,
    use_optimized_rank: bool = True,
    show_score: bool = True,
) -> dict[str, Any]:
    raw_text = await call_predict_text_api(
        file_bytes=file_bytes,
        filename=filename,
        content_type=content_type,
        url=url,
        timeout_seconds=timeout_seconds,
    )
    scores = parse_predict_text(raw_text)
    high_risk, low_risk, risk_map = classify_high_low_risk(scores)
    ranking = rank_diseases_optimized(scores, risk_map) if use_optimized_rank else rank_diseases_by_score(scores)
    report = format_result(high_risk=high_risk, low_risk=low_risk, ranking=ranking, show_score=show_score)
    return {
        "raw_text": raw_text,
        "scores": {str(cid): score for cid, score in scores.items()},
        "labels": {str(cid): CLASS_NAME_ZH[cid] for cid in CLASS_NAME_ZH},
        "high_risk": high_risk,
        "low_risk": low_risk,
        "risk_map": {str(cid): level for cid, level in risk_map.items()},
        "ranking": [
            {"class_id": cid, "class_name": CLASS_NAME_ZH[cid], "score": score}
            for cid, score in ranking
        ],
        "report": report,
    }
