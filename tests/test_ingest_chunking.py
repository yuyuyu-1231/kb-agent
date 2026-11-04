"""app/services/ingest.py 的清洗与分块测试。

这里测的全是纯函数，不需要真实 PDF —— 用构造的 Block 验证规则。
最后有一个集成测试，只在样例 PDF 存在时才跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.ingest import (
    Block,
    assign_sections,
    chunk_blocks,
    detect_margin_keys,
    drop_margins,
    is_heading,
    merge_broken_lines,
    parse_pdf,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PDF = ROOT / "data" / "corpus" / "员工手册.pdf"


def blk(page: int, y0: float, y1: float, text: str, x0: float = 72.0) -> Block:
    return Block(page_no=page, x0=x0, y0=y0, y1=y1, text=text)


# --------------------------------------------------------------------------- #
# 页眉页脚检测
# --------------------------------------------------------------------------- #

CHAPTER_TITLES = [
    "第一章  总则",
    "第三章  假期制度",
    "第五章  费用报销",
    "第七章  考勤管理",
    "第九章  附则",
    "第十一章  其他",
]


def test_detect_margin_keys_finds_header_and_footer() -> None:
    page_height = 842.0
    blocks: list[Block] = []
    for p in range(1, 7):
        blocks.append(blk(p, 46.5, 56.7, "示例科技有限公司 · 员工手册（2026 版）"))
        blocks.append(blk(p, 791.5, 801.7, f"- 第 {p} 页 / 共 6 页 -"))
        # 各页在同一 y 位置有不同章节标题：不能因为位置相同就误杀
        blocks.append(blk(p, 95.0, 113.0, CHAPTER_TITLES[p - 1]))
        blocks.append(blk(p, 200.0, 212.0, f"第 {p} 页的正文内容。"))

    bad = detect_margin_keys(blocks, page_height)

    assert 45 in bad, "页眉应该被识别"
    assert 790 in bad, "页脚应该被识别"
    assert 95 not in bad, "y 相同但内容不同的章节标题不能被误杀"


def test_detect_margin_requires_enough_pages() -> None:
    """只有 2 页时无法判断"重复出现"，应该放弃检测。"""
    blocks = [
        blk(1, 46.5, 56.7, "页眉"),
        blk(2, 46.5, 56.7, "页眉"),
    ]
    assert detect_margin_keys(blocks, 842.0) == set()


def test_drop_margins() -> None:
    blocks = [
        blk(1, 46.5, 56.7, "页眉"),
        blk(1, 200.0, 212.0, "正文"),
    ]
    kept, dropped = drop_margins(blocks, {45})
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0].text == "正文"


# --------------------------------------------------------------------------- #
# 断行合并
# --------------------------------------------------------------------------- #

def test_merge_chinese_line_break_without_space() -> None:
    blocks = [
        blk(1, 166.5, 179.1, "本手册适用于与公司签订劳动合同的全体员工，另有约定的从"),
        blk(1, 182.3, 194.9, "其约定。"),
    ]
    merged = merge_broken_lines(blocks)
    assert len(merged) == 1
    assert merged[0].text == "本手册适用于与公司签订劳动合同的全体员工，另有约定的从其约定。"
    assert "从 其" not in merged[0].text, "中文之间不能加空格"


def test_merge_english_line_break_with_space() -> None:
    blocks = [
        blk(1, 100.0, 112.0, "The annual leave policy is"),
        blk(1, 115.0, 127.0, "described in this section."),
    ]
    merged = merge_broken_lines(blocks)
    assert len(merged) == 1
    assert merged[0].text == "The annual leave policy is described in this section."


def test_no_merge_after_sentence_end() -> None:
    blocks = [
        blk(1, 100.0, 112.0, "第一段结束了。"),
        blk(1, 115.0, 127.0, "第二段开始。"),
    ]
    merged = merge_broken_lines(blocks)
    assert len(merged) == 2


def test_no_merge_across_pages() -> None:
    blocks = [
        blk(1, 700.0, 712.0, "上一页末尾没有句号"),
        blk(2, 100.0, 112.0, "下一页开头"),
    ]
    merged = merge_broken_lines(blocks)
    assert len(merged) == 2


def test_no_merge_when_gap_large() -> None:
    blocks = [
        blk(1, 100.0, 112.0, "上一行没有句号"),
        blk(1, 160.0, 172.0, "隔了很远的一行"),
    ]
    merged = merge_broken_lines(blocks)
    assert len(merged) == 2


def test_no_merge_when_indent_differs() -> None:
    """左边界差很多，说明是新的列表项或缩进段落。"""
    blocks = [
        blk(1, 100.0, 112.0, "项目符号下的内容", x0=100.0),
        blk(1, 115.0, 127.0, "另一个缩进层级", x0=140.0),
    ]
    merged = merge_broken_lines(blocks)
    assert len(merged) == 2


# --------------------------------------------------------------------------- #
# 标题识别
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("第三章  假期制度", True),
    ("3.1  年休假", True),
    ("1.1", True),
    ("第一章", True),
    ("本手册自 2026 年 1 月 1 日起施行。", False),
    ("这是普通的一段正文，既没有章节编号也不短。", False),
    ("3.1  这是一个特别特别长的标题" * 3, False),      # 过长
    ("第三章  假期制度 ............................ 4", False),   # 目录行
    ("第七章  考勤管理 ........ 6", False),                       # 目录行
])
def test_is_heading(text: str, expected: bool) -> None:
    assert is_heading(text) is expected


def test_consecutive_headings_stay_with_body() -> None:
    """连续两个标题 + 正文应该合成一块，而不是产出只有标题的碎片。"""
    blocks = [
        blk(1, 100.0, 112.0, "第一章  总则"),
        blk(1, 120.0, 132.0, "1.1  适用范围"),
        blk(1, 140.0, 152.0, "本手册适用于全体员工。"),
    ]
    assign_sections(blocks)
    chunks = chunk_blocks(blocks, chunk_size=1000, overlap=0)

    assert len(chunks) == 1, "标题不应单独成块"
    assert "本手册适用于全体员工。" in chunks[0]["text"]
    assert chunks[0]["section_title"] == "1.1  适用范围", "section 取最近的标题"


def test_heading_breaks_when_body_precedes() -> None:
    """前面已有正文时，新标题必须开启新 chunk。"""
    blocks = [
        blk(1, 100.0, 112.0, "第一章  总则"),
        blk(1, 120.0, 132.0, "总则的正文内容。"),
        blk(1, 140.0, 152.0, "3.1  年休假"),
        blk(1, 160.0, 172.0, "年休假的正文内容。"),
    ]
    assign_sections(blocks)
    chunks = chunk_blocks(blocks, chunk_size=1000, overlap=0)

    assert len(chunks) == 2
    assert chunks[0]["section_title"] == "第一章  总则"
    assert chunks[1]["section_title"] == "3.1  年休假"


def test_assign_sections_propagates() -> None:
    blocks = [
        blk(1, 95.0, 113.0, "第三章  假期制度"),
        blk(1, 142.5, 155.1, "3.1  年休假"),
        blk(1, 166.5, 179.1, "员工转正后可申请年休假。"),
        blk(1, 200.0, 212.0, "3.2  病假"),
        blk(1, 220.0, 232.0, "病假须提供证明。"),
    ]
    sections = assign_sections(blocks)

    assert sections == ["第三章  假期制度", "3.1  年休假", "3.2  病假"]
    assert blocks[2].section_title == "3.1  年休假"
    assert blocks[4].section_title == "3.2  病假"
    assert blocks[0].is_heading is True
    assert blocks[2].is_heading is False


# --------------------------------------------------------------------------- #
# 分块
# --------------------------------------------------------------------------- #

def test_chunk_breaks_on_heading() -> None:
    """标题必须开启新 chunk，否则一块里会混进两个不相关的主题。"""
    blocks = [
        blk(1, 100.0, 112.0, "第三章  假期制度"),
        blk(1, 120.0, 132.0, "甲" * 100),
        blk(1, 140.0, 152.0, "3.1  年休假"),
        blk(1, 160.0, 172.0, "乙" * 100),
    ]
    assign_sections(blocks)

    chunks = chunk_blocks(blocks, chunk_size=1000, overlap=0)

    assert len(chunks) == 2
    assert chunks[0]["section_title"] == "第三章  假期制度"
    assert "甲" in chunks[0]["text"]
    assert chunks[1]["section_title"] == "3.1  年休假"
    assert "乙" in chunks[1]["text"]


def test_chunk_respects_size() -> None:
    blocks = [blk(1, 100.0 + i * 20, 112.0 + i * 20, "内容" * 50) for i in range(10)]
    chunks = chunk_blocks(blocks, chunk_size=200, overlap=0)

    assert len(chunks) > 1
    for c in chunks:
        assert len(c["text"]) <= 250, "单块不应明显超过 chunk_size"


def test_chunk_no_content_lost() -> None:
    """所有正文都必须出现在某个 chunk 里（overlap 会重复，但不会丢）。"""
    texts = [f"第{i}段内容。" for i in range(8)]
    blocks = [blk(1, 100.0 + i * 20, 112.0 + i * 20, t) for i, t in enumerate(texts)]
    chunks = chunk_blocks(blocks, chunk_size=50, overlap=0)

    joined = "".join(c["text"] for c in chunks)
    for t in texts:
        assert t in joined


def test_chunk_overlap_repeats_tail() -> None:
    """overlap > 0 时尾部内容会重复出现在下一块开头。

    chunk_size 必须明显小于总长度，否则只会切出一块（没有可重叠的边界）。
    6 段 x 5 字 = 30 字，取 chunk_size=12 才能切出多块。
    """
    blocks = [blk(1, 100.0 + i * 20, 112.0 + i * 20, "句子内容。") for i in range(6)]
    chunks = chunk_blocks(blocks, chunk_size=12, overlap=5)

    assert len(chunks) >= 2
    # 有 overlap 时，总字符数应大于原文总长度
    total_chars = sum(len(c["text"]) for c in chunks)
    original = sum(len(b.text) for b in blocks)
    assert total_chars > original


def test_chunk_breaks_on_page_change() -> None:
    """跨页必须切分：chunk 只有一个 page_no，跨页会让页码溯源失真。"""
    blocks = [
        blk(1, 700.0, 712.0, "第一页的最后一句话。"),
        blk(2, 100.0, 112.0, "第二页的第一句话。"),
    ]
    chunks = chunk_blocks(blocks, chunk_size=1000, overlap=100)

    assert len(chunks) == 2
    assert chunks[0]["page_no"] == 1
    assert chunks[1]["page_no"] == 2
    assert "第二页" not in chunks[0]["text"], "不能把下一页内容带进上一页的 chunk"


def test_chunk_empty_input() -> None:
    assert chunk_blocks([]) == []


def test_chunk_index_sequential() -> None:
    blocks = [blk(1, 100.0 + i * 20, 112.0 + i * 20, "内容" * 50) for i in range(6)]
    chunks = chunk_blocks(blocks, chunk_size=150, overlap=0)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


# --------------------------------------------------------------------------- #
# 集成：解析真实样例 PDF
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="样例句料未生成")
def test_parse_sample_pdf() -> None:
    chunks, result = parse_pdf(SAMPLE_PDF)

    assert result.pages == 6
    assert result.chunks == len(chunks)
    assert result.chunks > 0
    assert result.dropped_margin_blocks > 0, "页眉页脚必须被剔除"

    # 页眉不能出现在任何 chunk 里
    for c in chunks:
        assert "示例科技有限公司 · 员工手册" not in c["text"]

    # 页脚（页码）也不能出现
    for c in chunks:
        assert "/ 共 6 页 -" not in c["text"]

    # 章节被识别到
    joined_sections = " ".join(result.sections)
    assert "第三章" in joined_sections
    assert "第五章" in joined_sections

    # 断行被合并：不该出现以"从"结尾又紧接"其约定"的碎片
    all_text = "".join(c["text"] for c in chunks)
    assert "另有约定的从其约定" in all_text
