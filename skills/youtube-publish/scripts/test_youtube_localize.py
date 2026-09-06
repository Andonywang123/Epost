#!/usr/bin/env python3
"""Offline tests for metadata translation and YouTube tag normalization."""

from __future__ import annotations

import json
from types import SimpleNamespace

from youtube_localize import LocalizationError, local_youtube_tags, normalize_youtube_tags, parser, translate_metadata


class FakeResponses:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def create(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(output_text=json.dumps(self.payload))


class FakeClient:
    def __init__(self, payload: dict) -> None:
        self.responses = FakeResponses(payload)


def main() -> None:
    tags = normalize_youtube_tags(["#AI Tools", "AI Tools", "Productivity", "中文标签"])
    assert tags == ["AI Tools", "Productivity"]

    local_tags = local_youtube_tags(
        "Gentle Romance in Film Photography",
        "A personal film camera memory shared through an old photo album.",
    )
    assert len(local_tags) >= 6
    assert all(not any("\u4e00" <= character <= "\u9fff" for character in tag) for tag in local_tags)
    assert parser().parse_args(["preflight"]).translation_provider == "argos"

    client = FakeClient({
        "title": "Five AI Tools That Save Time",
        "description": "A practical walkthrough for creators.",
        "tags": [
            "AI tools", "creator productivity", "workflow automation", "time saving apps",
            "content creation", "productivity tips", "automation tutorial",
        ],
    })
    title, description, generated = translate_metadata(client, "test", "中文标题", "中文简介", "English")
    assert title == "Five AI Tools That Save Time"
    assert description.startswith("A practical")
    assert len(generated) == 7

    bad = FakeClient({"title": "English", "description": "English", "tags": ["one"] * 6})
    try:
        translate_metadata(bad, "test", "中文", "中文", "English")
    except LocalizationError as exc:
        assert "fewer than six" in str(exc)
    else:
        raise AssertionError("expected insufficient unique tags to fail")

    print(json.dumps({"event": "localization_tests_passed", "count": 5}))


if __name__ == "__main__":
    main()
