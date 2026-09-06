# English localization pipeline

Read this when preparing or diagnosing an English YouTube platform variant.

## Inputs and outputs

Required inputs are one Chinese video, Chinese title, and Chinese description. A Chinese SRT/VTT and Chinese thumbnail are optional inputs; when supplied, both must be localized.

The localizer produces only the assets needed by publishing:

- `*.en.vtt`: timed English accessibility captions.
- `*.youtube-en.mp4`: source video with the same audio and burned-in English captions.
- `*.youtube-en.jpg`: 1280×720 English thumbnail when a source thumbnail exists.
- `*.youtube-en.json`: READY manifest containing English metadata, 6–15 relevant discovery tags, file paths, hashes, locale, and cue count.

## Deterministic rules

- A supplied Chinese caption track is the source of truth for wording and timing.
- Without a caption track, extract temporary mono audio, transcribe Chinese speech in bounded chunks, and discard the temporary audio after use.
- Translate cues in bounded batches, preserve every cue id, and reject missing, empty, reordered, or Chinese-containing results.
- Translate the title and description directly, then generate concise English discovery tags from the actual subject matter. Reject generic unrelated trends, duplicate tags, `#` prefixes, and Chinese-containing output.
- Wrap captions to roughly 42 characters per line and split unusually long cues across their existing time range.
- Burn subtitles with a high-contrast mobile-readable style, retaining an independent WebVTT upload.
- Detect Chinese cover text with local RapidOCR, cover the detected regions, place concise English text, and export a YouTube-sized JPEG.
- A supplied cover that yields no Chinese text is held for review by default. Use `--thumbnail-no-text` only when the creator intentionally supplied a text-free cover.
- Validate the localized video hash when loading the manifest so editing after READY invalidates the task.
- Publishing may refine `english_tags` with related recent high-view YouTube results, but semantic relevance remains the first gate and localization tags remain the offline fallback.

## Runtime dependencies

- The default `argos` provider runs locally: Argos Translate converts Chinese to English, faster-whisper creates timed Chinese captions, and RapidOCR reads Chinese cover text. It does not need `OPENAI_API_KEY` and does not send the title, audio, video, captions or cover to a translation service.
- After installing `scripts/requirements-localization.txt`, run `youtube_localize.py install-local-models` once to download the Chinese→English Argos language package and the selected local speech model. The default speech model is `small`; pass `--local-asr-model base` when storage is limited or `--local-asr-model medium` when extra accuracy is needed.
- `--translation-provider openai` is a compatibility route. It requires `OPENAI_API_KEY`; its defaults remain `gpt-5-mini` for text/cover work and `whisper-1` for timestamped transcription.
- `ffmpeg` must include the `subtitles` filter backed by libass. The preflight refuses to claim readiness when this filter is absent. Install the dependencies in `scripts/requirements-localization.txt`; a system ffmpeg with libass is preferred because the `imageio-ffmpeg` fallback may omit that filter.
- A local result that fails a language, timestamp, subtitle or cover-text gate still produces `NEEDS_REVIEW` and blocks dispatch. Offline translation may need creator review for nuanced wording, names and stylized source text.
