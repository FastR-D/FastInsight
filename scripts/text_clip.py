"""Sentence-boundary clipping for paper abstracts and publish payloads."""

import re

ABSTRACT_STORE_LIMIT = 8000

_SPACES = re.compile(r"[ \t]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")
_SOURCE_RANK = {
    "paper": 3,
    "image_summary": 2,
    "search": 1,
}


def normalize_ws(value):
    text = "" if value is None else str(value)
    text = _SPACES.sub(" ", text).strip()
    return _MANY_NEWLINES.sub("\n\n", text)


def _last_boundary(chunk):
    return max(
        chunk.rfind("。"),
        chunk.rfind("！"),
        chunk.rfind("？"),
        chunk.rfind("\n"),
        chunk.rfind(". "),
        chunk.rfind("! "),
        chunk.rfind("? "),
    )


def _next_boundary(chunk):
    hits = [
        index
        for index in (
            chunk.find("。"),
            chunk.find("！"),
            chunk.find("？"),
            chunk.find("\n"),
            chunk.find(". "),
            chunk.find("! "),
            chunk.find("? "),
        )
        if index >= 0
    ]
    return min(hits) if hits else -1


def clip_at_sentence(value, limit=ABSTRACT_STORE_LIMIT, empty=""):
    """Keep ``value`` intact unless it exceeds ``limit``.

    Hard mid-string slices are what produced truncated FastInsight/FastResearch
    abstracts. Prefer the last sentence boundary inside the budget; if that
    would drop most of the text, finish the current sentence slightly past the
    limit instead of cutting it open.
    """
    text = normalize_ws(value)
    if not text:
        return empty
    if limit is None or limit <= 0 or len(text) <= limit:
        return text

    min_keep = max(24, limit // 3)
    last = _last_boundary(text[:limit])
    if last >= min_keep:
        return text[: last + 1].strip()

    overflow_limit = min(len(text), limit + max(120, min(400, max(limit // 5, 1))))
    nxt = _next_boundary(text[limit:overflow_limit])
    if nxt >= 0:
        return text[: limit + nxt + 1].strip()
    if len(text) <= overflow_limit:
        return text
    return text[:limit].rstrip() + "……"


def compose_research_summary(abstract, trend_summary, limit=ABSTRACT_STORE_LIMIT):
    """Join official abstract + trend without mid-sentence truncation.

    The abstract is kept first. If the combined payload would exceed ``limit``,
    the abstract is shortened at a sentence boundary so the trend still fits.
    """
    trend = normalize_ws(trend_summary)
    if trend:
        reserved = min(len(trend) + 6, max(0, limit // 3))
        abstract = clip_at_sentence(abstract, max(200, limit - reserved - 4))
    else:
        abstract = clip_at_sentence(abstract, limit)
    if abstract and trend:
        return f"{abstract}\n\n趋势：{trend}"
    return abstract or clip_at_sentence(trend, limit)


def prefer_abstract(target, source):
    """Fill ``target`` with a higher-priority abstract from ``source``.

    Official paper abstracts outrank LLM/search summaries. Existing paper
    abstracts are never replaced by snippets.
    """
    if not isinstance(target, dict) or not isinstance(source, dict):
        return target
    incoming = normalize_ws(source.get("abstract"))
    if not incoming:
        return target
    incoming_source = str(source.get("abstract_source") or "").strip()
    current = normalize_ws(target.get("abstract"))
    current_source = str(target.get("abstract_source") or "").strip()
    incoming_rank = _SOURCE_RANK.get(incoming_source, 1)
    current_rank = _SOURCE_RANK.get(current_source, 1 if current else 0)
    if not current or incoming_rank > current_rank:
        target["abstract"] = incoming
        target["abstract_source"] = incoming_source or current_source or "search"
    elif current and not current_source and incoming_source:
        target["abstract_source"] = incoming_source
    return target


if __name__ == "__main__":
    long_en = "This is sentence one. This is sentence two. This is sentence three. leftover"
    clipped_en = clip_at_sentence(long_en, 70)
    assert clipped_en.endswith(".")
    assert "sentence two" in clipped_en
    assert "leftover" not in clipped_en
    zh = "这是第一句。这是第二句。这是第三句。多余内容"
    clipped_zh = clip_at_sentence(zh, 14)
    assert clipped_zh.endswith("。")
    assert "多余内容" not in clipped_zh
    overflow = "A" * 30 + " ends here. leftover"
    assert clip_at_sentence(overflow, 32).endswith(".")
    assert "leftover" not in clip_at_sentence(overflow, 32)
    summary = compose_research_summary(
        "Official abstract. Another sentence.",
        "趋势很好。",
        limit=80,
    )
    assert "Official abstract." in summary
    assert "趋势：" in summary
    paper = {"abstract": "snippet", "abstract_source": "search"}
    prefer_abstract(
        paper,
        {"abstract": "The official abstract.", "abstract_source": "paper"},
    )
    assert paper["abstract"] == "The official abstract."
    assert paper["abstract_source"] == "paper"
    prefer_abstract(
        paper,
        {"abstract": "a search snippet", "abstract_source": "search"},
    )
    assert paper["abstract_source"] == "paper"
    print("text_clip self-check ok")
