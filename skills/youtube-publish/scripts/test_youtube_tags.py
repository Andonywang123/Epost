#!/usr/bin/env python3
"""Offline tests for YouTube tag cleanup and popularity refinement."""

from __future__ import annotations

import json

from youtube_publish import merge_youtube_tags, related_popular_tags


class Call:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def execute(self) -> dict:
        return self.payload


class Search:
    def list(self, **_: object) -> Call:
        return Call({"items": [{"id": {"videoId": "a"}}, {"id": {"videoId": "b"}}]})


class Videos:
    def list(self, **_: object) -> Call:
        return Call({"items": [
            {"snippet": {"tags": ["AI tools", "creator workflow", "unrelated"]},
             "statistics": {"viewCount": "500000"}},
            {"snippet": {"tags": ["AI tools", "workflow automation", "unrelated"]},
             "statistics": {"viewCount": "100000"}},
        ]})


class YouTube:
    def search(self) -> Search:
        return Search()

    def videos(self) -> Videos:
        return Videos()


def main() -> None:
    cleaned = merge_youtube_tags(["#AI tools", "AI tools", "Productivity", "中文"])
    assert cleaned == ["AI tools", "Productivity"]
    popular = related_popular_tags(YouTube(), "AI workflow automation", "Tools for creators", "US", "22")
    assert popular[0] == "AI tools"
    assert "workflow automation" in popular
    assert "unrelated" not in popular  # popularity never bypasses the relevance gate
    try:
        related_popular_tags(YouTube(), "AI", "", "USA", "22")
    except ValueError:
        pass
    else:
        raise AssertionError("expected invalid region to fail")
    print(json.dumps({"event": "tag_tests_passed", "count": 3}))


if __name__ == "__main__":
    main()
