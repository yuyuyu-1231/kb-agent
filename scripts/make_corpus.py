r"""生成制度语料 PDF（考勤 / 报销 / IT 设备）。

## 为什么和 `make_sample_pdf.py` 分开

那份是手写的「每页一个 tuple」，页数固定、内容量人工控制。
这批文档内容长得多，手写分页会撞上 `insert_textbox` 的老问题：
**放不下时返回负数，并且一个字符都不写**——内容凭空消失，还不报错。

所以这里做**自动分页**：

    放得下   → 写进去，y 往下移
    放不下   → 新起一页再试
    还放不下 → 按句号劈成两半，递归（单个段落比整页还高的极端情况）

## 排版必须和员工手册一致

页眉 y=55（下面一条细线）、页脚 y=800、正文 10.5pt / 1.45 行高。
如果新语料排版差异太大，`ingest.py` 里那套「上下 10% 区域 + 去数字后重复」
的页眉页脚清洗逻辑就测不到真实场景了——那等于换了个更简单的题。

## 页数

页脚写「共 N 页」需要先知道总页数。做法是**渲染两遍**：第一遍数页数，
第二遍带总数重渲。多花几十毫秒，换一个和员工手册一致的页脚。

跑法：
    python scripts/make_corpus.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from scripts.corpus_content import DOCUMENTS  # noqa: E402

OUT_DIR = ROOT / "data" / "corpus"

MARGIN_LEFT = 72
MARGIN_RIGHT = 523
BODY_TOP = 110
BODY_BOTTOM = 760
FONT = "china-s"
BODY_SIZE = 10.5
LINE_HEIGHT = 1.45

# 判断「这一页还剩多少空间」的经验值：低于这个高度就换页，
# 免得一段话被挤在页面最底下只放得下一行。
MIN_SPACE = 60


class _Writer:
    """把章节和段落写进 PDF，自动翻页。"""

    def __init__(self, doc: pymupdf.Document, header: str, total_pages: int) -> None:
        self.doc = doc
        self.header = header
        self.total = total_pages
        self.page: pymupdf.Page | None = None
        self.page_no = 0
        self.y = BODY_TOP

    # ---------------------------------------------------------------- 页面

    def _blank_page(self) -> None:
        self.page_no += 1
        page = self.doc.new_page(width=595, height=842)

        page.insert_text(
            (MARGIN_LEFT, 55), self.header,
            fontname=FONT, fontsize=8.5, color=(0.45, 0.45, 0.45),
        )
        page.draw_line(
            pymupdf.Point(MARGIN_LEFT, 62),
            pymupdf.Point(MARGIN_RIGHT, 62),
            color=(0.75, 0.75, 0.75),
            width=0.6,
        )
        # total 为 0 表示这是第一遍（数页数用），先不写总数
        tail = f"共 {self.total} 页" if self.total else "共 ? 页"
        page.insert_text(
            (MARGIN_LEFT, 800), f"- 第 {self.page_no} 页 / {tail} -",
            fontname=FONT, fontsize=8.5, color=(0.45, 0.45, 0.45),
        )
        self.page = page
        self.y = BODY_TOP

    def new_page(self) -> None:
        self._blank_page()

    def start_section(self, title: str) -> None:
        """章节标题永远新起一页，和员工手册保持同一种版式。"""
        self._blank_page()
        assert self.page is not None
        self.page.insert_text(
            (MARGIN_LEFT, BODY_TOP), title,
            fontname=FONT, fontsize=15, color=(0.1, 0.1, 0.1),
        )
        self.y = BODY_TOP + 32

    # ---------------------------------------------------------------- 正文

    def _try_place(self, text: str) -> bool:
        """在当前页尝试放一段。放得下返回 True。"""
        assert self.page is not None
        if self.y >= BODY_BOTTOM - MIN_SPACE:
            return False
        rect = pymupdf.Rect(MARGIN_LEFT, self.y, MARGIN_RIGHT, BODY_BOTTOM)
        leftover = self.page.insert_textbox(
            rect, text,
            fontname=FONT, fontsize=BODY_SIZE,
            lineheight=LINE_HEIGHT,
            align=pymupdf.TEXT_ALIGN_LEFT,
        )
        if leftover < 0:
            # 放不下时 PyMuPDF 不写入任何内容，所以这里 retry 是安全的
            return False
        self.y += (BODY_BOTTOM - self.y - leftover) + 6
        return True

    def write(self, text: str, depth: int = 0) -> None:
        if not text:
            self.y += 8          # 空行 = 段落间距
            return

        if self._try_place(text):
            return

        self.new_page()
        if self._try_place(text):
            return

        # 换页后还放不下：说明这一段比整页还高，按句号劈开
        if depth > 3:
            # 兜底：硬塞进去（会溢出，但不至于死循环），并告警
            assert self.page is not None
            self.page.insert_textbox(
                pymupdf.Rect(MARGIN_LEFT, BODY_BOTTOM - MIN_SPACE, MARGIN_RIGHT, BODY_BOTTOM),
                text, fontname=FONT, fontsize=BODY_SIZE, lineheight=LINE_HEIGHT,
            )
            print(f"[WARN] 段落过长被截断（depth={depth}）：{text[:30]}…")
            self.y = BODY_BOTTOM - MIN_SPACE
            return

        head, tail = _split_at_sentence(text)
        self.write(head, depth + 1)
        self.write(tail, depth + 1)


def _split_at_sentence(text: str) -> tuple[str, str]:
    """在中间附近的句号处劈开；找不到句号就在中点劈。"""
    mid = len(text) // 2
    best = -1
    for punct in ("。", "；", "\n"):
        pos = text.find(punct, mid)
        if pos != -1 and (best == -1 or pos < best):
            best = pos
    if best == -1:
        pos = text.rfind("。", 0, mid)
        best = pos if pos != -1 else mid
    return text[: best + 1], text[best + 1 :]


def render(spec: dict, total_pages: int = 0) -> pymupdf.Document:
    doc = pymupdf.open()
    w = _Writer(doc, spec["header"], total_pages)

    for title, paragraphs in spec["sections"]:
        w.start_section(title)
        for para in paragraphs:
            w.write(para)

    return doc


def build(spec: dict) -> Path:
    """两遍渲染：先数页数，再带「共 N 页」重渲。"""
    probe = render(spec, total_pages=0)
    pages = probe.page_count
    probe.close()

    doc = render(spec, total_pages=pages)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / spec["filename"]
    doc.save(str(out))
    doc.close()
    return out


def main() -> int:
    if not DOCUMENTS:
        print("corpus_content.py 里没有文档")
        return 1

    for spec in DOCUMENTS:
        path = build(spec)
        size_kb = path.stat().st_size / 1024
        # 用同一套解析器反查，确认没有内容被吞
        from app.services.ingest import parse_pdf

        chunks, result = parse_pdf(path, chunk_size=400, overlap=80)
        print(
            f"{path.name}: {size_kb:.1f} KB, "
            f"写 {result.pages if hasattr(result, 'pages') else '?'} 页, "
            f"解析出 {len(chunks)} chunks"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
