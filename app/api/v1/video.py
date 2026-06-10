"""
Videos API route (OpenAI-compatible create endpoint).
"""

import asyncio
import base64
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiofiles
import orjson
from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.config import get_config
from app.core.exceptions import UpstreamException, ValidationException
from app.core.logger import logger
from app.core.storage import DATA_DIR
from app.services.grok.services.model import ModelService
from app.services.grok.services.video import VideoService
from app.services.grok.services.video_extend import VideoExtendService
from app.services.reverse.utils.session import ResettableSession


router = APIRouter(tags=["Videos"])

VIDEO_MODEL_ID = "grok-imagine-1.0-video"
SIZE_TO_ASPECT = {
    "1280x720": "16:9",
    "720x1280": "9:16",
    "1792x1024": "3:2",
    "1024x1792": "2:3",
    "1024x1024": "1:1",
}
QUALITY_TO_RESOLUTION = {
    "standard": "480p",
    "high": "720p",
}
VALID_RESOLUTIONS = {"480p", "720p"}

# ── async job storage ──────────────────────────────────────────────
_VIDEO_JOBS: dict[str, "_VideoJob"] = {}
_VIDEO_JOBS_LOCK = asyncio.Lock()
_VIDEO_JOB_TTL_S = 3600


@dataclass
class _VideoJob:
    id: str
    model: str
    prompt: str
    seconds: str
    size: str
    quality: str
    created_at: int
    status: str = "queued"
    progress: int = 0
    completed_at: int | None = None
    error: dict[str, Any] | None = None
    video_url: str = ""
    content_path: str = ""

    def to_dict(self, *, url: str = "") -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "object": "video",
            "created_at": self.created_at,
            "status": self.status,
            "model": self.model,
            "progress": self.progress,
            "prompt": self.prompt,
            "seconds": self.seconds,
            "size": self.size,
            "quality": self.quality,
        }
        if self.completed_at is not None:
            d["completed_at"] = self.completed_at
        if self.status == "completed" and url:
            d["url"] = url
            d["video_url"] = url
        if self.error is not None:
            d["error"] = self.error
        return d


async def _put_job(job: _VideoJob) -> None:
    async with _VIDEO_JOBS_LOCK:
        _VIDEO_JOBS[job.id] = job


async def _get_job(video_id: str) -> _VideoJob | None:
    async with _VIDEO_JOBS_LOCK:
        return _VIDEO_JOBS.get(video_id)


async def _cleanup_expired_jobs() -> None:
    now = time.time()
    async with _VIDEO_JOBS_LOCK:
        expired = [
            jid
            for jid, job in _VIDEO_JOBS.items()
            if now - job.created_at > _VIDEO_JOB_TTL_S
        ]
        for jid in expired:
            job = _VIDEO_JOBS.pop(jid, None)
            if job and job.content_path:
                try:
                    os.remove(job.content_path)
                except Exception:
                    pass


# ── request model ───────────────────────────────────────────────────

class VideoCreateRequest(BaseModel):
    """Supported create params only; unknown fields are ignored by design."""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(..., description="Video prompt")
    model: Optional[str] = Field(VIDEO_MODEL_ID, description="Model id")
    size: Optional[str] = Field("1792x1024", description="Output size")
    seconds: Optional[int] = Field(6, description="Video length in seconds")
    quality: Optional[str] = Field("standard", description="Quality: standard/high")
    resolution_name: Optional[str] = Field(
        None, description="Resolution: 480p / 720p (overrides quality)"
    )
    image_reference: Optional[Any] = Field(
        None,
        description="Image references using chat/completions content-block array format: [{type:'image_url', image_url:{url:'...'}}] or an array of plain URL strings",
    )
    input_reference: Optional[Any] = Field(
        None, description="Multipart input reference file"
    )


class VideoExtendDirectRequest(BaseModel):
    """Direct extension params (non-OpenAI-compatible)."""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(..., description="Prompt text mapped to message/originalPrompt")
    reference_id: str = Field(
        ...,
        description="Reference id mapped to extendPostId/originalPostId/parentPostId",
    )
    start_time: float = Field(..., description="Mapped to videoExtensionStartTime")
    ratio: str = Field("2:3", description="Mapped to aspectRatio")
    length: int = Field(6, description="Mapped to videoLength")
    resolution: str = Field("480p", description="Mapped to resolutionName")


# ── helpers ─────────────────────────────────────────────────────────

