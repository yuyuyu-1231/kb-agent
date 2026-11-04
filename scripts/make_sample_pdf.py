r"""生成测试用 PDF：一份带真实排版特征（页眉、页脚、章节标题）的员工手册。

为什么要自己生成：
- 需要精确控制页眉页脚来测试清洗逻辑（真实文档里这两样最难自动识别）
- 需要清晰的章节结构来验证标题识别
- 当天就能跑通 ingest 全链路

第 3 周做正式评估前，会用真实语料替换这里的内容并重灌。

输出：E:\PythonProject3\kb-agent\data\corpus\员工手册.pdf
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

OUT_DIR = ROOT / "data" / "corpus"
OUT_PATH = OUT_DIR / "员工手册.pdf"

HEADER = "示例科技有限公司 · 员工手册（2026 版）"
MARGIN_LEFT = 72
MARGIN_RIGHT = 523
BODY_TOP = 110
BODY_BOTTOM = 760

# 每页内容：(页内标题, 正文段落列表)
PAGES: list[tuple[str, list[str]]] = [
    (
        "封面",
        [
            "示例科技有限公司",
            "员工手册",
            "",
            "2026 年版",
            "",
            "本手册适用于公司全体在职员工，包括试用期员工。",
            "手册内容如与国家法律法规冲突，以法律法规为准。",
        ],
    ),
    (
        "目录",
        [
            "第一章  总则 ................................ 3",
            "第三章  假期制度 ............................ 4",
            "第五章  费用报销 ............................ 5",
            "第七章  考勤管理 ............................ 6",
        ],
    ),
    (
        "第一章  总则",
        [
            "1.1  适用范围",
            "本手册适用于与公司签订劳动合同的全体员工。劳务派遣人员、实习生参照执行，另有约定的从其约定。",
            "",
            "1.2  生效与修订",
            "本手册自 2026 年 1 月 1 日起施行，由人力资源部负责解释。公司可根据经营需要修订本手册，修订后通过内部邮件通知全体员工，自通知之日起生效。",
            "",
            "1.3  员工义务",
            "员工应当遵守公司各项规章制度，保守公司商业秘密。因违反制度给公司造成损失的，公司有权依据劳动合同及法律规定追究相应责任。",
        ],
    ),
    (
        "第三章  假期制度",
        [
            "3.1  年休假",
            "员工累计工作满 1 年不满 10 年的，年休假 5 天；满 10 年不满 20 年的，年休假 10 天；满 20 年的，年休假 15 天。",
            "试用期包含在劳动合同期限内。试用期员工与正式员工享有同等休假权利，但需转正后方可申请年休假。",
            "年休假应当在当年内使用完毕，因工作原因未能安排的，经部门负责人批准可跨年顺延至次年 3 月 31 日前使用。",
            "",
            "3.2  病假",
            "员工请病假须提供二级以上医院出具的诊断证明与病假条。病假期间工资按本人工资的 80% 发放，低于当地最低工资标准 80% 的按最低标准执行。",
            "连续病假超过 30 天的，需提交复岗体检报告。",
            "",
            "3.3  婚假与产假",
            "员工依法办理结婚登记的，享受婚假 3 天；符合晚婚条件的，另加 7 天。",
            "女职工生育享受产假 98 天，难产的增加 15 天，多胞胎每多生育一个婴儿增加 15 天。男职工享受陪产假 15 天。",
        ],
    ),
    (
        "第五章  费用报销",
        [
            "5.1  报销时限",
            "员工应在费用发生后 30 日内提交发票与审批单，逾期不予受理。跨年度费用须在次年 1 月 15 日前提交。",
            "",
            "5.2  差旅标准",
            "一线城市（北京、上海、广州、深圳）住宿标准为每人每晚 500 元；二线城市为每人每晚 350 元；其他城市为每人每晚 250 元。",
            "市内交通费凭票据实报销，单次超过 100 元的需说明事由。",
            "出差期间的餐费补贴按每人每天 100 元包干，不再另行报销餐饮发票。",
            "",
            "5.3  审批流程",
            "报销单须经直属主管审核、部门负责人审批后提交财务。单笔金额超过 5000 元的，还需总经理审批。",
            "财务部在收到完整单据后的 5 个工作日内完成付款。",
        ],
    ),
    (
        "第七章  考勤管理",
        [
            "7.1  工作时间",
            "公司实行标准工时制，工作时间为上午 9:00 至下午 18:00，其中 12:00 至 13:00 为午休时间。",
            "每周工作 5 天，周六、周日为休息日。因业务需要调整作息的，由部门另行通知。",
            "",
            "7.2  迟到与早退",
            "迟到或早退 30 分钟以内的，每次扣发当日工资的 10%；超过 30 分钟不足 2 小时的，按事假半天处理；超过 2 小时的，按事假 1 天处理。",
            "当月累计迟到超过 3 次的，取消当季全勤奖。",
            "",
            "7.3  加班",
            "加班须提前在系统中提交申请并经直属主管批准，未经批准的延时工作不计为加班。",
            "工作日加班按小时工资的 1.5 倍支付，休息日加班且不能安排调休的按 2 倍支付，法定节假日加班按 3 倍支付。",
        ],
    ),
]


def build_pdf() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()

    total = len(PAGES)
    for idx, (title, paragraphs) in enumerate(PAGES, start=1):
        page = doc.new_page(width=595, height=842)   # A4

        # ---- 页眉：每页相同，用来测试"重复行剔除" ----
        page.insert_text(
            (MARGIN_LEFT, 55),
            HEADER,
            fontname="china-s",
            fontsize=8.5,
            color=(0.45, 0.45, 0.45),
        )
        # 页眉下的一条横线，让它更像真实文档
        page.draw_line(
            pymupdf.Point(MARGIN_LEFT, 62),
            pymupdf.Point(MARGIN_RIGHT, 62),
            color=(0.75, 0.75, 0.75),
            width=0.6,
        )

        # ---- 页脚：每页不同（含页码），测试"如何区分页码与正文" ----
        page.insert_text(
            (MARGIN_LEFT, 800),
            f"- 第 {idx} 页 / 共 {total} 页 -",
            fontname="china-s",
            fontsize=8.5,
            color=(0.45, 0.45, 0.45),
        )

        # ---- 正文 ----
        # 封面居中放大，其余页面正常排版
        if idx == 1:
            y = 260
            for line in paragraphs:
                if not line:
                    y += 14
                    continue
                size = 20 if y < 320 else 11
                page.insert_text(
                    (MARGIN_LEFT + 60, y), line,
                    fontname="china-s", fontsize=size,
                )
                y += 30 if size == 20 else 20
            continue

        # 页内标题
        page.insert_text(
            (MARGIN_LEFT, BODY_TOP),
            title,
            fontname="china-s",
            fontsize=15,
            color=(0.1, 0.1, 0.1),
        )
        y = BODY_TOP + 32

        for para in paragraphs:
            if not para:
                y += 8
                continue
            rect = pymupdf.Rect(MARGIN_LEFT, y, MARGIN_RIGHT, BODY_BOTTOM)
            # insert_textbox 会自动换行，返回剩余高度（负值表示放不下）
            leftover = page.insert_textbox(
                rect, para,
                fontname="china-s", fontsize=10.5,
                lineheight=1.45,
                align=pymupdf.TEXT_ALIGN_LEFT,
            )
            used = (BODY_BOTTOM - y) - leftover
            y += used + 6
            if y > BODY_BOTTOM - 20:
                break

    doc.save(str(OUT_PATH))
    doc.close()
    return OUT_PATH


if __name__ == "__main__":
    path = build_pdf()
    size_kb = path.stat().st_size / 1024
    print(f"written: {path} ({size_kb:.1f} KB, {len(PAGES)} pages)")
