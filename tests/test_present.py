def test_ago_reads_like_a_person_would_say_it():
    from dayfile.web.present import ago

    now = "2026-11-12T12:00:00Z"
    cases = {
        "2026-11-12T11:59:40Z": "just now",
        "2026-11-12T11:40:00Z": "20 min ago",
        "2026-11-12T09:00:00Z": "3 h ago",
        "2026-11-11T09:00:00Z": "yesterday",
        "2026-11-07T09:00:00Z": "5 d ago",
        "2026-09-01T09:00:00Z": "2026-09-01",
    }
    for stored, shown in cases.items():
        assert ago(stored, now) == shown
    assert ago("not a time", now) == "not a time"


def test_markdown_renders_a_small_subset_and_nothing_else():
    from dayfile.web.present import markdown

    out = str(markdown("# T\n\nA **b** and *i* and `c<d>`\n\n- x\n- y\n\n1. p\n2. q\n\n> quote"))
    assert "<h3>T</h3>" in out and "<strong>b</strong>" in out and "<em>i</em>" in out
    assert "<code>c&lt;d&gt;</code>" in out and "<ul><li>x</li><li>y</li></ul>" in out
    assert "<ol><li>p</li><li>q</li></ol>" in out and "<blockquote>quote</blockquote>" in out
    assert str(markdown("```\nraw <b>\n```")) == "<pre><code>raw &lt;b&gt;</code></pre>"
    assert str(markdown("one\ntwo")) == "<p>one<br>two</p>"
    assert str(markdown("")) == "" and str(markdown(None)) == ""


def test_markdown_cannot_add_markup_or_unsafe_links():
    from dayfile.web.present import markdown

    out = str(markdown("<script>alert(1)</script> <img src=x onerror=y> [a](javascript:alert(1))"))
    assert "<script" not in out and "<img" not in out and "<a " not in out
    assert "&lt;script&gt;" in out
    good = str(markdown('[ok](https://x.example/?a=1&b="2")'))
    assert 'href="https://x.example/?a=1&amp;b=&quot;2&quot;"' in good
    assert 'rel="noopener noreferrer nofollow"' in good
    assert "<a " not in str(markdown("[x](data:text/html,<b>)")) and "<a " not in str(
        markdown("[x](//evil.example)")
    )


def test_markdown_keeps_every_line_when_kinds_are_mixed_without_blank_lines():
    from dayfile.web.present import markdown

    out = str(markdown("# Heading\nSome *text*\n- a\n- b\nmore\n> q"))
    assert out == (
        "<h3>Heading</h3><p>Some <em>text</em></p><ul><li>a</li><li>b</li></ul>"
        "<p>more</p><blockquote>q</blockquote>"
    )
    assert str(markdown("## Two")) == "<h4>Two</h4>"
    assert str(markdown("#nospace")) == "<p>#nospace</p>"