def _raise_validation_error(exc: ValidationError) -> None:
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = first.get("loc", [])
        msg = first.get("msg", "Invalid request")
        code = first.get("type", "invalid_value")
        param_parts = [
            str(x) for x in loc if not (isinstance(x, int) or str(x).isdigit())
        ]
        param = ".".join(param_parts) if param_parts else None
        raise ValidationException(message=msg, param=param, code=code)
    raise ValidationException(message="Invalid request", code="invalid_value")


def _extract_video_url(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        return ""

    md_match = re.search(r"\[video\]\(([^)\s]+)\)", content)
    if md_match:
        return md_match.group(1).strip()

    html_match = re.search(r"""<source[^>]+src=["']([^"']+)["']""", content)
    if html_match:
        return html_match.group(1).strip()

    url_match = re.search(r"""https?://[^\s"'<>]+""", content)
    if url_match:
        return url_match.group(0).strip().rstrip(".,)")

    return ""


def _normalize_model(model: Optional[str]) -> str:
    requested = (model or VIDEO_MODEL_ID).strip()
    model_info = ModelService.get(requested)
    if not model_info or not model_info.is_video:
        video_models = [m.model_id for m in ModelService.MODELS if m.is_video]
        raise ValidationException(
            message=(
                f"The model `{requested}` is not supported for video generation. "
                f"Supported: {video_models}"
            ),
            param="model",
            code="model_not_supported",
        )
    return requested


def _normalize_size(size: Optional[str]) -> Tuple[str, str]:
    value = (size or "1792x1024").strip()
    aspect_ratio = SIZE_TO_ASPECT.get(value)
    if not aspect_ratio:
        raise ValidationException(
            message=f"size must be one of {sorted(SIZE_TO_ASPECT.keys())}",
            param="size",
            code="invalid_size",
        )
    return value, aspect_ratio


def _resolve_effective_resolution(
    quality: Optional[str], resolution_name: Optional[str]
) -> Tuple[str, str]:
    """Return (quality_display, resolution_name).

    resolution_name (480p/720p) takes precedence over quality (standard/high).
    """
    if resolution_name and str(resolution_name).strip():
        rn = str(resolution_name).strip()
        if rn not in VALID_RESOLUTIONS:
            raise ValidationException(
                message=f"resolution_name must be one of {sorted(VALID_RESOLUTIONS)}",
                param="resolution_name",
                code="invalid_resolution",
            )
        # reverse-map quality for display
        quality_display = (
            "high" if rn == "720p" else "standard"
        )
        return quality_display, rn

    q = (quality or "standard").strip().lower()
    rn = QUALITY_TO_RESOLUTION.get(q)
    if not rn:
        raise ValidationException(
            message=f"quality must be one of {sorted(QUALITY_TO_RESOLUTION.keys())}",
            param="quality",
            code="invalid_quality",
        )
    return q, rn


def _normalize_seconds(seconds: Optional[int]) -> int:
    value = int(seconds or 6)
    if value < 6 or value > 30:
        raise ValidationException(
            message="seconds must be between 6 and 30",
            param="seconds",
            code="invalid_seconds",
        )
    return value


def _validate_reference_value(value: str, param: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("http://") or candidate.startswith("https://"):
        return candidate
    if candidate.startswith("data:"):
        return candidate
    raise ValidationException(
        message=f"{param} must be a URL or data URI",
        param=param,
        code="invalid_reference",
    )


def _parse_image_reference_item(value: Any, idx: int) -> str:
    """Parse a single image reference item inside an array."""
    param_prefix = f"image_reference[{idx}]" if idx is not None else "image_reference"

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValidationException(
                message=f"{param_prefix} cannot be empty",
                param=param_prefix,
                code="invalid_reference",
            )
        return _validate_reference_value(stripped, param_prefix)

    if isinstance(value, dict):
        block_type = value.get("type")
        if block_type != "image_url":
            raise ValidationException(
                message=f'{param_prefix} must have type="image_url"',
                param=f"{param_prefix}.type",
                code="invalid_reference",
            )
        inner = value.get("image_url")
        if not isinstance(inner, dict):
            raise ValidationException(
                message=f"{param_prefix}.image_url must be an object with a url field",
                param=f"{param_prefix}.image_url",
                code="invalid_reference",
            )
        url = inner.get("url", "")
        if not isinstance(url, str) or not url.strip():
            raise ValidationException(
                message=f"{param_prefix}.image_url.url cannot be empty",
                param=f"{param_prefix}.image_url.url",
                code="invalid_reference",
            )
        return _validate_reference_value(url.strip(), f"{param_prefix}.image_url.url")

    raise ValidationException(
        message=(
            f"{param_prefix} must be a URL string or "
            f'{{"type": "image_url", "image_url": {{"url": "..."}}}}'
        ),
        param=param_prefix,
        code="invalid_reference",
    )


def _parse_image_references(value: Any) -> List[str]:
    """Parse image_reference into a list of validated URL strings.

    Uses the same content-block format as chat/completions.
    Accepts:
      - None / ""  -> []
      - ["url", {"type": "image_url", ...}, ...] -> [url, ...]
      - JSON string of an array (for multipart/form-data)
    """
    if value is None or value == "":
        return []

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped[0] == "[":
            try:
                value = orjson.loads(stripped)
            except orjson.JSONDecodeError:
                raise ValidationException(
                    message="image_reference must be a JSON array string",
                    param="image_reference",
                    code="invalid_reference",
                )
        else:
            raise ValidationException(
                message="image_reference must be an array",
                param="image_reference",
                code="invalid_reference",
            )

    if isinstance(value, list):
        if not value:
            return []
        return [
            _parse_image_reference_item(item, idx=i) for i, item in enumerate(value)
        ]

    raise ValidationException(
        message="image_reference must be an array",
        param="image_reference",
        code="invalid_reference",
    )


async def _upload_to_data_uri(file: UploadFile, param: str) -> str:
    payload = await file.read()
    if not payload:
        raise ValidationException(
            message=f"{param} upload is empty",
            param=param,
            code="empty_file",
        )
    content_type = (file.content_type or "application/octet-stream").strip()
    encoded = base64.b64encode(payload).decode()
    return f"data:{content_type};base64,{encoded}"


async def _build_references_for_json(payload: BaseModel) -> List[str]:
    references: List[str] = []
    parsed_refs = _parse_image_references(getattr(payload, "image_reference", None))
    references.extend(parsed_refs)
    if getattr(payload, "input_reference", None) not in (None, ""):
        raise ValidationException(
            message="input_reference must be uploaded as multipart/form-data file",
            param="input_reference",
            code="invalid_reference",
        )
    return references


async def _build_payload_and_references_for_form(
    *,
    schema: type[BaseModel],
    prompt: Optional[str],
    model: Optional[str],
    size: Optional[str],
    seconds: Optional[int],
    quality: Optional[str],
    resolution_name: Optional[str],
    image_reference: Optional[str],
    upload_references: Optional[List[UploadFile]],
) -> Tuple[BaseModel, List[str]]:
    try:
        payload = schema.model_validate(
            {
                "prompt": prompt,
                "model": model,
                "size": size,
                "seconds": seconds,
                "quality": quality,
                "resolution_name": resolution_name,
                "image_reference": image_reference,
                "input_reference": None,
            }
        )
    except ValidationError as exc:
        _raise_validation_error(exc)

    references: List[str] = []
    for index, upload in enumerate(upload_references or []):
        if not isinstance(upload, (UploadFile, StarletteUploadFile)):
            raise ValidationException(
                message="input_reference must be a file in multipart/form-data",
                param=f"input_reference[{index}]",
                code="invalid_reference",
            )
        references.append(
            await _upload_to_data_uri(upload, f"input_reference[{index}]")
        )

    parsed_refs = _parse_image_references(payload.image_reference)
    references.extend(parsed_refs)
    return payload, references


def _multipart_create_schema(default_seconds: int) -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string", "default": VIDEO_MODEL_ID},
            "size": {"type": "string", "default": "1792x1024"},
            "seconds": {"type": "integer", "default": default_seconds},
            "quality": {"type": "string", "default": "standard"},
            "resolution_name": {"type": "string", "description": "480p or 720p"},
            "image_reference": {
                "type": "string",
                "description": "JSON string for image_reference array",
            },
            "input_reference": {"type": "string", "format": "binary"},
        },
    }


