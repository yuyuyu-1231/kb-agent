r"""逐轮追踪 chunk_blocks 的执行过程，用真实 PDF 数据。

输出 E:\DevCaches\kb_chunk_trace.txt
目的：讲解时每个数字都有出处，不靠推演。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ingest import (  # noqa: E402
    _join_text,
    assign_sections,
    detect_margin_keys,
    drop_margins,
    extract_blocks,
    merge_broken_lines,
)

PDF = ROOT / "data" / "corpus" / "员工手册.pdf"
OUT = r"E:\DevCaches\kb_chunk_trace.txt"
CHUNK_SIZE = 400
OVERLAP = 80


class ChunkTracer:
    """完全复刻 chunk_blocks 的逻辑，只在关键节点插入日志。"""

    def __init__(self, chunk_size: int, overlap: int) -> None:
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.chunks: list[dict] = []
        self.buf: list = []
        self.buf_len = 0
        self.lines: list[str] = []

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    def dump(self) -> str:
        if not self.buf:
            return "(空)"
        head = " | ".join(b.text[:10].replace("\n", " ") for b in self.buf)
        return f"{len(self.buf)}块 / {self.buf_len}字 :: {head}"

    def flush(self, carry_overlap: bool = True, reason: str = "") -> None:
        if not self.buf:
            return

        text = ""
        for b in self.buf:
            text = _join_text(text, b.text)

        idx = len(self.chunks)
        self.chunks.append({
            "chunk_index": idx,
            "page_no": self.buf[0].page_no,
            "section_title": self.buf[-1].section_title,
            "text": text,
        })

        self.say(f"    >> flush({reason}) 产出 chunk[{idx}] "
                 f"p.{self.buf[0].page_no} len={len(text)} "
                 f"§{self.buf[-1].section_title[:14]}")
        self.say(f"       内容开头: {text[:36]}")

        if not carry_overlap or self.overlap <= 0:
            self.say(f"       carry_overlap={carry_overlap} overlap={self.overlap}"
                     f" -> buf 直接清空")
            self.buf, self.buf_len = [], 0
            return

        keep: list = []
        keep_len = 0
        for b in reversed(self.buf):
            if b.is_heading:
                self.say(f"       overlap 回溯遇到标题块，停止")
                break
            if keep_len + len(b.text) > self.overlap:
                self.say(f"       overlap 回溯: {keep_len}+{len(b.text)} > {self.overlap}，停止")
                break
            keep.insert(0, b)
            keep_len += len(b.text)
            self.say(f"       overlap 保留尾部块 len={len(b.text)} -> keep_len={keep_len}")

        self.buf, self.buf_len = keep, keep_len
        self.say(f"       buf 重置为 {self.dump()}")

    def run(self, blocks: list) -> None:
        self.say(f"参数: chunk_size={self.chunk_size}, overlap={self.overlap}")
        self.say(f"待处理块数: {len(blocks)}")
        self.say("")

        for i, b in enumerate(blocks):
            self.say(f"[块#{i:>2}] p.{b.page_no} heading={int(b.is_heading)} "
                     f"len={len(b.text):>3} | {b.text[:26].replace(chr(10), ' ')}")
            self.say(f"    进入前 buf = {self.dump()}")

            # 条件 1：换页
            if self.buf and b.page_no != self.buf[-1].page_no:
                self.say(f"    ** 条件1 换页: {self.buf[-1].page_no} -> {b.page_no}")
                self.flush(carry_overlap=False, reason="换页")

            # 条件 2：标题
            if b.is_heading and self.buf:
                if any(not x.is_heading for x in self.buf):
                    self.say("    ** 条件2 标题切分（buf 里有正文）")
                    self.flush(carry_overlap=False, reason="标题")
                else:
                    self.say("    -- 是标题但 buf 里全是标题，不切分")

            # 条件 3：超长
            if self.buf and self.buf_len + len(b.text) > self.chunk_size:
                self.say(f"    ** 条件3 超长: {self.buf_len}+{len(b.text)} > {self.chunk_size}")
                self.flush(carry_overlap=True, reason="超长")

            self.buf.append(b)
            self.buf_len += len(b.text)
            self.say(f"    加入后 buf = {self.dump()}")
            self.say("")

        self.say("[循环结束] 收尾 flush")
        self.flush(carry_overlap=True, reason="收尾")
        self.say("")


def main() -> None:
    blocks, pages, page_height = extract_blocks(PDF)
    bad = detect_margin_keys(blocks, page_height)
    blocks, dropped = drop_margins(blocks, bad)
    blocks = merge_broken_lines(blocks)
    sections = assign_sections(blocks)

    tracer = ChunkTracer(CHUNK_SIZE, OVERLAP)
    tracer.say("=" * 78)
    tracer.say("chunk_blocks 逐轮追踪")
    tracer.say("=" * 78)
    tracer.say(f"PDF: {PDF.name}  页数={pages}")
    tracer.say(f"提取块数(清洗前) = {len(blocks) + dropped}")
    tracer.say(f"剔除页眉页脚     = {dropped}")
    tracer.say(f"合并断行后块数   = {len(blocks)}")
    tracer.say(f"识别章节数       = {len(sections)}")
    tracer.say("")

    tracer.run(blocks)

    tracer.say("=" * 78)
    tracer.say(f"最终产出 {len(tracer.chunks)} 个 chunk")
    tracer.say("=" * 78)
    total = 0
    for c in tracer.chunks:
        total += len(c["text"])
        tracer.say(f"  [{c['chunk_index']:>2}] p.{c['page_no']} "
                   f"len={len(c['text']):>3} §{c['section_title'][:18]}")

    src_total = sum(len(b.text) for b in blocks)
    tracer.say("")
    tracer.say(f"原文块总字符 = {src_total}")
    tracer.say(f"chunk 总字符 = {total}")
    tracer.say(f"膨胀比       = {total / src_total:.3f}  （overlap 导致重复的部分）")

    Path(OUT).write_text("\n".join(tracer.lines), encoding="utf-8")
    print(f"written: {OUT} ({len(tracer.lines)} lines)")


main()
