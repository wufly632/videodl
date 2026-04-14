'''
Function:
    HTTP API wrapper for VideoClient
Author:
    Zhenchao Jin
WeChat Official Account (微信公众号):
    Charles的皮卡丘
'''
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import click
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .videodl import VideoClient
from .modules import VideoInfo
from .__init__ import __version__


DATA_FILE = Path(__file__).resolve().parents[1] / "videodl_api_data.json"
DATA_LOCK = Lock()
MAX_HISTORY_ITEMS = 500


class ClientOptions(BaseModel):
    allowed_video_sources: list[str] = Field(default_factory=list)
    init_video_clients_cfg: dict[str, dict[str, Any]] = Field(default_factory=dict)
    clients_threadings: dict[str, int] = Field(default_factory=dict)
    requests_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    apply_common_video_clients_only: bool = False


class ParseRequest(ClientOptions):
    url: str


class DownloadRequest(ClientOptions):
    video_infos: list[dict[str, Any]]


class ParseAndDownloadRequest(ParseRequest):
    pass


class MPParseRequest(ClientOptions):
    text: str | None = None
    url: str | None = None


class MPDownloadRequest(BaseModel):
    video_url: str | None = None
    video_id: str | None = None


class MPRefreshRequest(ClientOptions):
    text: str | None = None
    url: str | None = None
    video_url: str | None = None
    video_id: str | None = None
    platform: str | None = None


def _build_client(options: ClientOptions) -> VideoClient:
    return VideoClient(
        allowed_video_sources=options.allowed_video_sources,
        init_video_clients_cfg=options.init_video_clients_cfg,
        clients_threadings=options.clients_threadings,
        requests_overrides=options.requests_overrides,
        apply_common_video_clients_only=options.apply_common_video_clients_only,
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _serialize_video_info(video_info: VideoInfo) -> dict[str, Any]:
    data = video_info.todict() if isinstance(video_info, VideoInfo) else dict(video_info)
    data["with_valid_download_url"] = bool(video_info.with_valid_download_url)
    data["with_valid_audio_download_url"] = bool(video_info.with_valid_audio_download_url)
    return _json_safe(data)


def _legacy_success(data: Any = None, retdesc: str = "success", succ: bool = True) -> dict[str, Any]:
    return {
        "retcode": 200,
        "retdesc": retdesc,
        "msg": retdesc,
        "succ": succ,
        "data": _json_safe(data),
    }


def _legacy_failure(
    retdesc: str,
    data: Any = None,
    *,
    succ: bool = False,
    ) -> dict[str, Any]:
    return _legacy_success(data=data, retdesc=retdesc, succ=succ)


def _load_data() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {"history": [], "total_parse_count": 0}
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"history": [], "total_parse_count": 0}


def _save_data(data: dict[str, Any]) -> None:
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _format_display_time(timestamp_ms: int) -> str:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def _build_record_from_video_info(video_info: VideoInfo) -> dict[str, Any]:
    created_at = _now_ms()
    video_id = video_info.identifier or video_info.title or str(created_at)
    platform = _format_platform_name(video_info.source) or "未知来源"
    return {
        "id": f"{video_id}:{created_at}",
        "video_id": str(video_id),
        "title": video_info.title or "未命名素材",
        "subtitle": f"来自 {platform} 的真实解析记录",
        "platform": platform,
        "cover_url": video_info.cover_url or "",
        "video_url": video_info.download_url or "",
        "source": video_info.source or platform,
        "created_at": created_at,
        "display_date": _format_display_time(created_at),
    }


def _append_history_record(video_info: VideoInfo) -> None:
    with DATA_LOCK:
        data = _load_data()
        history = data.get("history", [])
        history.insert(0, _build_record_from_video_info(video_info))
        data["history"] = history[:MAX_HISTORY_ITEMS]
        data["total_parse_count"] = int(data.get("total_parse_count", 0)) + 1
        _save_data(data)


def _match_period(timestamp_ms: int, period: str | None) -> bool:
    if not period or period == "all":
        return True

    now = _now_ms()
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone()
    today = datetime.now(tz=timezone.utc).astimezone()

    if period == "today":
      return dt.date() == today.date()

    if period == "yesterday":
      return (today.date() - dt.date()).days == 1

    try:
        days = int(period)
    except Exception:
        return True
    return now - timestamp_ms <= days * 24 * 60 * 60 * 1000