def _build_create_response(
    *,
    model: str,
    prompt: str,
    size: str,
    seconds: int,
    quality: str,
    url: str,
) -> Dict[str, Any]:
    ts = int(time.time())
    return {
        "id": f"video_{uuid.uuid4().hex[:24]}",
        "object": "video",
        "created_at": ts,
        "completed_at": ts,
        "status": "completed",
        "model": model,
        "prompt": prompt,
        "size": size,
        "seconds": str(seconds),
        "quality": quality,
        "url": url,
    }


# ── video download helper ───────────────────────────────────────────

def _video_cache_dir() -> Path:
    d = DATA_DIR / "tmp" / "video"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _download_video_bytes(video_url: str, *, job_id: str = "") -> str:
    """Download video from *video_url*, save to files video cache, return content_path."""
    filename = f"users-{job_id}-generated_video.mp4" if job_id else f"{uuid.uuid4().hex}.mp4"
    cache_path = _video_cache_dir() / filename
    tmp_path = cache_path.with_suffix(".mp4.tmp")

    session = ResettableSession()
    try:
        response = await session.get(video_url, stream=True)
        if response.status_code >= 400:
            raise UpstreamException(
                message=f"Video download failed, status {response.status_code}",
                details={"url": video_url, "status": response.status_code},
            )
        async with aiofiles.open(tmp_path, "wb") as f:
            if hasattr(response, "aiter_content"):
                async for chunk in response.aiter_content():
                    if chunk:
                        await f.write(chunk)
            else:
                await f.write(response.content)
        os.replace(tmp_path, cache_path)
    finally:
        try:
            await session.close()
        except Exception:
            pass

    return str(cache_path)


