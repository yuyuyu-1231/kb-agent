"""app/services/ingest.py — PDF 摄入：解析 -> 清洗 -> 分块。

这是全链路里最"脏"的一段，因为 PDF 没有语义结构，只有坐标。
下面每一步都是被真实数据逼出来的，不是设计出来的：

1. **页眉页脚必须剔除**
   每页都有的"公司名 + 文档名"和"- 第 N 页 -"，会进入每个 chunk。
   后果有两个：一是污染检索（任何 query 都可能匹配到页眉的字），
   二是浪费 token。

   检测不能只看 y 坐标。实测发现：各页的章节标题也在同一个 y 位置
   （因为版式固定），只看位置会把标题误删。所以必须
   **位置在上下 10% 区域 + 去掉数字后内容高度重复**，两个条件同时满足。

2. **被切碎的行必须合并回去**
   pymupdf 的 get_text("blocks") 在行距偏大时会把一段话切成多个 block：
       y=[166.5,179.1] 本手册适用于与公司签订劳动合同的全体员工……另有约定的从
       y=[182.3,194.9] 其约定。
   不合并的话，chunk 会是半句话，检索和生成都会受影响。

   合并规则：同一页 + x0 接近 + 行间距小 + 上一块不是句末标点。
   拼接时中文之间不加空格（否则会出现"其约定 。"这种），
   只有两边都是 ASCII 字母数字时才加空格。

3. **章节标题要跟着 chunk 走**
   chunk 的 payload 里有 section_title，第 4 周做"标题感知分块"、
   以及答案里说"依据第三章 3.2 节"都要用它。
   v1 只做"遇到标题就强制开新 chunk"，不做更复杂的标题层级树。

4. **块内不打断语义**
   分块按"完整段落"聚合，不按字符硬切。宁可某个 chunk 略超 chunk_size，
   也不要把一句话从中间切开 —— 后者会让 embedding 语义偏移。
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pymupdf

# 章节标题：第一章 / 第 1 章 / 一、
CHAPTER_RE = re.compile(r"^第\s*[一二三四五六七八九十百零\d]+\s*章")
# 小节标题：1.1 / 3.2.1
SECTION_RE = re.compile(r"^\d+\.\d+(\s|$)")
# 句子结束标点，用来判断"这块结束了吗"
SENTENCE_END = ("。", "！", "？", "；", "：", ".", "!", "?", ";", ":")
# 目录行：章节名后面跟一串点号再跟页码，例如 "第三章  假期制度 ........ 4"
# 这种行也以 "第X章" 开头，不排除的话会被误判成真标题。
DOT_LEADER_RE = re.compile(r"[.．·]{3,}")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Block:
    """一个文本块。坐标来自 PDF，doc_offset 是它全文里的字符位置。"""
    page_no: int
    x0: float
    y0: float
    y1: float
    text: str
    section_title: str = ""
    is_heading: bool = False
    doc_offset: int = 0

    def __post_init__(self) -> None:
        self.text = self.text.strip()


@dataclass
class IngestResult:
    doc_id: str
    doc_name: str
    pages: int
    blocks: int
    chunks: int
    dropped_margin_blocks: int
    sections: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "pages": self.pages,
            "blocks": self.blocks,
            "chunks": self.chunks,
            "dropped_margin_blocks": self.dropped_margin_blocks,
            "sections": self.sections,
            "elapsed_ms": self.elapsed_ms,
        }


# --------------------------------------------------------------------------- #
# 第 1 步：提取
# --------------------------------------------------------------------------- #

def extract_blocks(pdf_path: Path) -> tuple[list[Block], int, float]:
    """逐页提取文本块。返回 (blocks, 页数, 页高)。

    用 get_text("blocks") 而不是 get_text("text")：
    - text 模式把整页拼成一个字符串，页码/页眉混在正文里没法剔除
    - blocks 模式保留每个块的坐标，才有机会按位置过滤
    """
    doc = pymupdf.open(str(pdf_path))
    try:
        page_count = doc.page_count
        page_height = doc[0].rect.height if page_count else 842.0

        blocks: list[Block] = []
        offset = 0
        for page_idx in range(page_count):
            page = doc[page_idx]
            for raw in page.get_text("blocks"):
                x0, y0, x1, y1, text, _block_no, block_type = raw
                if block_type != 0:      # 0 = 文本块，1 = 图片
                    continue
                block = Block(
                    page_no=page_idx + 1,
                    x0=float(x0),
                    y0=float(y0),
                    y1=float(y1),
                    text=str(text),
                    doc_offset=offset,
                )
                if not block.text:
                    continue
                blocks.append(block)
                offset += len(block.text)
        return blocks, page_count, float(page_height)
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 第 2 步：剔除页眉页脚
# --------------------------------------------------------------------------- #

def _normalize_for_compare(text: str) -> str:
    """把数字替换掉，用来比较"是不是同一行模板"。

    "- 第 3 页 / 共 6 页 -" 和 "- 第 4 页 / 共 6 页 -" 归一化后相同，
    而"第三章 假期制度"和"第五章 费用报销"归一化后仍不同。
    """
    return re.sub(r"\d+", "#", text.strip())


def detect_margin_keys(
    blocks: list[Block],
    page_height: float,
    *,
    band_ratio: float = 0.10,
    page_ratio: float = 0.80,
) -> set[int]:
    """找出页眉页脚所在的 y 位置（按 5pt 粒度取整）。

    两个条件同时满足才判定：
    1. 位置落在页面上下 band_ratio 区域内（默认各 10%）
    2. 该位置上的文本，去掉数字后，在 page_ratio 以上的页里重复出现

    只用条件 1 会误杀固定的章节标题（实测踩过）；只用条件 2 会误杀
    每页重复出现的正文模板。两个一起才稳。
    """
    if not blocks:
        return set()

    page_numbers = {b.page_no for b in blocks}
    total_pages = len(page_numbers)
    if total_pages < 3:
        # 页数太少，"重复出现"没有统计意义
        return set()

    top_limit = page_height * band_ratio
    bottom_limit = page_height * (1.0 - band_ratio)

    candidates: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for b in blocks:
        in_top = b.y0 < top_limit
        in_bottom = b.y1 > bottom_limit
        if not (in_top or in_bottom):
            continue
        key = int(round(b.y0 / 5.0) * 5)
        candidates[key].append((b.page_no, b.text))

    bad: set[int] = set()
    for key, items in candidates.items():
        pages_hit = len({p for p, _ in items})
        if pages_hit < total_pages * page_ratio:
            continue
        normalized = [_normalize_for_compare(t) for _, t in items]
        most_common_count = Counter(normalized).most_common(1)[0][1]
        if most_common_count >= pages_hit * page_ratio:
            bad.add(key)

    return bad


def drop_margins(blocks: list[Block], bad_keys: set[int]) -> tuple[list[Block], int]:
    """剔除页眉页脚块，返回 (保留的块, 剔除数量)。"""
    if not bad_keys:
        return blocks, 0
    kept: list[Block] = []
    dropped = 0
    for b in blocks:
        key = int(round(b.y0 / 5.0) * 5)
        if key in bad_keys:
            dropped += 1
            continue
        kept.append(b)
    return kept, dropped


# --------------------------------------------------------------------------- #
# 第 3 步：合并断行
# --------------------------------------------------------------------------- #

def _is_ascii_word(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "-_/")


def _join_text(left: str, right: str) -> str:
    """拼两段文字。

    中文换行处直接拼（PDF 里"其约定。"的"其"就是上一行的延续）；
    只有两边都是英文/数字时才补一个空格，否则 "work" + "flow" 会变成 "workflow"。
    """
    if not left:
        return right
    if not right:
        return left
    if _is_ascii_word(left[-1]) and _is_ascii_word(right[0]):
        return left + " " + right
    return left + right


def merge_broken_lines(blocks: list[Block], gap_tolerance: float = 8.0) -> list[Block]:
    """把被 PDF 切断的同一段合并回去。

    判定"是同一段"的四个条件：
    - 同一页
    - 左边界接近（版式一致，允许 6pt 误差）
    - 行间距小（y0 - 上块 y1 < gap_tolerance）
    - 上一块不是句末标点（否则是段落结束）
    """
    if not blocks:
        return []

    merged: list[Block] = [blocks[0]]
    for b in blocks[1:]:
        prev = merged[-1]
        same_page = b.page_no == prev.page_no
        aligned = abs(b.x0 - prev.x0) < 6.0
        close = (b.y0 - prev.y1) < gap_tolerance
        not_finished = not prev.text.endswith(SENTENCE_END)

        if same_page and aligned and close and not_finished:
            prev.text = _join_text(prev.text, b.text)
            prev.y1 = b.y1
        else:
            merged.append(b)
    return merged


# --------------------------------------------------------------------------- #
# 第 4 步：识别章节
# --------------------------------------------------------------------------- #

def is_heading(text: str) -> bool:
    """判断一个块是不是标题。

    三个条件：短、匹配章节编号格式、且不是目录行。
    - 只按长度判断会把短段落误判成标题
    - 只按格式判断会让带编号的长句被当成标题
    - 不排除目录行的话，"第三章  假期制度 ...... 4" 会被当成第三章的开头，
      产出一堆只有 40 字的垃圾 chunk（实测踩过）
    """
    t = text.strip()
    if not t or len(t) > 40:
        return False
    if "\n" in t:
        return False
    if DOT_LEADER_RE.search(t):
        return False
    return bool(CHAPTER_RE.match(t) or SECTION_RE.match(t))


def assign_sections(blocks: list[Block]) -> list[str]:
    """给每个块打上所属章节，返回出现过的章节列表（按顺序）。"""
    current = ""
    seen: list[str] = []
    for b in blocks:
        if is_heading(b.text):
            b.is_heading = True
            current = b.text.strip()
            if current not in seen:
                seen.append(current)
        b.section_title = current
    return seen


# --------------------------------------------------------------------------- #
# 第 5 步：分块
# --------------------------------------------------------------------------- #

def chunk_blocks(
    blocks: list[Block],
    *,
    chunk_size: int = 400,
    overlap: int = 80,
) -> list[dict[str, Any]]:
    """把块聚合成 chunk。

    三个规则：
    1. 遇到标题强制开新 chunk（v1 的"标题感知"）
    2. 累积长度超过 chunk_size 就切
    3. 切的时候保留尾部 overlap 字符作为下一块的开头（上下文不丢）

    刻意不做的事：不从句子中间切。宁可某块超长，也不破坏语义完整性 ——
    因为 embedding 对半句话的编码质量明显更差。
    """
    if not blocks:
        return []

    chunks: list[dict[str, Any]] = []
    buf: list[Block] = []
    buf_len = 0

    def flush(carry_overlap: bool = True) -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        text = ""
        for b in buf:
            text = _join_text(text, b.text)
        chunks.append({
            "text": text,
            "page_no": buf[0].page_no,
            # 取 buf 中最后一个块的章节：一个 chunk 里可能跨了标题，
            # 正文实际归属最近的那个标题
            "section_title": buf[-1].section_title,
            "chunk_index": len(chunks),
            "char_start": buf[0].doc_offset,
            "char_end": buf[-1].doc_offset + len(buf[-1].text),
        })

        if not carry_overlap or overlap <= 0:
            buf, buf_len = [], 0
            return

        # 保留尾部若干块做 overlap；标题块不参与重叠（它是新段落的起点）
        keep: list[Block] = []
        keep_len = 0
        for b in reversed(buf):
            if b.is_heading:
                break
            if keep_len + len(b.text) > overlap:
                break
            keep.insert(0, b)
            keep_len += len(b.text)
        buf, buf_len = keep, keep_len

    for b in blocks:
        # 换页强制切分：chunk 只有一个 page_no，跨页会让页码溯源失真。
        # 这里不保留 overlap —— 把上一页的尾巴带到下一页，页归属就说不清了。
        if buf and b.page_no != buf[-1].page_no:
            flush(carry_overlap=False)

        if b.is_heading and buf:
            # 只有当缓冲区里已经有正文时才切分。
            # 如果缓冲区里全是标题（连续两个标题），让它们和后续正文待在一块，
            # 否则会产生"只有标题、没有内容"的碎片 chunk（实测出现过 7-9 字符的块）。
            if any(not x.is_heading for x in buf):
                # carry_overlap=False：标题是新一节的起点，不需要带上一节的尾巴。
                # 之前写成 flush() + 手动清空 buf，导致 overlap 算完立刻丢弃 ——
                # 8 次无用计算，而且真实文档上膨胀比恒为 1.000（overlap 参数空转），
                # 是跑 trace_chunking.py 逐轮追踪时才发现的。
                flush(carry_overlap=False)

        if buf and buf_len + len(b.text) > chunk_size:
            flush()

        buf.append(b)
        buf_len += len(b.text)

    flush()
    return chunks


# --------------------------------------------------------------------------- #
# 完整流程
# --------------------------------------------------------------------------- #

def parse_pdf(
    pdf_path: Path,
    *,
    chunk_size: int = 400,
    overlap: int = 80,
) -> tuple[list[dict[str, Any]], IngestResult]:
    """解析 + 清洗 + 分块。不涉及 embedding 和入库。"""
    pdf_path = Path(pdf_path)
    blocks, pages, page_height = extract_blocks(pdf_path)

    bad_keys = detect_margin_keys(blocks, page_height)
    blocks, dropped = drop_margins(blocks, bad_keys)
    blocks = merge_broken_lines(blocks)
    sections = assign_sections(blocks)
    chunks = chunk_blocks(blocks, chunk_size=chunk_size, overlap=overlap)

    result = IngestResult(
        doc_id=pdf_path.stem,
        doc_name=pdf_path.name,
        pages=pages,
        blocks=len(blocks),
        chunks=len(chunks),
        dropped_margin_blocks=dropped,
        sections=sections,
    )
    return chunks, result


async def ingest_pdf(
    pdf_path: Path,
    *,
    embedder: Any,
    store: Any,
    chunk_size: int = 400,
    overlap: int = 80,
    doc_id: str | None = None,
) -> IngestResult:
    """完整摄入：解析 -> 嵌入 -> 入库。

    doc_id 默认用文件名（不含扩展名）。重新上传同一文档时，
    先按 doc_id 删除旧数据再写入，避免改名后残留。
    """
    import time

    started = time.perf_counter()
    pdf_path = Path(pdf_path)
    chunks, result = parse_pdf(pdf_path, chunk_size=chunk_size, overlap=overlap)

    if doc_id:
        result.doc_id = doc_id
    if not result.doc_id:
        result.doc_id = uuid.uuid4().hex[:12]

    if not chunks:
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    await store.ensure_collection()
    # 先清旧数据：同一文档重复上传时不残留旧的 chunk
    await store.delete_document(result.doc_id)

    vectors = await embedder.embed_passages([c["text"] for c in chunks])
    await store.upsert_chunks(
        doc_id=result.doc_id,
        doc_name=result.doc_name,
        chunks=chunks,
        vectors=vectors,
    )

    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result
