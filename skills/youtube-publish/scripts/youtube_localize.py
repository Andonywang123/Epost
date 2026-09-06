#!/usr/bin/env python3
"""Create a publish-ready English YouTube variant from one Chinese master.

The pipeline translates title/description, turns a Chinese caption file (or a
Chinese speech transcription) into timed English WebVTT, burns the English
captions into a copy of the video, and localizes Chinese text on a thumbnail.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
TIMESTAMP_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{3}|\d{1,2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{3}|\d{1,2}:\d{2}[,.]\d{3})"
)


@dataclass
class Cue:
    cue_id: int
    start: float
    end: float
    text: str


class LocalizationError(RuntimeError):
    pass


def emit(event: str, **data: Any) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise LocalizationError(f"{label} does not exist: {path}")
    return path


def openai_client(api_key_env: str) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LocalizationError(
            "Missing OpenAI SDK. Install scripts/requirements.txt before localization."
        ) from exc
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise LocalizationError(f"{api_key_env} is not configured.")
    return OpenAI(api_key=api_key)


def response_json(client: Any, model: str, instructions: str, payload: dict[str, Any],
                  schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=json.dumps(payload, ensure_ascii=False),
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
    )
    raw = getattr(response, "output_text", None)
    if not raw:
        raise LocalizationError(f"Translation model returned no {schema_name} output.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LocalizationError(f"Translation model returned invalid {schema_name} JSON.") from exc
    if not isinstance(parsed, dict):
        raise LocalizationError(f"Translation model returned an invalid {schema_name} object.")
    return parsed


def normalize_youtube_tags(values: Iterable[Any], limit: int = 15) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = re.sub(r"\s+", " ", str(value)).strip().lstrip("#").strip()
        if not tag or len(tag) > 40 or CJK_RE.search(tag):
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= limit:
            break
    while sum(len(tag) for tag in tags) + max(0, len(tags) - 1) > 450:
        tags.pop()
    return tags


def translate_metadata(client: Any, model: str, title: str, description: str,
                       locale: str) -> tuple[str, str, list[str]]:
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 6,
                "maxItems": 15,
            },
        },
        "required": ["title", "description", "tags"],
        "additionalProperties": False,
    }
    result = response_json(
        client,
        model,
        (
            f"Translate Chinese creator metadata into natural {locale} for YouTube. "
            "Preserve meaning, names, paragraph breaks, links and disclosures. Do not add claims, "
            "hashtags or promotional language in the title or description. Keep the title concise and "
            "under 100 characters. Also return 6-15 concise English YouTube tags: combine broad discovery "
            "terms with specific high-intent phrases, keep every tag directly relevant, and avoid misleading "
            "trends, unrelated celebrities, or claims of real-time popularity. Return tags without #."
        ),
        {"title": title, "description": description},
        "youtube_metadata",
        schema,
    )
    en_title = str(result["title"]).strip()
    en_description = str(result["description"]).strip()
    en_tags = normalize_youtube_tags(result.get("tags", []))
    if not en_title or len(en_title) > 100:
        raise LocalizationError("Translated YouTube title is empty or exceeds 100 characters.")
    if description.strip() and not en_description:
        raise LocalizationError("Translated YouTube description is empty.")
    if CJK_RE.search(en_title) or CJK_RE.search(en_description):
        raise LocalizationError("Translated metadata still contains Chinese text and needs review.")
    if len(en_tags) < 6:
        raise LocalizationError("YouTube tag recommendation returned fewer than six usable English tags.")
    return en_title, en_description, en_tags


def require_argos_translation() -> Any:
    """Return the installed Simplified-Chinese-to-English Argos translator."""
    try:
        import argostranslate.translate
    except ImportError as exc:
        raise LocalizationError(
            "Argos Translate is not installed. Install scripts/requirements-localization.txt."
        ) from exc
    languages = argostranslate.translate.get_installed_languages()
    source = next((language for language in languages
                   if language.code.lower().replace("_", "-") in {"zh", "zh-cn", "zh-hans"}), None)
    target = next((language for language in languages
                   if language.code.lower().replace("_", "-") in {"en", "en-gb", "en-us"}), None)
    if source is None or target is None:
        raise LocalizationError(
            "The local Chinese-to-English Argos model is not installed. Run "
            "youtube_localize.py install-local-models before publishing."
        )
    try:
        translator = source.get_translation(target)
    except Exception as exc:
        raise LocalizationError(
            "The local Chinese-to-English Argos model is not installed. Run "
            "youtube_localize.py install-local-models before publishing."
        ) from exc
    if translator is None:
        raise LocalizationError(
            "The local Chinese-to-English Argos model is not installed. Run "
            "youtube_localize.py install-local-models before publishing."
        )
    return translator


def argos_translate(translator: Any, text: str, label: str) -> str:
    translated = re.sub(r"\s+", " ", str(translator.translate(text))).strip()
    if not translated:
        raise LocalizationError(f"Local translation returned an empty {label}.")
    if CJK_RE.search(translated):
        raise LocalizationError(f"Local translation left Chinese text in {label}.")
    return translated


def local_youtube_tags(title: str, description: str) -> list[str]:
    """Make conservative discovery tags from the translated creator copy."""
    stop_words = {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it",
        "my", "of", "on", "or", "that", "the", "this", "to", "was", "with", "you", "your",
    }
    candidates: list[str] = []
    for text in (title, description):
        words = [word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z'-]*", text)]
        useful = [word for word in words
                  if word not in stop_words and not word.endswith("'s") and len(word) > 2]
        if 1 < len(useful) <= 6:
            candidates.append(" ".join(useful))
        for size in (3, 2, 1):
            for start in range(0, max(0, len(useful) - size + 1)):
                candidates.append(" ".join(useful[start:start + size]))
    tags = normalize_youtube_tags(candidates)
    if len(tags) < 6:
        raise LocalizationError(
            "Local translation could not derive six relevant English tags. Add a fuller English title or description."
        )
    return tags


def translate_metadata_argos(title: str, description: str, locale: str) -> tuple[str, str, list[str]]:
    translator = require_argos_translation()
    en_title = argos_translate(translator, title, "title")
    en_description = argos_translate(translator, description, "description") if description.strip() else ""
    if len(en_title) > 100:
        raise LocalizationError("Translated YouTube title exceeds 100 characters.")
    return en_title, en_description, local_youtube_tags(en_title, en_description)


def timestamp_seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise LocalizationError(f"Invalid caption timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_caption(path: Path) -> list[Cue]:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    blocks = re.split(r"\n\s*\n", raw.strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.splitlines()]
        match_index = next((i for i, line in enumerate(lines) if TIMESTAMP_RE.search(line)), None)
        if match_index is None:
            continue
        match = TIMESTAMP_RE.search(lines[match_index])
        assert match is not None
        text = " ".join(line.strip() for line in lines[match_index + 1:] if line.strip())
        text = re.sub(r"<[^>]+>", "", text).strip()
        if not text:
            continue
        cues.append(Cue(len(cues), timestamp_seconds(match.group("start")),
                        timestamp_seconds(match.group("end")), text))
    if not cues:
        raise LocalizationError("No timed captions were found in the Chinese subtitle file.")
    for previous, current in zip(cues, cues[1:]):
        if current.start < previous.start or current.end <= current.start:
            raise LocalizationError("Chinese subtitle timestamps are invalid or out of order.")
    return cues


def ffmpeg_binary() -> str:
    configured = os.environ.get("FFMPEG_BINARY")
    if configured and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser().resolve())
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise LocalizationError(
            "ffmpeg is required to extract audio and burn subtitles. Install ffmpeg or "
            "scripts/requirements-localization.txt."
        ) from exc


def run_ffmpeg(command: list[str], purpose: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-12:])
        raise LocalizationError(f"ffmpeg failed while {purpose}: {tail}")
    return result


def media_duration(ffmpeg: str, video: Path) -> float:
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video), "-f", "null", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise LocalizationError("Could not determine video duration.")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def media_dimensions(ffmpeg: str, video: Path) -> tuple[int, int]:
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})(?:[ ,]|$)", result.stderr)
    if not match:
        raise LocalizationError("Could not determine video dimensions.")
    return int(match.group(1)), int(match.group(2))


def object_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def transcribe_video(client: Any, video: Path, transcription_model: str,
                     chunk_seconds: int = 900) -> list[Cue]:
    ffmpeg = ffmpeg_binary()
    duration = media_duration(ffmpeg, video)
    cues: list[Cue] = []
    with tempfile.TemporaryDirectory(prefix="epost-youtube-audio-") as tmp:
        temp_root = Path(tmp)
        chunk_count = max(1, math.ceil(duration / chunk_seconds))
        for index in range(chunk_count):
            start = index * chunk_seconds
            length = min(chunk_seconds, max(0.1, duration - start))
            audio = temp_root / f"audio-{index:03d}.m4a"
            run_ffmpeg(
                [ffmpeg, "-y", "-ss", str(start), "-t", str(length), "-i", str(video),
                 "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k", str(audio)],
                "extracting Chinese speech",
            )
            with audio.open("rb") as handle:
                response = client.audio.transcriptions.create(
                    model=transcription_model,
                    file=handle,
                    language="zh",
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                    temperature=0,
                )
            segments = object_value(response, "segments", []) or []
            for segment in segments:
                text = str(object_value(segment, "text", "")).strip()
                if not text:
                    continue
                seg_start = float(object_value(segment, "start", 0)) + start
                seg_end = float(object_value(segment, "end", seg_start)) + start
                if seg_end <= seg_start:
                    continue
                avg_logprob = object_value(segment, "avg_logprob")
                compression_ratio = object_value(segment, "compression_ratio")
                if avg_logprob is not None and float(avg_logprob) < -1.0:
                    raise LocalizationError("Chinese speech transcription confidence is too low.")
                if compression_ratio is not None and float(compression_ratio) > 2.4:
                    raise LocalizationError("Chinese speech transcription repetition check failed.")
                cues.append(Cue(len(cues), seg_start, seg_end, text))
            emit("transcription_progress", completed=index + 1, total=chunk_count)
    if not cues:
        raise LocalizationError("No Chinese speech was detected in the video.")
    return cues


def local_speech_model(model_name: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise LocalizationError(
            "faster-whisper is not installed. Install scripts/requirements-localization.txt."
        ) from exc
    try:
        return WhisperModel(model_name, device="cpu", compute_type="int8")
    except Exception as exc:
        raise LocalizationError(
            f"The local speech model '{model_name}' is unavailable. Run "
            "youtube_localize.py install-local-models before publishing."
        ) from exc


def transcribe_video_local(video: Path, model_name: str) -> list[Cue]:
    model = local_speech_model(model_name)
    try:
        segments, _ = model.transcribe(
            str(video), language="zh", beam_size=5, vad_filter=True,
            condition_on_previous_text=True,
        )
        cues: list[Cue] = []
        for segment in segments:
            text = str(getattr(segment, "text", "")).strip()
            start, end = float(getattr(segment, "start", 0)), float(getattr(segment, "end", 0))
            if not text or end <= start:
                continue
            if float(getattr(segment, "no_speech_prob", 0)) >= 0.85:
                continue
            if float(getattr(segment, "compression_ratio", 0)) > 2.6:
                raise LocalizationError("Local Chinese speech transcription repetition check failed.")
            cues.append(Cue(len(cues), start, end, text))
    except LocalizationError:
        raise
    except Exception as exc:
        raise LocalizationError("Local Chinese speech transcription failed.") from exc
    if not cues:
        raise LocalizationError("No Chinese speech was detected in the video.")
    emit("transcription_progress", completed=1, total=1, provider="faster-whisper")
    return cues


def batched(values: list[Cue], size: int) -> Iterable[list[Cue]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def translate_cues(client: Any, model: str, cues: list[Cue], locale: str) -> list[Cue]:
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    translations: dict[int, str] = {}
    batches = list(batched(cues, 60))
    for number, batch in enumerate(batches, start=1):
        payload = {"items": [{"id": cue.cue_id, "text": cue.text} for cue in batch]}
        result = response_json(
            client,
            model,
            (
                f"Translate each Chinese subtitle cue into concise, natural {locale}. Preserve every id. "
                "Translate only what is said; do not merge, omit, explain or add punctuation-only cues. "
                "Keep names consistent and make each line easy to read on a mobile video."
            ),
            payload,
            "subtitle_translation",
            schema,
        )
        items = result.get("items")
        if not isinstance(items, list) or len(items) != len(batch):
            raise LocalizationError("Subtitle translation changed the number of cues.")
        for item in items:
            cue_id = int(item["id"])
            translated = re.sub(r"\s+", " ", str(item["text"])).strip()
            if not translated or CJK_RE.search(translated):
                raise LocalizationError(f"Subtitle cue {cue_id} was not fully translated.")
            translations[cue_id] = translated
        emit("subtitle_translation_progress", completed=number, total=len(batches))
    if set(translations) != {cue.cue_id for cue in cues}:
        raise LocalizationError("Subtitle translation ids do not match the Chinese source.")
    return [Cue(cue.cue_id, cue.start, cue.end, translations[cue.cue_id]) for cue in cues]


def translate_cues_argos(cues: list[Cue]) -> list[Cue]:
    translator = require_argos_translation()
    translated: list[Cue] = []
    batches = list(batched(cues, 60))
    for number, batch in enumerate(batches, start=1):
        for cue in batch:
            translated.append(Cue(cue.cue_id, cue.start, cue.end,
                                  argos_translate(translator, cue.text, f"subtitle cue {cue.cue_id}")))
        emit("subtitle_translation_progress", completed=number, total=len(batches), provider="argos")
    return translated


def split_long_cue(cue: Cue, max_chars: int) -> list[Cue]:
    words = cue.text.split()
    if len(cue.text) <= max_chars or len(words) < 2:
        return [cue]
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and len(candidate) > max_chars:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    if len(chunks) == 1:
        return [cue]
    total_weight = sum(max(1, len(chunk)) for chunk in chunks)
    duration = max(0.2, cue.end - cue.start)
    result: list[Cue] = []
    cursor = cue.start
    for index, chunk in enumerate(chunks):
        share = duration * max(1, len(chunk)) / total_weight
        end = cue.end if index == len(chunks) - 1 else cursor + share
        result.append(Cue(cue.cue_id * 100 + index, cursor, end, chunk))
        cursor = end
    return result


def caption_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def write_vtt(cues: list[Cue], output: Path, line_width: int = 42) -> None:
    expanded = [piece for cue in cues for piece in split_long_cue(cue, line_width * 2)]
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(expanded, start=1):
        wrapped = "\n".join(textwrap.wrap(cue.text, width=line_width, break_long_words=False,
                                           break_on_hyphens=False))
        lines.extend([str(index), f"{caption_time(cue.start)} --> {caption_time(cue.end)}", wrapped, ""])
    output.write_text("\n".join(lines), encoding="utf-8")


def detect_cover_text(client: Any, model: str, image: Path) -> list[dict[str, Any]]:
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "x": {"type": "integer", "minimum": 0, "maximum": 1000},
                        "y": {"type": "integer", "minimum": 0, "maximum": 1000},
                        "width": {"type": "integer", "minimum": 1, "maximum": 1000},
                        "height": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                    "required": ["text", "x", "y", "width", "height"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    mime = mimetypes.guess_type(str(image))[0] or "image/jpeg"
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    response = client.responses.create(
        model=model,
        input=[{
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Find every visible line or compact group of Chinese text in this thumbnail. "
                        "Return the exact text and a tight bounding box. Coordinates use a 0-1000 grid "
                        "with x/y at the top-left. Do not return English text, numbers alone, faces or logos."
                    ),
                },
                {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}", "detail": "high"},
            ],
        }],
        text={"format": {"type": "json_schema", "name": "thumbnail_ocr",
                          "schema": schema, "strict": True}},
    )
    raw = getattr(response, "output_text", None)
    if not raw:
        raise LocalizationError("Thumbnail OCR returned no output.")
    try:
        items = json.loads(raw).get("items", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise LocalizationError("Thumbnail OCR returned invalid JSON.") from exc
    return [item for item in items if CJK_RE.search(str(item.get("text", "")))]


def translate_cover_lines(client: Any, model: str, lines: list[dict[str, Any]],
                          locale: str) -> dict[int, str]:
    if not lines:
        return {}
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    payload = {"items": [{"id": index, "text": item["text"]} for index, item in enumerate(lines)]}
    result = response_json(
        client,
        model,
        f"Translate each Chinese thumbnail phrase into very short, punchy {locale}. Preserve every id. No hashtags.",
        payload,
        "thumbnail_translation",
        schema,
    )
    translated = {int(item["id"]): str(item["text"]).strip() for item in result["items"]}
    if set(translated) != set(range(len(lines))) or any(not text for text in translated.values()):
        raise LocalizationError("Thumbnail translation is incomplete.")
    if any(CJK_RE.search(text) for text in translated.values()):
        raise LocalizationError("Thumbnail translation still contains Chinese text.")
    return translated


def font_path() -> str:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    return next((path for path in candidates if Path(path).is_file()), candidates[-1])


def fit_text(draw: Any, text: str, max_width: int, max_height: int, image_font: Any) -> tuple[Any, str]:
    for size in range(max(14, min(72, max_height)), 13, -2):
        font = image_font.truetype(font_path(), size=size)
        for width in range(28, 11, -2):
            wrapped = "\n".join(textwrap.wrap(text, width=width, break_long_words=False))
            box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=max(2, size // 6),
                                           stroke_width=max(1, size // 16))
            if box[2] - box[0] <= max_width and box[3] - box[1] <= max_height:
                return font, wrapped
    font = image_font.truetype(font_path(), size=14)
    return font, textwrap.shorten(text, width=32, placeholder="…")


def localize_thumbnail(client: Any, model: str, thumbnail: Path, output: Path,
                       locale: str, require_chinese_text: bool = True) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError as exc:
        raise LocalizationError("Pillow is required to localize YouTube thumbnails.") from exc
    image = Image.open(thumbnail).convert("RGB")
    width, height = image.size
    lines = detect_cover_text(client, model, thumbnail)
    if require_chinese_text and not lines:
        raise LocalizationError(
            "No Chinese thumbnail text was detected. Review the cover or mark it as text-free."
        )
    translations = translate_cover_lines(client, model, lines, locale)
    for item in lines:
        x = float(item["x"]) / 1000
        y = float(item["y"]) / 1000
        w = float(item["width"]) / 1000
        h = float(item["height"]) / 1000
        left = max(0, int(x * width) - 8)
        top = max(0, int(y * height) - 8)
        right = min(width, int((x + w) * width) + 8)
        bottom = min(height, int((y + h) * height) + 8)
        blurred = image.crop((left, top, right, bottom)).filter(ImageFilter.GaussianBlur(radius=12))
        image.paste(blurred, (left, top))
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle((left, top, right, bottom), radius=max(6, (bottom-top)//8),
                                       fill=(8, 12, 18, 105))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    target = (1280, 720)
    if image.size != target:
        background = image.copy()
        background.thumbnail(target)
        fill = image.resize(target).filter(ImageFilter.GaussianBlur(radius=28))
        dark = Image.new("RGBA", target, (0, 0, 0, 80))
        canvas = Image.alpha_composite(fill.convert("RGBA"), dark).convert("RGB")
        x = (target[0] - background.width) // 2
        y = (target[1] - background.height) // 2
        canvas.paste(background, (x, y))
        image = canvas
    if translations:
        headline = " — ".join(translations[index] for index in range(len(lines)))
        overlay = Image.new("RGBA", target, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        panel_top = 486
        overlay_draw.rectangle((0, panel_top, target[0], target[1]), fill=(7, 12, 16, 205))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(image)
        font, wrapped = fit_text(draw, headline, 1160, 170, ImageFont)
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=7, stroke_width=2)
        text_height = box[3] - box[1]
        draw.multiline_text((60, panel_top + (234 - text_height) / 2 - box[1]), wrapped,
                            font=font, fill="white", spacing=7, stroke_width=2,
                            stroke_fill="#111111")
    quality = 90
    image.save(output, "JPEG", quality=quality, optimize=True)
    while output.stat().st_size > 2 * 1024 * 1024 and quality > 55:
        quality -= 5
        image.save(output, "JPEG", quality=quality, optimize=True)


def detect_cover_text_local(image: Path) -> list[dict[str, Any]]:
    script = Path(__file__).with_name("thumbnail_ocr.py")
    if not script.is_file():
        raise LocalizationError("RapidOCR is unavailable for local thumbnail localization.")
    result = subprocess.run([os.sys.executable, str(script), "--image", str(image)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise LocalizationError(f"Local thumbnail OCR failed: {result.stderr.strip() or 'unknown error'}")
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LocalizationError("Local thumbnail OCR returned invalid JSON.") from exc
    if not isinstance(values, list):
        raise LocalizationError("Local thumbnail OCR returned an invalid result.")
    lines: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict) or not CJK_RE.search(str(item.get("text", ""))):
            continue
        try:
            normalized = {
                "text": str(item["text"]).strip(),
                "x": max(0, min(1000, int(item["x"]))),
                "y": max(0, min(1000, int(item["y"]))),
                "width": max(1, min(1000, int(item["width"]))),
                "height": max(1, min(1000, int(item["height"]))),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalizationError("Local thumbnail OCR returned invalid coordinates.") from exc
        lines.append(normalized)
    return lines


def render_localized_thumbnail(thumbnail: Path, output: Path, lines: list[dict[str, Any]],
                               translations: dict[int, str], require_chinese_text: bool,
                               headline: str | None = None) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError as exc:
        raise LocalizationError("Pillow is required to localize YouTube thumbnails.") from exc
    if require_chinese_text and not lines:
        raise LocalizationError(
            "No Chinese thumbnail text was detected. Review the cover or mark it as text-free."
        )
    image = Image.open(thumbnail).convert("RGB")
    width, height = image.size
    for item in lines:
        x, y = float(item["x"]) / 1000, float(item["y"]) / 1000
        w, h = float(item["width"]) / 1000, float(item["height"]) / 1000
        left, top = max(0, int(x * width) - 8), max(0, int(y * height) - 8)
        right, bottom = min(width, int((x + w) * width) + 8), min(height, int((y + h) * height) + 8)
        blurred = image.crop((left, top, right, bottom)).filter(ImageFilter.GaussianBlur(radius=12))
        image.paste(blurred, (left, top))
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle((left, top, right, bottom),
                                                    radius=max(6, (bottom - top) // 8),
                                                    fill=(8, 12, 18, 105))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    target = (1280, 720)
    if image.size != target:
        background = image.copy()
        background.thumbnail(target)
        fill = image.resize(target).filter(ImageFilter.GaussianBlur(radius=28))
        canvas = Image.alpha_composite(fill.convert("RGBA"),
                                       Image.new("RGBA", target, (0, 0, 0, 80))).convert("RGB")
        canvas.paste(background, ((target[0] - background.width) // 2,
                                  (target[1] - background.height) // 2))
        image = canvas
    if headline or translations:
        headline = headline or " — ".join(translations[index] for index in range(len(lines)))
        overlay = Image.new("RGBA", target, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle((0, 486, target[0], target[1]), fill=(7, 12, 16, 205))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(image)
        font, wrapped = fit_text(draw, headline, 1160, 170, ImageFont)
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=7, stroke_width=2)
        draw.multiline_text((60, 486 + (234 - (box[3] - box[1])) / 2 - box[1]), wrapped,
                            font=font, fill="white", spacing=7, stroke_width=2, stroke_fill="#111111")
    quality = 90
    image.save(output, "JPEG", quality=quality, optimize=True)
    while output.stat().st_size > 2 * 1024 * 1024 and quality > 55:
        quality -= 5
        image.save(output, "JPEG", quality=quality, optimize=True)


def localize_thumbnail_argos(thumbnail: Path, output: Path,
                             require_chinese_text: bool = True) -> None:
    lines = detect_cover_text_local(thumbnail)
    translator = require_argos_translation()
    source_headline = "".join(str(item["text"]) for item in sorted(lines, key=lambda item: (item["x"], item["y"])))
    headline = argos_translate(translator, source_headline, "thumbnail text") if source_headline else None
    render_localized_thumbnail(thumbnail, output, lines, {}, require_chinese_text, headline)


def subtitle_filter_path(path: Path) -> str:
    value = str(path).replace("\\", "\\\\").replace(":", "\\:")
    return value.replace("'", "\\'").replace("[", "\\[").replace("]", "\\]")


def burn_subtitles(video: Path, caption: Path, output: Path) -> None:
    ffmpeg = ffmpeg_binary()
    filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    if " subtitles " not in filters.stdout:
        raise LocalizationError("This ffmpeg build does not include the libass subtitles filter.")
    style = (
        "FontName=Arial,FontSize=10,PrimaryColour=&H00FFFFFF,"
        "BackColour=&H88000000,BorderStyle=3,Outline=1,Shadow=0,"
        "Alignment=2,MarginL=24,MarginR=24,MarginV=24"
    )
    vf = f"subtitles='{subtitle_filter_path(caption)}':force_style='{style}'"
    temp = output.with_suffix(".tmp.mp4")
    run_ffmpeg(
        [ffmpeg, "-y", "-i", str(video), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(temp)],
        "burning English subtitles into the video",
    )
    temp.replace(output)


def localize_bundle(*, video: str | Path, source_title: str, source_description: str,
                    output_dir: str | Path, source_caption: str | Path | None = None,
                    thumbnail: str | Path | None = None, locale: str = "British English",
                    translation_model: str = "gpt-5-mini", transcription_model: str = "whisper-1",
                    api_key_env: str = "OPENAI_API_KEY", translation_provider: str = "argos",
                    local_asr_model: str = "small", burn: bool = True,
                    thumbnail_no_text: bool = False) -> dict[str, Any]:
    source_video = require_file(video, "video")
    if not source_title.strip():
        raise LocalizationError("Chinese source title is required.")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if translation_provider not in {"argos", "openai"}:
        raise LocalizationError("translation_provider must be 'argos' or 'openai'.")
    client = openai_client(api_key_env) if translation_provider == "openai" else None
    emit("localization_started", video=str(source_video))

    if translation_provider == "openai":
        assert client is not None
        en_title, en_description, en_tags = translate_metadata(
            client, translation_model, source_title, source_description, locale
        )
    else:
        en_title, en_description, en_tags = translate_metadata_argos(
            source_title, source_description, locale
        )
    if source_caption:
        zh_cues = parse_caption(require_file(source_caption, "Chinese caption"))
        caption_source = "provided_caption"
    else:
        zh_cues = (transcribe_video(client, source_video, transcription_model)
                   if translation_provider == "openai" else
                   transcribe_video_local(source_video, local_asr_model))
        caption_source = "speech_transcription"
    en_cues = (translate_cues(client, translation_model, zh_cues, locale)
               if translation_provider == "openai" else translate_cues_argos(zh_cues))
    caption_path = output_root / f"{source_video.stem}.en.vtt"
    video_width, video_height = media_dimensions(ffmpeg_binary(), source_video)
    line_width = 28 if video_width / video_height < 0.75 else 42
    write_vtt(en_cues, caption_path, line_width=line_width)

    localized_video = source_video
    if burn:
        localized_video = output_root / f"{source_video.stem}.youtube-en.mp4"
        burn_subtitles(source_video, caption_path, localized_video)

    localized_thumbnail: Path | None = None
    if thumbnail:
        source_thumbnail = require_file(thumbnail, "thumbnail")
        localized_thumbnail = output_root / f"{source_thumbnail.stem}.youtube-en.jpg"
        if translation_provider == "openai":
            assert client is not None
            localize_thumbnail(client, translation_model, source_thumbnail, localized_thumbnail, locale,
                               require_chinese_text=not thumbnail_no_text)
        else:
            localize_thumbnail_argos(source_thumbnail, localized_thumbnail,
                                     require_chinese_text=not thumbnail_no_text)

    result = {
        "version": 1,
        "status": "READY",
        "source_video": str(source_video),
        "source_video_sha256": sha256(source_video),
        "localized_video": str(localized_video),
        "localized_video_sha256": sha256(localized_video),
        "english_title": en_title,
        "english_description": en_description,
        "english_tags": en_tags,
        "english_caption": str(caption_path),
        "caption_source": caption_source,
        "localized_thumbnail": str(localized_thumbnail) if localized_thumbnail else None,
        "locale": locale,
        "translation_provider": translation_provider,
        "cue_count": len(en_cues),
    }
    manifest = output_root / f"{source_video.stem}.youtube-en.json"
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["manifest"] = str(manifest)
    emit("localization_ready", **result)
    return result


def preflight_command(args: argparse.Namespace) -> None:
    ffmpeg = ffmpeg_binary()
    filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    if " subtitles " not in filters.stdout:
        raise LocalizationError("ffmpeg is installed but lacks the libass subtitles filter.")
    if args.translation_provider == "openai":
        client = openai_client(args.api_key_env)
        client.models.retrieve(args.translation_model)
        emit("localization_preflight_ok", ffmpeg=ffmpeg, model=args.translation_model,
             provider="openai", thumbnail_ocr="OpenAI vision model")
        return
    require_argos_translation()
    local_speech_model(args.local_asr_model)
    if not Path(__file__).with_name("thumbnail_ocr.py").is_file():
        raise LocalizationError("RapidOCR is unavailable for local thumbnail localization.")
    try:
        import rapidocr_onnxruntime  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LocalizationError("RapidOCR is unavailable for local thumbnail localization.") from exc
    emit("localization_preflight_ok", ffmpeg=ffmpeg, provider="argos",
         speech_model=args.local_asr_model, thumbnail_ocr="RapidOCR")


def install_local_models_command(args: argparse.Namespace) -> None:
    try:
        import argostranslate.package
    except ImportError as exc:
        raise LocalizationError(
            "Argos Translate is not installed. Install scripts/requirements-localization.txt."
        ) from exc
    try:
        require_argos_translation()
        argos_status = "already_installed"
    except LocalizationError:
        try:
            argostranslate.package.update_package_index()
            package = next((item for item in argostranslate.package.get_available_packages()
                            if item.from_code == "zh" and item.to_code == "en"), None)
            if package is None:
                raise LocalizationError("The Argos Chinese-to-English model is unavailable in its package index.")
            argostranslate.package.install_from_path(package.download())
            require_argos_translation()
            argos_status = "installed"
        except LocalizationError:
            raise
        except Exception as exc:
            raise LocalizationError("Could not download the local Argos Chinese-to-English model.") from exc
    local_speech_model(args.local_asr_model)
    emit("local_models_ready", provider="argos", argos_model=argos_status,
         speech_model=args.local_asr_model)


def localize_command(args: argparse.Namespace) -> None:
    localize_bundle(
        video=args.video,
        source_title=args.source_title,
        source_description=args.source_description,
        source_caption=args.source_caption,
        thumbnail=args.thumbnail,
        output_dir=args.output_dir,
        locale=args.locale,
        translation_model=args.translation_model,
        transcription_model=args.transcription_model,
        api_key_env=args.api_key_env,
        translation_provider=args.translation_provider,
        local_asr_model=args.local_asr_model,
        burn=not args.no_burn,
        thumbnail_no_text=args.thumbnail_no_text,
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Localize a Chinese YouTube master into English")
    root.add_argument("--translation-provider", choices=("argos", "openai"), default="argos")
    root.add_argument("--api-key-env", default="OPENAI_API_KEY")
    root.add_argument("--translation-model", default="gpt-5-mini")
    root.add_argument("--local-asr-model", default="small")
    sub = root.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight")
    preflight.set_defaults(func=preflight_command)
    models = sub.add_parser("install-local-models")
    models.set_defaults(func=install_local_models_command)
    localize = sub.add_parser("localize")
    localize.add_argument("--video", required=True)
    localize.add_argument("--source-title", required=True)
    localize.add_argument("--source-description", default="")
    localize.add_argument("--source-caption")
    localize.add_argument("--thumbnail")
    localize.add_argument("--thumbnail-no-text", action="store_true",
                          help="thumbnail is intentionally text-free; skip the Chinese-text gate")
    localize.add_argument("--output-dir", required=True)
    localize.add_argument("--locale", default="British English")
    localize.add_argument("--transcription-model", default="whisper-1")
    localize.add_argument("--no-burn", action="store_true")
    localize.set_defaults(func=localize_command)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        args.func(args)
    except LocalizationError as exc:
        emit("localization_failed", message=str(exc), status="NEEDS_REVIEW")
        raise SystemExit(3)


if __name__ == "__main__":
    main()