# ── core generation logic (used by both sync response path and async job) ──

async def _generate_video_url(
    payload: BaseModel,
    references: List[str],
    *,
    require_extension: bool = False,
) -> Tuple[str, str, str, str, str, int]:
    """Validate inputs, call VideoService, return (video_url, model, prompt, size, quality, seconds)."""
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise ValidationException(
            message="prompt is required",
            param="prompt",
            code="invalid_request_error",
        )

    model = _normalize_model(payload.model)
    size, aspect_ratio = _normalize_size(payload.size)

    quality_display, resolution_name = _resolve_effective_resolution(
        getattr(payload, "quality", None),
        getattr(payload, "resolution_name", None),
    )

    seconds = _normalize_seconds(payload.seconds)
    if require_extension and seconds <= 6:
        raise ValidationException(
            message="seconds must be between 7 and 30 for /video/extend",
            param="seconds",
            code="invalid_seconds",
        )

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for ref in references:
        content.append({"type": "image_url", "image_url": {"url": ref}})

    result = await VideoService.completions(
        model=model,
        messages=[{"role": "user", "content": content}],
        stream=False,
        reasoning_effort=None,
        aspect_ratio=aspect_ratio,
        video_length=seconds,
        resolution=resolution_name,
        preset="custom",
    )

    choices = result.get("choices") if isinstance(result, dict) else None
    if not isinstance(choices, list) or not choices:
        raise UpstreamException("Video generation failed: empty result")

    msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    rendered = msg.get("content", "") if isinstance(msg, dict) else ""
    video_url = _extract_video_url(rendered)
    if not video_url:
        raise UpstreamException("Video generation failed: missing video URL")

    return video_url, model, prompt, size, quality_display, seconds


# ── background job runner ────────────────────────────────────────────

async def _run_video_job(job: _VideoJob, payload: BaseModel, references: List[str]) -> None:
    try:
        job.status = "in_progress"
        job.progress = 10

        video_url, model, prompt, size, quality_display, seconds = (
            await _generate_video_url(payload, references)
        )

        job.progress = 80
        job.video_url = video_url

        content_path = await _download_video_bytes(video_url, job_id=job.id)
        job.content_path = content_path

        job.status = "completed"
        job.progress = 100
        job.completed_at = int(time.time())
        logger.info(f"Video job {job.id} completed: {video_url}")
    except Exception as e:
        job.status = "failed"
        job.progress = 0
        job.error = {
            "code": "video_generation_failed",
            "message": str(e),
        }
        logger.warning(f"Video job {job.id} failed: {e}")


# ── routes ──────────────────────────────────────────────────────────

