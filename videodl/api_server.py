'''
Function:
    HTTP API wrapper for VideoClient
Author:
    Zhenchao Jin
WeChat Official Account (微信公众号):
    Charles的皮卡丘
'''
import json
import mimetypes
import os
import re
import sqlite3
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote, urlparse

import click
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .videodl import VideoClient
from .modules import VideoInfo, legalizestring
from .__init__ import __version__


DB_FILE = Path(__file__).resolve().parents[1] / "videodl_api.db"
LEGACY_DATA_FILE = Path(__file__).resolve().parents[1] / "videodl_api_data.json"
DATA_LOCK = Lock()
MAX_HISTORY_ITEMS = 500
DOWNLOAD_TMP_DIR = Path(tempfile.gettempdir()) / "videodl_mp_downloads"


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
    user_key: str | None = None


class MPDownloadRequest(BaseModel):
    video_url: str | None = None
    video_id: str | None = None
    user_key: str | None = None


class MPRefreshRequest(ClientOptions):
    text: str | None = None
    url: str | None = None
    video_url: str | None = None
    video_id: str | None = None
    platform: str | None = None
    user_key: str | None = None


class MPHistoryClearRequest(BaseModel):
    user_key: str | None = None


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


def _get_db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def _storage_settings() -> dict[str, Any]:
    return {
        "bucket": os.getenv("OBJECT_STORAGE_BUCKET", "").strip(),
        "region": os.getenv("OBJECT_STORAGE_REGION", "").strip() or None,
        "endpoint_url": os.getenv("OBJECT_STORAGE_ENDPOINT_URL", "").strip() or None,
        "access_key_id": os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID", "").strip(),
        "secret_access_key": os.getenv("OBJECT_STORAGE_SECRET_ACCESS_KEY", "").strip(),
        "public_base_url": os.getenv("OBJECT_STORAGE_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        "prefix": os.getenv("OBJECT_STORAGE_PREFIX", "miniapp-downloads").strip().strip("/") or "miniapp-downloads",
        "signed_url_expires": max(60, int(os.getenv("OBJECT_STORAGE_SIGNED_URL_EXPIRES", "604800"))),
        "addressing_style": os.getenv("OBJECT_STORAGE_ADDRESSING_STYLE", "auto").strip() or "auto",
    }


def _storage_ready() -> bool:
    settings = _storage_settings()
    return bool(settings["bucket"] and settings["access_key_id"] and settings["secret_access_key"])


def _create_s3_client():
    settings = _storage_settings()
    from boto3.session import Session
    from botocore.config import Config

    session = Session(
        aws_access_key_id=settings["access_key_id"],
        aws_secret_access_key=settings["secret_access_key"],
        region_name=settings["region"],
    )
    return session.client(
        "s3",
        endpoint_url=settings["endpoint_url"],
        config=Config(signature_version="s3v4", s3={"addressing_style": settings["addressing_style"]}),
    )


def _guess_ext_from_url(url: str) -> str:
    path = urlparse(url or "").path
    ext = Path(path).suffix.lstrip(".").lower()
    return ext or "mp4"


def _safe_video_stem(value: str | None) -> str:
    safe = legalizestring((value or "").strip()) if value else ""
    safe = re.sub(r"\s+", "-", safe).strip("._-")
    return (safe[:80] or "video")


def _ensure_history_columns(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(parse_history)").fetchall()}
    required_columns = {
        "user_key": "TEXT",
        "share_url": "TEXT",
        "video_info_json": "TEXT",
        "object_url": "TEXT",
        "object_key": "TEXT",
        "mirrored_at": "INTEGER",
    }
    for column_name, column_type in required_columns.items():
        if column_name not in columns:
            connection.execute(f"ALTER TABLE parse_history ADD COLUMN {column_name} {column_type}")


def _init_db() -> None:
    with _get_db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS parse_history (
                id TEXT PRIMARY KEY,
                user_key TEXT,
                video_id TEXT,
                title TEXT NOT NULL,
                subtitle TEXT,
                platform TEXT,
                cover_url TEXT,
                video_url TEXT,
                share_url TEXT,
                video_info_json TEXT,
                object_url TEXT,
                object_key TEXT,
                mirrored_at INTEGER,
                source TEXT,
                created_at INTEGER NOT NULL,
                display_date TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS parse_stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_parse_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS parse_user_stats (
                user_key TEXT PRIMARY KEY,
                total_parse_count INTEGER NOT NULL DEFAULT 0
            );

            INSERT OR IGNORE INTO parse_stats (id, total_parse_count)
            VALUES (1, 0);
            """
        )
        _ensure_history_columns(connection)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_parse_history_user_key_created_at ON parse_history(user_key, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_parse_history_video_id_created_at ON parse_history(video_id, created_at DESC)"
        )


def _load_legacy_data() -> dict[str, Any]:
    if not LEGACY_DATA_FILE.exists():
        return {"history": [], "total_parse_count": 0}
    try:
        return json.loads(LEGACY_DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"history": [], "total_parse_count": 0}


def _migrate_legacy_json_if_needed() -> None:
    if not LEGACY_DATA_FILE.exists():
        return

    with _get_db_connection() as connection:
        current_count = connection.execute("SELECT COUNT(*) FROM parse_history").fetchone()[0]
        if current_count > 0:
            return

        legacy = _load_legacy_data()
        history = legacy.get("history", []) or []
        total_count = int(legacy.get("total_parse_count", 0) or 0)
        if not history and total_count <= 0:
            return

        for item in history:
            connection.execute(
                """
                INSERT OR REPLACE INTO parse_history
                (id, user_key, video_id, title, subtitle, platform, cover_url, video_url, share_url, video_info_json, object_url, object_key, mirrored_at, source, created_at, display_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.get("id"),
                    item.get("user_key"),
                    item.get("video_id"),
                    item.get("title") or "未命名素材",
                    item.get("subtitle"),
                    item.get("platform"),
                    item.get("cover_url"),
                    item.get("video_url"),
                    item.get("share_url"),
                    item.get("video_info_json"),
                    item.get("object_url"),
                    item.get("object_key"),
                    item.get("mirrored_at"),
                    item.get("source"),
                    int(item.get("created_at", 0) or 0),
                    item.get("display_date") or _format_display_time(int(item.get("created_at", 0) or 0)),
                ),
            )

        connection.execute(
            "UPDATE parse_stats SET total_parse_count = ? WHERE id = 1",
            (max(total_count, len(history)),),
        )
        connection.commit()


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _format_display_time(timestamp_ms: int) -> str:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def _json_dumps(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False)


def _json_loads_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        data = json.loads(str(value))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _build_record_from_video_info(
    video_info: VideoInfo,
    user_key: str | None = None,
    share_url: str | None = None,
) -> dict[str, Any]:
    created_at = _now_ms()
    video_id = video_info.identifier or video_info.title or str(created_at)
    platform = _format_platform_name(video_info.source) or "未知来源"
    return {
        "id": f"{video_id}:{created_at}",
        "user_key": (user_key or "").strip() or None,
        "video_id": str(video_id),
        "title": video_info.title or "未命名素材",
        "subtitle": f"来自 {platform} 的真实解析记录",
        "platform": platform,
        "cover_url": video_info.cover_url or "",
        "video_url": video_info.download_url or "",
        "share_url": (share_url or "").strip() or None,
        "video_info_json": _json_dumps(_serialize_video_info(video_info)),
        "object_url": None,
        "object_key": None,
        "mirrored_at": None,
        "source": video_info.source or platform,
        "created_at": created_at,
        "display_date": _format_display_time(created_at),
    }


def _append_history_record(
    video_info: VideoInfo,
    user_key: str | None = None,
    share_url: str | None = None,
) -> dict[str, Any]:
    record = _build_record_from_video_info(video_info, user_key=user_key, share_url=share_url)
    with DATA_LOCK:
        with _get_db_connection() as connection:
            connection.execute(
                """
                INSERT INTO parse_history
                (id, user_key, video_id, title, subtitle, platform, cover_url, video_url, share_url, video_info_json, object_url, object_key, mirrored_at, source, created_at, display_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["user_key"],
                    record["video_id"],
                    record["title"],
                    record["subtitle"],
                    record["platform"],
                    record["cover_url"],
                    record["video_url"],
                    record["share_url"],
                    record["video_info_json"],
                    record["object_url"],
                    record["object_key"],
                    record["mirrored_at"],
                    record["source"],
                    record["created_at"],
                    record["display_date"],
                ),
            )
            connection.execute(
                "UPDATE parse_stats SET total_parse_count = total_parse_count + 1 WHERE id = 1"
            )
            if record["user_key"]:
                connection.execute(
                    """
                    INSERT INTO parse_user_stats (user_key, total_parse_count)
                    VALUES (?, 1)
                    ON CONFLICT(user_key) DO UPDATE SET total_parse_count = total_parse_count + 1
                    """,
                    (record["user_key"],),
                )
            connection.execute(
                """
                DELETE FROM parse_history
                WHERE id IN (
                    SELECT id FROM parse_history
                    ORDER BY created_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (MAX_HISTORY_ITEMS,),
            )
            connection.commit()
    return record


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
        "user_key": item.get("user_key"),
        "video_id": item.get("video_id"),
        "title": item.get("title"),
        "subtitle": item.get("subtitle"),
        "platform": item.get("platform"),
        "cover_url": item.get("cover_url"),
        "video_url": item.get("video_url"),
        "share_url": item.get("share_url"),
        "video_info_json": item.get("video_info_json"),
        "object_url": item.get("object_url"),
        "object_key": item.get("object_key"),
        "mirrored_at": item.get("mirrored_at"),
        "source": item.get("source"),
        "created_at": item.get("created_at"),
        "display_date": item.get("display_date") or _format_display_time(int(item.get("created_at", 0) or 0)),
    }


def _get_total_parse_count(user_key: str | None = None) -> int:
    normalized_user_key = (user_key or "").strip()
    with _get_db_connection() as connection:
        if normalized_user_key:
            row = connection.execute(
                "SELECT total_parse_count FROM parse_user_stats WHERE user_key = ?",
                (normalized_user_key,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT total_parse_count FROM parse_stats WHERE id = 1"
            ).fetchone()
    return int(row["total_parse_count"] if row else 0)


def _list_history_items(limit: int | None = None, user_key: str | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT id, user_key, video_id, title, subtitle, platform, cover_url, video_url, share_url, video_info_json, object_url, object_key, mirrored_at, source, created_at, display_date
        FROM parse_history
    """
    params: list[Any] = []
    normalized_user_key = (user_key or "").strip()
    if normalized_user_key:
        sql += " WHERE user_key = ?"
        params.append(normalized_user_key)
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with _get_db_connection() as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [_normalize_history_item(dict(row)) for row in rows]


def _clear_history_items(user_key: str | None = None) -> None:
    normalized_user_key = (user_key or "").strip()
    with DATA_LOCK:
        with _get_db_connection() as connection:
            if normalized_user_key:
                connection.execute("DELETE FROM parse_history WHERE user_key = ?", (normalized_user_key,))
            else:
                connection.execute("DELETE FROM parse_history")
            connection.commit()


def _find_history_record(video_identifier: str | None = None, video_url: str | None = None, user_key: str | None = None) -> dict[str, Any] | None:
    normalized_identifier = (video_identifier or "").strip()
    normalized_video_url = (video_url or "").strip()
    normalized_user_key = (user_key or "").strip()
    clauses: list[str] = []
    params: list[Any] = []

    if normalized_identifier:
        clauses.append("(id = ? OR video_id = ?)")
        params.extend([normalized_identifier, normalized_identifier])
    if normalized_video_url:
        clauses.append("video_url = ?")
        params.append(normalized_video_url)
    if not clauses:
        return None

    sql = """
        SELECT id, user_key, video_id, title, subtitle, platform, cover_url, video_url, share_url, video_info_json, object_url, object_key, mirrored_at, source, created_at, display_date
        FROM parse_history
        WHERE ({conditions})
    """.format(conditions=" OR ".join(clauses))
    if normalized_user_key:
        sql += " AND user_key = ?"
        params.append(normalized_user_key)
    sql += " ORDER BY created_at DESC LIMIT 1"

    with _get_db_connection() as connection:
        row = connection.execute(sql, tuple(params)).fetchone()
    return _normalize_history_item(dict(row)) if row else None


def _build_temp_video_info(record: dict[str, Any]) -> VideoInfo:
    stored_video_info = _json_loads_dict(record.get("video_info_json"))
    if stored_video_info:
        video_info = VideoInfo.fromdict(stored_video_info)
    else:
        fallback_url = str(record.get("video_url") or "").strip()
        if not fallback_url:
            raise RuntimeError("未找到可用的视频下载地址")
        video_info = VideoInfo(
            source=record.get("source"),
            title=record.get("title") or "",
            cover_url=record.get("cover_url") or "",
            identifier=record.get("video_id") or record.get("id") or "",
            download_url=fallback_url,
            ext=_guess_ext_from_url(fallback_url),
        )

    ext = (video_info.ext or "").strip().lower() or _guess_ext_from_url(str(video_info.download_url or ""))
    file_stem = _safe_video_stem(video_info.title or video_info.identifier or record.get("id"))
    file_suffix = f".{ext or 'mp4'}"
    DOWNLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    video_info.ext = ext or "mp4"
    video_info.save_path = str(DOWNLOAD_TMP_DIR / f"{file_stem}-{uuid.uuid4().hex[:12]}{file_suffix}")

    if video_info.with_valid_audio_download_url:
        audio_ext = (video_info.audio_ext or "m4a").strip().lower() or "m4a"
        video_info.audio_save_path = str(DOWNLOAD_TMP_DIR / f"{file_stem}-{uuid.uuid4().hex[:12]}-audio.{audio_ext}")
    else:
        video_info.audio_save_path = ""

    return video_info


def _mirror_video_to_object_storage(record: dict[str, Any]) -> tuple[str, str]:
    if not _storage_ready():
        raise RuntimeError("对象存储未配置，请先配置 OBJECT_STORAGE_* 环境变量")

    video_info = _build_temp_video_info(record)
    client = _build_client(ClientOptions())
    downloaded_video_infos = client.download(video_infos=[video_info])
    if not downloaded_video_infos:
        raise RuntimeError("服务端下载视频失败")

    downloaded_video_info = downloaded_video_infos[0]
    local_path = Path(downloaded_video_info.save_path or video_info.save_path)
    if not local_path.exists():
        raise RuntimeError("服务端未生成可上传的视频文件")

    settings = _storage_settings()
    object_key = "/".join([
        settings["prefix"],
        datetime.now(tz=timezone.utc).strftime("%Y/%m/%d"),
        f"{_safe_video_stem(downloaded_video_info.title or downloaded_video_info.identifier or record.get('video_id'))}-{uuid.uuid4().hex[:12]}{local_path.suffix or '.mp4'}",
    ])
    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    s3_client = _create_s3_client()

    try:
        s3_client.upload_file(
            str(local_path),
            settings["bucket"],
            object_key,
            ExtraArgs={"ContentType": content_type},
        )
    finally:
        local_path.exists() and local_path.unlink()
        audio_save_path = (downloaded_video_info.audio_save_path or video_info.audio_save_path or "").strip()
        audio_path = Path(audio_save_path) if audio_save_path else None
        if audio_path and audio_path.exists():
            audio_path.unlink()

    if settings["public_base_url"]:
        object_url = f"{settings['public_base_url']}/{quote(object_key, safe='/')}"
    else:
        object_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings["bucket"], "Key": object_key},
            ExpiresIn=settings["signed_url_expires"],
        )

    return object_url, object_key


def _persist_mirrored_object(record_id: str, object_url: str, object_key: str) -> None:
    mirrored_at = _now_ms()
    with DATA_LOCK:
        with _get_db_connection() as connection:
            connection.execute(
                """
                UPDATE parse_history
                SET object_url = ?, object_key = ?, mirrored_at = ?, video_url = ?
                WHERE id = ?
                """,
                (object_url, object_key, mirrored_at, object_url, record_id),
            )
            connection.commit()


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
        "history_id": None,
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
        "history_id": None,
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
        record = _append_history_record(video_info, user_key=request.user_key, share_url=url)
        response_data = _to_mp_video_data(video_info)
        response_data["history_id"] = record["id"]
        return _legacy_success(response_data, retdesc="解析成功")
    except Exception as exc:
        return _legacy_failure(f"解析失败: {exc}", _null_mp_video_data())


app = FastAPI(
    title="videodl API",
    version=__version__,
    description="HTTP wrapper around videodl.VideoClient for parsing and downloading videos.",
)

_init_db()
_migrate_legacy_json_if_needed()


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
    if not _storage_ready():
        return _legacy_failure("对象存储未配置，请先配置 OBJECT_STORAGE_* 环境变量", {"download_url": None})

    record = _find_history_record(
        video_identifier=request.video_id,
        video_url=request.video_url,
        user_key=request.user_key,
    )
    if record is None:
        return _legacy_failure("未找到可下载的视频记录，请重新解析后再试", {"download_url": None})

    if record.get("object_url"):
        return _legacy_success(
            {
                "download_url": record["object_url"],
                "video_id": record.get("video_id") or request.video_id or None,
                "history_id": record["id"],
            },
            retdesc="获取下载地址成功",
        )

    try:
        object_url, object_key = _mirror_video_to_object_storage(record)
        _persist_mirrored_object(record["id"], object_url, object_key)
        return _legacy_success(
            {
                "download_url": object_url,
                "video_id": record.get("video_id") or request.video_id or None,
                "history_id": record["id"],
            },
            retdesc="获取下载地址成功",
        )
    except Exception as exc:
        return _legacy_failure(f"生成下载地址失败: {exc}", {"download_url": None})


@app.post("/api/refresh_video")
def mp_refresh_video(request: MPRefreshRequest) -> dict[str, Any]:
    return _parse_to_mp_result(request)


@app.get("/api/history")
def mp_history(search: str = "", period: str = "all", limit: int = 100, user_key: str = "") -> dict[str, Any]:
    history = _list_history_items(user_key=user_key)
    filtered = _filter_history_items(history, search=search, period=period)
    return _legacy_success(
        {
            "items": filtered[: max(0, limit)],
            "total_count": _get_total_parse_count(user_key=user_key),
            "history_count": len(history),
        },
        retdesc="获取历史记录成功",
    )


@app.post("/api/history/clear")
def mp_clear_history(request: MPHistoryClearRequest) -> dict[str, Any]:
    _clear_history_items(user_key=request.user_key)
    return _legacy_success({"cleared": True}, retdesc="已清空历史记录")


@app.get("/api/ranking")
def mp_ranking(search: str = "", period: str = "7", limit: int = 50) -> dict[str, Any]:
    history = _list_history_items()
    filtered = _filter_history_items(history, search=search, period=period)
    ranking = _build_ranking_items(filtered)
    return _legacy_success(
        {
            "items": ranking[: max(0, limit)],
            "total_count": _get_total_parse_count(),
        },
        retdesc="获取热门榜单成功",
    )


@app.get("/api/stats")
def mp_stats(user_key: str = "") -> dict[str, Any]:
    history = _list_history_items(user_key=user_key)
    return _legacy_success(
        {
            "total_count": _get_total_parse_count(user_key=user_key),
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
