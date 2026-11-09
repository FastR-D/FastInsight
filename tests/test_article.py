#!/usr/bin/env python3
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from fastinsight.article import (
    clean_share_text,
    extract_identifiers,
    extract_paper_from_article,
    parse_article_html,
)
from fastinsight.parse import (
    interesting_payload,
    is_social_article_url,
    parse_feishu_message,
    preferred_url,
)
from fastinsight.pipeline import paper_from_url

WECHAT_HTML = """
<html>
<head>
<title>Transformer 论文解读</title>
<meta property="og:title" content="Transformer 论文解读">
<meta property="og:description" content="介绍 Attention Is All You Need">
</head>
<body>
<h1 id="activity-name">Transformer 论文解读</h1>
<div id="js_content" class="rich_media_content">
<p>本文介绍论文 Attention Is All You Need。</p>
<p>预印本：https://arxiv.org/abs/1706.03762</p>
<p>DOI: 10.5555/3295222.3295349</p>
</div>
</body>
</html>
"""

XHS_HTML = """
<html>
<head>
<meta property="og:title" content="今天读的论文">
<meta property="og:description" content="Attention Is All You Need">
<script>
window.__INITIAL_STATE__={"note":{"noteDetailMap":{"n1":{"note":{"noteId":"n1","title":"今天读的论文","desc":"推荐 arXiv:1706.03762 Attention Is All You Need"}}}}}
</script>
</head>
<body>小红书</body>
</html>
"""


class ArticleParseTests(unittest.TestCase):
    def test_wechat_html_finds_arxiv_and_doi(self) -> None:
        article = parse_article_html("https://mp.weixin.qq.com/s/abc", WECHAT_HTML)
        self.assertEqual(article["source_kind"], "wechat")
        self.assertEqual(article["title"], "Transformer 论文解读")
        self.assertIn("Attention Is All You Need", article["text"])
        paper = extract_paper_from_article("https://mp.weixin.qq.com/s/abc", html=WECHAT_HTML)
        self.assertEqual(paper["arxiv_id"], "1706.03762")
        self.assertEqual(paper["doi"], "10.5555/3295222.3295349")
        self.assertIn("arxiv.org/abs/1706.03762", paper["url"])
        self.assertTrue(paper["paper_found"])

    def test_xiaohongshu_initial_state(self) -> None:
        article = parse_article_html("https://www.xiaohongshu.com/explore/n1", XHS_HTML)
        self.assertEqual(article["source_kind"], "xiaohongshu")
        self.assertEqual(article["title"], "今天读的论文")
        self.assertIn("1706.03762", article["text"])
        paper = extract_paper_from_article(
            "https://www.xiaohongshu.com/explore/n1", html=XHS_HTML
        )
        self.assertEqual(paper["arxiv_id"], "1706.03762")
        self.assertIn("arxiv.org", paper["url"])

    def test_clean_share_boilerplate(self) -> None:
        text = "张三发布了一篇小红书笔记，快来看吧！ 复制后打开【小红书】查看笔记"
        self.assertEqual(clean_share_text(text), "")
        self.assertEqual(clean_share_text("Attention Is All You Need"), "Attention Is All You Need")

    def test_identifiers_from_blob(self) -> None:
        found = extract_identifiers("see arXiv:1706.03762 and https://doi.org/10.1000/xyz")
        self.assertEqual(found["arxiv_id"], "1706.03762")
        self.assertEqual(found["doi"], "10.1000/xyz")

    def test_body_identifiers_produce_canonical_arxiv_url(self) -> None:
        for arxiv_id in ("1409.0473", "1409.0473v2", "1706.03762", "1706.03762v2"):
            for label in ("", "arXiv:"):
                with self.subTest(arxiv_id=arxiv_id, label=label):
                    html = (
                        '<html><title>论文介绍</title><div id="js_content">'
                        f"本文介绍预印本 {label}{arxiv_id} 的研究成果。"
                        "</div></html>"
                    )
                    llm = Mock(return_value=None)
                    with patch("fastinsight.article.fetch_html") as fetch:
                        paper = extract_paper_from_article(
                            "https://mp.weixin.qq.com/s/abc", html=html, llm=llm
                        )
                    self.assertEqual(paper["arxiv_id"], arxiv_id)
                    self.assertEqual(paper["url"], f"https://arxiv.org/abs/{arxiv_id}")
                    self.assertTrue(paper["paper_found"])
                    fetch.assert_not_called()
                    llm.assert_not_called()

    def test_body_doi_still_takes_priority_over_arxiv(self) -> None:
        html = (
            '<html><title>论文介绍</title><div id="js_content">'
            "arXiv:1409.0473v2 DOI: 10.1000/ABC;"
            "</div></html>"
        )
        paper = extract_paper_from_article("https://mp.weixin.qq.com/s/abc", html=html)
        self.assertEqual(paper["arxiv_id"], "1409.0473v2")
        self.assertEqual(paper["doi"], "10.1000/abc")
        self.assertEqual(paper["url"], "https://doi.org/10.1000/abc")
        self.assertTrue(paper["paper_found"])

    def test_existing_pdf_url_still_takes_priority_over_doi(self) -> None:
        url = "https://arxiv.org/pdf/1409.0473v2.pdf"
        html = (
            '<html><title>论文介绍</title><div id="js_content">'
            f'<a href="{url}">论文</a> DOI: 10.1000/ABC;'
            "</div></html>"
        )
        paper = extract_paper_from_article("https://mp.weixin.qq.com/s/abc", html=html)
        self.assertEqual(paper["arxiv_id"], "1409.0473v2")
        self.assertEqual(paper["doi"], "10.1000/abc")
        self.assertEqual(paper["url"], url)
        self.assertTrue(paper["paper_found"])