def _filter_history_items(
    history: list[dict[str, Any]],
    *,
    search: str = "",
    period: str = "all",
) -> list[dict[str, Any]]:
    keyword = (search or "").strip().lower()
    filtered = []
    for item in history:
        if not _match_period(int(item.get("created_at", 0) or 0), period):
            continue
        if keyword:
            haystack = " ".join([
                str(item.get("title", "")),
                str(item.get("subtitle", "")),
                str(item.get("platform", "")),
                str(item.get("source", "")),
            ]).lower()
            if keyword not in haystack:
                continue
        filtered.append(item)
    return filtered


def _normalize_history_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "video_id": item.get("video_id"),
        "title": item.get("title"),
        "subtitle": item.get("subtitle"),
        "platform": item.get("platform"),
        "cover_url": item.get("cover_url"),
        "video_url": item.get("video_url"),
        "source": item.get("source"),
        "created_at": item.get("created_at"),
        "display_date": item.get("display_date") or _format_display_time(int(item.get("created_at", 0) or 0)),
    }


def _build_ranking_items(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(dict)
    counts: dict[str, int] = defaultdict(int)
    latest_times: dict[str, int] = defaultdict(int)

    for item in history:
        key = str(item.get("video_id") or item.get("video_url") or item.get("title") or item.get("id"))
        counts[key] += 1
        created_at = int(item.get("created_at", 0) or 0)
        if created_at >= latest_times[key]:
            latest_times[key] = created_at
            grouped[key] = item

    ranking = []
    now = _now_ms()
    for key, count in counts.items():
        item = grouped[key]
        last_at = latest_times[key]
        age_days = max(0, int((now - last_at) / (24 * 60 * 60 * 1000)))
        ranking.append({
            "id": key,
            "title": item.get("title") or "未命名素材",
            "summary": f"近期开启解析 {count} 次，最近一次于 {item.get('display_date') or _format_display_time(last_at)}",
            "hot": count,
            "source": item.get("platform") or item.get("source") or "未知来源",
            "cover_url": item.get("cover_url") or "",
            "video_url": item.get("video_url") or "",
            "cover_title": item.get("title") or "未命名素材",
            "last_created_at": last_at,
            "age_days": age_days,
        })

    ranking.sort(key=lambda item: (-int(item["hot"]), -int(item["last_created_at"])))
    for index, item in enumerate(ranking, start=1):
        item["rank"] = index
    return ranking


def _null_mp_video_data() -> dict[str, Any]:
    return {
        "video_url": None,
        "title": None,
        "cover_url": None,
        "video_id": None,
        "platform": None,
        "source": None,
        "heat": 0,
    }


def _extract_first_url(text: str | None) -> str:
    if not text:
        return ""
    text = text.strip()
    if text.startswith(("http://", "https://")):
        return text
    matched = re.search(r"https?://[^\s]+", text)
    return matched.group(0).strip().rstrip(").,;\"'") if matched else ""


def _pick_video_info(video_infos: list[VideoInfo]) -> VideoInfo | None:
    if not video_infos:
        return None
    for video_info in video_infos:
        if video_info.with_valid_download_url:
            return video_info
    return video_infos[0]


def _format_platform_name(source: Any) -> str | None:
    if not source:
        return None
    source = str(source)
    return source[:-11] if source.endswith("VideoClient") else source


def _to_mp_video_data(video_info: VideoInfo | None) -> dict[str, Any]:
    if video_info is None:
        return _null_mp_video_data()
    return {
        "video_url": video_info.download_url or None,
        "title": video_info.title or None,
        "cover_url": video_info.cover_url or None,
        "video_id": video_info.identifier or video_info.title or None,
        "platform": _format_platform_name(video_info.source),
        "source": video_info.source or None,
        "heat": 0,
    }


def _parse_to_mp_result(request: MPParseRequest | MPRefreshRequest) -> dict[str, Any]:
    url = _extract_first_url(request.url or request.text or request.video_url)
    if not url:
        return _legacy_failure("未提供可解析的链接", _null_mp_video_data())
    try:
        client = _build_client(request)
        video_infos = client.parsefromurl(url=url)
        video_info = _pick_video_info(video_infos)
        if video_info is None or not video_info.with_valid_download_url:
            return _legacy_failure("未解析到可用视频信息", _to_mp_video_data(video_info))
        _append_history_record(video_info)
        return _legacy_success(_to_mp_video_data(video_info), retdesc="解析成功")
    except Exception as exc:
        return _legacy_failure(f"解析失败: {exc}", _null_mp_video_data())


app = FastAPI(
    title="videodl API",
    version=__version__,
    description="HTTP wrapper around videodl.VideoClient for parsing and downloading videos.",
)


@app.get("/")
def index() -> dict[str, Any]:
    return {
        "name": "videodl-api",
        "version": __version__,
        "docs": "/docs",
        "endpoints": [
            "/health",
            "/parse",
            "/download",
            "/parse-and-download",
            "/api/parse",
            "/api/download",
            "/api/refresh_video",
            "/api/history",
            "/api/history/clear",
            "/api/ranking",
            "/api/stats",
        ],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@app.post("/parse")
def parse_video(request: ParseRequest) -> dict[str, Any]:
    try:
        client = _build_client(request)
        video_infos = client.parsefromurl(url=request.url)
        return {
            "ok": True,
            "count": len(video_infos),
            "video_infos": [_serialize_video_info(video_info) for video_info in video_infos],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"parse failed: {exc}") from exc


@app.post("/download")
def download_video(request: DownloadRequest) -> dict[str, Any]:
    try:
        client = _build_client(request)
        video_infos = [VideoInfo.fromdict(item) for item in request.video_infos]
        downloaded_video_infos = client.download(video_infos=video_infos)
        return {
            "ok": True,
            "count": len(downloaded_video_infos),
            "video_infos": [_serialize_video_info(video_info) for video_info in downloaded_video_infos],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"download failed: {exc}") from exc


@app.post("/parse-and-download")
def parse_and_download_video(request: ParseAndDownloadRequest) -> dict[str, Any]:
    try:
        client = _build_client(request)
        parsed_video_infos = client.parsefromurl(url=request.url)
        downloaded_video_infos = client.download(video_infos=parsed_video_infos)
        return {
            "ok": True,
            "parsed_count": len(parsed_video_infos),
            "downloaded_count": len(downloaded_video_infos),
            "video_infos": [_serialize_video_info(video_info) for video_info in downloaded_video_infos],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"parse-and-download failed: {exc}") from exc


@app.post("/api/parse")
def mp_parse_video(request: MPParseRequest) -> dict[str, Any]:
    return _parse_to_mp_result(request)


@app.post("/api/download")
def mp_download_video(request: MPDownloadRequest) -> dict[str, Any]:
    video_url = (request.video_url or "").strip()
    if not video_url:
        return _legacy_failure("缺少 video_url", {"download_url": None})
    return _legacy_success(
        {
            "download_url": video_url,
            "video_id": request.video_id or None,
        },
        retdesc="获取下载地址成功",
    )


@app.post("/api/refresh_video")
def mp_refresh_video(request: MPRefreshRequest) -> dict[str, Any]:
    return _parse_to_mp_result(request)


@app.get("/api/history")
def mp_history(search: str = "", period: str = "all", limit: int = 100) -> dict[str, Any]:
    with DATA_LOCK:
        data = _load_data()
    history = [_normalize_history_item(item) for item in data.get("history", [])]
    filtered = _filter_history_items(history, search=search, period=period)
    return _legacy_success(
        {
            "items": filtered[: max(0, limit)],
            "total_count": int(data.get("total_parse_count", 0)),
            "history_count": len(history),
        },
        retdesc="获取历史记录成功",
    )


@app.post("/api/history/clear")
def mp_clear_history() -> dict[str, Any]:
    with DATA_LOCK:
        data = _load_data()
        data["history"] = []
        _save_data(data)
    return _legacy_success({"cleared": True}, retdesc="已清空历史记录")


@app.get("/api/ranking")
def mp_ranking(search: str = "", period: str = "7", limit: int = 50) -> dict[str, Any]:
    with DATA_LOCK:
        data = _load_data()
    history = [_normalize_history_item(item) for item in data.get("history", [])]
    filtered = _filter_history_items(history, search=search, period=period)
    ranking = _build_ranking_items(filtered)
    return _legacy_success(
        {
            "items": ranking[: max(0, limit)],
            "total_count": int(data.get("total_parse_count", 0)),
        },
        retdesc="获取热门榜单成功",
    )


@app.get("/api/stats")
def mp_stats() -> dict[str, Any]:
    with DATA_LOCK:
        data = _load_data()
    history = data.get("history", [])
    return _legacy_success(
        {
            "total_count": int(data.get("total_parse_count", 0)),
            "history_count": len(history),
        },
        retdesc="获取统计信息成功",
    )


@click.command()
@click.option("--host", default="127.0.0.1", type=str, show_default=True, help="Host to bind the API server.")
@click.option("--port", default=8000, type=int, show_default=True, help="Port to bind the API server.")
@click.option("--reload", is_flag=True, default=False, help="Enable auto reload for development.")
def VideoClientAPICMD(host: str, port: int, reload: bool):
    uvicorn.run("videodl.api_server:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    VideoClientAPICMD()