@router.post(
    "/videos",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": VideoCreateRequest.model_json_schema()},
                "multipart/form-data": {"schema": _multipart_create_schema(6)},
            },
        }
    },
)
async def create_video(request: Request):
    """
    Videos create endpoint (async).

    POST /v1/videos returns immediately with a job id and status "queued".
    Use GET /v1/videos/{video_id} to poll for completion.
    Use GET /v1/videos/{video_id}/content to download the video file.
    """
    await _cleanup_expired_jobs()

    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            raw = await request.json()
        except ValueError:
            raise ValidationException(
                message=(
                    "Invalid JSON in request body. Please check for trailing commas or syntax errors."
                ),
                param="body",
                code="json_invalid",
            )
        if not isinstance(raw, dict):
            raise ValidationException(
                message="Request body must be a JSON object",
                param="body",
                code="invalid_request_error",
            )
        try:
            payload = VideoCreateRequest.model_validate(raw)
        except ValidationError as exc:
            _raise_validation_error(exc)
        references = await _build_references_for_json(payload)
    else:
        form = await request.form()
        upload_references: List[UploadFile] = []
        for field_name in ("input_reference", "input_reference[]", "image", "image[]"):
            for item in form.getlist(field_name):
                if isinstance(item, (UploadFile, StarletteUploadFile)):
                    upload_references.append(item)
                elif item not in (None, ""):
                    raise ValidationException(
                        message=f"{field_name} must be a file in multipart/form-data",
                        param=field_name,
                        code="invalid_reference",
                    )

        payload, references = await _build_payload_and_references_for_form(
            schema=VideoCreateRequest,
            prompt=form.get("prompt"),
            model=form.get("model"),
            size=form.get("size"),
            seconds=form.get("seconds"),
            quality=form.get("quality"),
            resolution_name=form.get("resolution_name"),
            image_reference=form.get("image_reference"),
            upload_references=upload_references,
        )

    # pre-validate inputs so we can return errors synchronously
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise ValidationException(
            message="prompt is required",
            param="prompt",
            code="invalid_request_error",
        )
    model = _normalize_model(payload.model)
    size, _ = _normalize_size(payload.size)
    quality_display, _ = _resolve_effective_resolution(
        getattr(payload, "quality", None),
        getattr(payload, "resolution_name", None),
    )
    seconds = _normalize_seconds(payload.seconds)

    job = _VideoJob(
        id=f"video_{uuid.uuid4().hex[:24]}",
        model=model,
        prompt=prompt,
        seconds=str(seconds),
        size=size,
        quality=quality_display,
        created_at=int(time.time()),
    )
    await _put_job(job)

    asyncio.create_task(_run_video_job(job, payload, references))
    asyncio.create_task(_cleanup_expired_jobs())

    return JSONResponse(content=job.to_dict())


@router.get("/videos/{video_id}")
async def retrieve_video(video_id: str, request: Request):
    """Retrieve video job status.

    GET /v1/videos/{video_id}

    Returns the current state of the video generation job.
    Completed jobs include a `url` field pointing to the downloadable content.
    """
    await _cleanup_expired_jobs()
    job = await _get_job(video_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"Video job '{video_id}' not found",
                    "type": "not_found",
                    "code": "video_not_found",
                }
            },
        )
    content_url = ""
    if job.status == "completed" and job.content_path:
        base = str(request.base_url).rstrip("/")
        content_url = f"{base}/v1/files/video/users/{video_id}/generated_video.mp4"
    return JSONResponse(content=job.to_dict(url=content_url))


@router.get("/videos/{video_id}/content")
async def get_video_content(video_id: str):
    """Download the generated video file.

    GET /v1/videos/{video_id}/content

    Returns the raw video/mp4 file.
    """
    await _cleanup_expired_jobs()
    job = await _get_job(video_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"Video job '{video_id}' not found",
                    "type": "not_found",
                    "code": "video_not_found",
                }
            },
        )
    if job.status == "queued" or job.status == "in_progress":
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "message": f"Video job '{video_id}' is still {job.status}",
                    "type": "conflict",
                    "code": "video_not_ready",
                }
            },
        )
    if job.status == "failed":
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "message": f"Video job '{video_id}' failed",
                    "type": "video_failed",
                    "code": "video_failed",
                }
            },
        )
    if not job.content_path or not Path(job.content_path).exists():
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"Video file for '{video_id}' not found on disk",
                    "type": "not_found",
                    "code": "video_content_not_found",
                }
            },
        )
    return FileResponse(
        job.content_path,
        media_type="video/mp4",
        filename=f"{video_id}.mp4",
    )


@router.post(
    "/video/extend",
)
async def extend_video(request: VideoExtendDirectRequest):
    """
    Extension endpoint (non-OpenAI-compatible direct mapping).
    """
    result = await VideoExtendService.extend(
        prompt=request.prompt,
        reference_id=request.reference_id,
        start_time=request.start_time,
        ratio=request.ratio,
        length=request.length,
        resolution=request.resolution,
    )
    return JSONResponse(content=result)


__all__ = ["router"]