class PaperFromUrlTests(unittest.TestCase):
    def test_arxiv_url_is_unchanged(self) -> None:
        paper = paper_from_url("https://arxiv.org/abs/1706.03762", "Attention Is All You Need")
        self.assertEqual(paper["title"], "Attention Is All You Need")
        self.assertEqual(paper["url"], "https://arxiv.org/abs/1706.03762")

    def test_wechat_url_reads_article(self) -> None:
        with patch("fastinsight.article.fetch_html", return_value=(WECHAT_HTML, "https://mp.weixin.qq.com/s/abc")):
            paper = paper_from_url("https://mp.weixin.qq.com/s/abc")
        self.assertEqual(paper["arxiv_id"], "1706.03762")
        self.assertIn("arxiv.org/abs/1706.03762", paper["url"])
        self.assertEqual(paper["source_url"], "https://mp.weixin.qq.com/s/abc")
        self.assertEqual(paper["source_kind"], "wechat")
        self.assertIn("Transformer", paper["title"])

    def test_xhs_share_text_is_not_used_as_title(self) -> None:
        share = "李四发布了一篇小红书笔记，快来看吧！ http://xhslink.com/o/abc 复制后打开【小红书】查看笔记"
        with patch(
            "fastinsight.article.fetch_html",
            return_value=(XHS_HTML, "https://www.xiaohongshu.com/explore/n1"),
        ):
            paper = paper_from_url("http://xhslink.com/o/abc", share)
        self.assertEqual(paper["arxiv_id"], "1706.03762")
        self.assertNotIn("小红书", paper["title"])


class SocialLinkParseTests(unittest.TestCase):
    def test_social_hosts(self) -> None:
        self.assertTrue(is_social_article_url("https://mp.weixin.qq.com/s/abc"))
        self.assertTrue(is_social_article_url("https://www.xiaohongshu.com/explore/n1"))
        self.assertTrue(is_social_article_url("http://xhslink.com/o/abc"))
        self.assertFalse(is_social_article_url("https://arxiv.org/abs/1706.03762"))

    def test_prefer_arxiv_over_wechat(self) -> None:
        urls = [
            "https://mp.weixin.qq.com/s/abc",
            "https://arxiv.org/abs/1706.03762",
        ]
        self.assertEqual(preferred_url(urls), "https://arxiv.org/abs/1706.03762")

    def test_wechat_message(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_wx",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"https://mp.weixin.qq.com/s/abc123"}',
                "sender": {"sender_type": "user"},
            }
        )
        payload = interesting_payload(parsed)
        assert payload is not None
        self.assertEqual(payload["kind"], "url")
        self.assertEqual(payload["url"], "https://mp.weixin.qq.com/s/abc123")

    def test_xhs_post_href(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_xhs",
                "chat_id": "oc_abc",
                "msg_type": "post",
                "content": '{"title":"笔记","content":[[{"tag":"a","href":"http://xhslink.com/o/xyz","text":"打开笔记"}]]}',
                "sender": {"sender_type": "user"},
            }
        )
        payload = interesting_payload(parsed)
        assert payload is not None
        self.assertEqual(payload["url"], "http://xhslink.com/o/xyz")

    def test_unescape_wechat_query(self) -> None:
        parsed = parse_feishu_message(
            {
                "message_id": "om_amp",
                "chat_id": "oc_abc",
                "msg_type": "text",
                "content": '{"text":"https://mp.weixin.qq.com/s?__biz=MzA&amp;mid=123"}',
                "sender": {"sender_type": "user"},
            }
        )
        self.assertEqual(parsed.urls[0], "https://mp.weixin.qq.com/s?__biz=MzA&mid=123")


class ArticleFallbackTests(unittest.TestCase):
    def test_llm_extracts_paper_when_no_identifier(self) -> None:
        html = """
        <html><head><title>重磅论文</title></head>
        <body>
        <h1 id="activity-name">重磅论文</h1>
        <div id="js_content">谷歌团队提出了 Transformer 架构，论文名为 Attention Is All You Need。</div>
        </body></html>
        """

        def fake_llm(text: str, title: str):
            self.assertIn("Transformer", text)
            return {
                "title": "Attention Is All You Need",
                "url": "https://arxiv.org/abs/1706.03762",
                "arxiv_id": "1706.03762",
            }

        paper = extract_paper_from_article(
            "https://mp.weixin.qq.com/s/no-id", html=html, llm=fake_llm
        )
        self.assertEqual(paper["title"], "Attention Is All You Need")
        self.assertEqual(paper["arxiv_id"], "1706.03762")
        self.assertIn("arxiv.org/abs/1706.03762", paper["url"])

    def test_jina_fallback_on_wechat_block(self) -> None:
        blocked = "<html><body>环境异常，请完成验证后即可继续</body></html>"
        jina = (
            "Title: Transformer 论文解读\n\n"
            "URL Source: https://mp.weixin.qq.com/s/abc\n\n"
            "Markdown Content:\n"
            "Attention Is All You Need https://arxiv.org/abs/1706.03762\n"
        )
        with patch("fastinsight.article.fetch_html", return_value=(blocked, "https://mp.weixin.qq.com/s/abc")), patch(
            "fastinsight.article.fetch_via_jina", return_value=jina
        ):
            paper = extract_paper_from_article("https://mp.weixin.qq.com/s/abc")
        self.assertEqual(paper["arxiv_id"], "1706.03762")
        self.assertIn("Transformer", paper["article_title"] or paper["title"])


if __name__ == "__main__":
    unittest.main()
