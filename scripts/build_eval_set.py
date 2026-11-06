r"""生成评估集：data/eval_set.jsonl + docs/EVAL_SET.md

设计要点：

1. **数据源在脚本里，产物是 JSONL 和 Markdown**
   手写 JSONL 容易出语法错误，而且标准答案和来源容易标错。
   脚本里用 Python 结构定义，生成时自动校验 chunk_index / page_no / section
   是否与真实语料一致 —— 标错会直接报错，不会写进文件。

2. **每条都带 answerable 和 expected_action**
   answerable: full | partial | none   —— 知识库覆盖程度
   expected_action: answer | ticket    —— Agent 应该答还是应该转单
   这两个字段分开，是因为"部分可答"和"应转单"是两种不同的正确行为。

3. **must_not 用来抓编造**
   应拒答题里列出"不该出现的具体数字/规则"，LLM-as-Judge 时用来判幻觉。
   比如"年终奖"绝不能出现"3 个月"或"13 薪"。

跑法：
    python scripts/build_eval_set.py
"""

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ingest import parse_pdf  # noqa: E402

PDF = ROOT / "data" / "corpus" / "员工手册.pdf"
JSONL_PATH = ROOT / "data" / "eval_set.jsonl"
MD_PATH = ROOT / "docs" / "EVAL_SET.md"
DOC_ID = "员工手册"


def src(*triples):
    """(chunk_index, page_no, section_title) -> expected_sources 列表"""
    return [
        {"doc_id": DOC_ID, "chunk_index": ci, "page_no": p, "section_title": s}
        for ci, p, s in triples
    ]


# --------------------------------------------------------------------------- #
# 直接事实 20 条
# --------------------------------------------------------------------------- #
DIRECT = [
    {
        "id": "D01", "type": "direct", "answerable": "full",
        "question": "年假有多少天？",
        "expected_answer": "年假天数按累计工作年限确定：满 1 年不满 10 年的 5 天；满 10 年不满 20 年的 10 天；满 20 年的 15 天。",
        "key_points": ["5 天", "10 天", "15 天", "累计工作年限"],
        "expected_sources": src((5, 4, "3.1  年休假")),
        "expected_action": "answer",
        "must_not": [],
        "notes": "",
    },
    {
        "id": "D02", "type": "direct", "answerable": "full",
        "question": "试用期员工能休年假吗？",
        "expected_answer": "试用期包含在劳动合同期限内，试用期员工与正式员工享有同等休假权利，但需转正后方可申请年休假。",
        "key_points": ["享有同等休假权利", "需转正后方可申请"],
        "expected_sources": src((5, 4, "3.1  年休假")),
        "expected_action": "answer",
        "must_not": ["试用期没有年假"],
        "notes": "核心区分点：权利有，申请时点受限",
    },
    {
        "id": "D03", "type": "direct", "answerable": "full",
        "question": "今年的年假没休完，可以留到明年吗？",
        "expected_answer": "年休假应在当年内使用完毕。因工作原因未能安排的，经部门负责人批准可跨年顺延至次年 3 月 31 日前使用。",
        "key_points": ["当年内使用完毕", "部门负责人批准", "次年 3 月 31 日前"],
        "expected_sources": src((5, 4, "3.1  年休假")),
        "expected_action": "answer",
        "must_not": ["自动顺延", "无限期累积"],
        "notes": "",
    },
    {
        "id": "D04", "type": "direct", "answerable": "full",
        "question": "请病假需要提供什么材料？",
        "expected_answer": "须提供二级以上医院出具的诊断证明与病假条。连续病假超过 30 天的，还需提交复岗体检报告。",
        "key_points": ["二级以上医院", "诊断证明", "病假条", "连续超 30 天需复岗体检"],
        "expected_sources": src((6, 4, "3.2  病假")),
        "expected_action": "answer",
        "must_not": ["一级医院", "社区医院即可"],
        "notes": "复岗体检只针对连续超 30 天，答成所有病假都要会扣分",
    },
    {
        "id": "D05", "type": "direct", "answerable": "full",
        "question": "病假期间工资怎么算？",
        "expected_answer": "病假期间工资按本人工资的 80% 发放；低于当地最低工资标准 80% 的，按最低标准执行。",
        "key_points": ["本人工资 80%", "最低工资标准 80% 兜底"],
        "expected_sources": src((6, 4, "3.2  病假")),
        "expected_action": "answer",
        "must_not": ["全额发放", "按 60%"],
        "notes": "",
    },
    {
        "id": "D06", "type": "direct", "answerable": "full",
        "question": "婚假有几天？",
        "expected_answer": "依法办理结婚登记的，享受婚假 3 天；符合晚婚条件的，另加 7 天。",
        "key_points": ["3 天", "晚婚另加 7 天"],
        "expected_sources": src((7, 4, "3.3  婚假与产假")),
        "expected_action": "answer",
        "must_not": ["10 天（不分晚婚）"],
        "notes": "",
    },
    {
        "id": "D07", "type": "direct", "answerable": "full",
        "question": "男职工有陪产假吗？多少天？",
        "expected_answer": "男职工享受陪产假 15 天。",
        "key_points": ["陪产假", "15 天"],
        "expected_sources": src((7, 4, "3.3  婚假与产假")),
        "expected_action": "answer",
        "must_not": ["7 天", "10 天"],
        "notes": "",
    },
    {
        "id": "D08", "type": "direct", "answerable": "full",
        "question": "发票最晚什么时候提交？",
        "expected_answer": "员工应在费用发生后 30 日内提交发票与审批单，逾期不予受理。",
        "key_points": ["30 日内", "逾期不予受理"],
        "expected_sources": src((8, 5, "5.1  报销时限")),
        "expected_action": "answer",
        "must_not": ["60 日", "当季度内"],
        "notes": "",
    },
    {
        "id": "D09", "type": "direct", "answerable": "full",
        "question": "去年的费用今年还能报销吗？",
        "expected_answer": "跨年度费用须在次年 1 月 15 日前提交。",
        "key_points": ["跨年度", "次年 1 月 15 日前"],
        "expected_sources": src((8, 5, "5.1  报销时限")),
        "expected_action": "answer",
        "must_not": ["不能报销"],
        "notes": "",
    },
    {
        "id": "D10", "type": "direct", "answerable": "full",
        "question": "去北京出差，住宿一晚上能报多少钱？",
        "expected_answer": "北京属于一线城市，住宿标准为每人每晚 500 元。",
        "key_points": ["一线城市", "500 元", "每人每晚"],
        "expected_sources": src((9, 5, "5.2  差旅标准")),
        "expected_action": "answer",
        "must_not": ["350 元", "250 元"],
        "notes": "需要模型自己判断北京属于一线城市",
    },
    {
        "id": "D11", "type": "direct", "answerable": "full",
        "question": "出差期间的餐费怎么补贴？",
        "expected_answer": "出差期间餐费补贴按每人每天 100 元包干，不再另行报销餐饮发票。",
        "key_points": ["100 元", "每人每天", "包干", "不再报销餐饮发票"],
        "expected_sources": src((9, 5, "5.2  差旅标准")),
        "expected_action": "answer",
        "must_not": ["凭票报销餐费"],
        "notes": "",
    },
    {
        "id": "D12", "type": "direct", "answerable": "full",
        "question": "报销单需要经过哪些人审批？",
        "expected_answer": "须经直属主管审核、部门负责人审批后提交财务。",
        "key_points": ["直属主管", "部门负责人", "财务"],
        "expected_sources": src((10, 5, "5.3  审批流程")),
        "expected_action": "answer",
        "must_not": ["直接提交财务"],
        "notes": "",
    },
    {
        "id": "D13", "type": "direct", "answerable": "full",
        "question": "报销单金额超过多少需要总经理审批？",
        "expected_answer": "单笔金额超过 5000 元的，还需总经理审批。",
        "key_points": ["5000 元", "总经理"],
        "expected_sources": src((10, 5, "5.3  审批流程")),
        "expected_action": "answer",
        "must_not": ["10000 元"],
        "notes": "",
    },
    {
        "id": "D14", "type": "direct", "answerable": "full",
        "question": "公司几点上班、几点下班？",
        "expected_answer": "工作时间为上午 9:00 至下午 18:00，其中 12:00 至 13:00 为午休时间。",
        "key_points": ["9:00", "18:00", "12:00-13:00 午休"],
        "expected_sources": src((11, 6, "7.1  工作时间")),
        "expected_action": "answer",
        "must_not": ["8:30", "17:30", "8 小时工作制含午休"],
        "notes": "",
    },
    {
        "id": "D15", "type": "direct", "answerable": "full",
        "question": "一周上几天班？哪几天休息？",
        "expected_answer": "每周工作 5 天，周六、周日为休息日。",
        "key_points": ["5 天", "周六日休息"],
        "expected_sources": src((11, 6, "7.1  工作时间")),
        "expected_action": "answer",
        "must_not": ["大小周", "6 天"],
        "notes": "",
    },
    {
        "id": "D16", "type": "direct", "answerable": "full",
        "question": "迟到 20 分钟怎么处理？",
        "expected_answer": "迟到 30 分钟以内的，每次扣发当日工资的 10%。",
        "key_points": ["30 分钟以内", "扣发当日工资 10%"],
        "expected_sources": src((12, 6, "7.2  迟到与早退")),
        "expected_action": "answer",
        "must_not": ["按事假半天", "罚款 100 元"],
        "notes": "",
    },
    {
        "id": "D17", "type": "direct", "answerable": "full",
        "question": "迟到超过 2 小时算旷工吗？",
        "expected_answer": "手册规定超过 2 小时的按事假 1 天处理。知识库中没有使用「旷工」这一概念，也没有旷工相关规定。",
        "key_points": ["按事假 1 天处理", "未使用「旷工」概念"],
        "expected_sources": src((12, 6, "7.2  迟到与早退")),
        "expected_action": "answer",
        "must_not": ["算旷工", "按旷工处理"],
        "notes": "陷阱题：问的是「旷工」，但手册用的是「事假」。答「旷工」说明没读原文",
    },
    {
        "id": "D18", "type": "direct", "answerable": "full",
        "question": "工作日加班工资怎么算？",
        "expected_answer": "工作日加班按小时工资的 1.5 倍支付。",
        "key_points": ["1.5 倍", "小时工资为基数"],
        "expected_sources": src((13, 6, "7.3  加班")),
        "expected_action": "answer",
        "must_not": ["2 倍", "3 倍"],
        "notes": "",
    },
    {
        "id": "D19", "type": "direct", "answerable": "full",
        "question": "法定节假日加班是几倍工资？",
        "expected_answer": "法定节假日加班按小时工资的 3 倍支付。",
        "key_points": ["3 倍"],
        "expected_sources": src((13, 6, "7.3  加班")),
        "expected_action": "answer",
        "must_not": ["2 倍"],
        "notes": "和 D18 成对，测模型能否区分三种加班类型",
    },
    {
        "id": "D20", "type": "direct", "answerable": "full",
        "question": "这本手册什么时候开始施行？由谁负责解释？",
        "expected_answer": "自 2026 年 1 月 1 日起施行，由人力资源部负责解释。",
        "key_points": ["2026 年 1 月 1 日", "人力资源部"],
        "expected_sources": src((3, 3, "1.2  生效与修订")),
        "expected_action": "answer",
        "must_not": ["2025 年"],
        "notes": "",
    },
]

# --------------------------------------------------------------------------- #
# 跨段落多跳 10 条
# --------------------------------------------------------------------------- #
MULTI_HOP = [
    {
        "id": "M01", "type": "multi_hop", "answerable": "full",
        "question": "我是试用期员工，上个月迟到了 4 次，会有什么影响？",
        "expected_answer": "当月累计迟到超过 3 次的，取消当季全勤奖。试用期员工同样受本手册考勤规定约束。",
        "key_points": ["累计超 3 次", "取消当季全勤奖", "试用期同样适用"],
        "expected_sources": src((12, 6, "7.2  迟到与早退"), (2, 3, "1.1  适用范围")),
        "expected_action": "answer",
        "must_not": ["试用期不适用"],
        "notes": "需要组合「迟到处罚」+「适用范围」",
    },
    {
        "id": "M02", "type": "multi_hop", "answerable": "full",
        "question": "出差回来 40 天后才想起报销，还能报吗？如果这批总金额 8000 元，需要谁审批？",
        "expected_answer": "不能报销——应在费用发生后 30 日内提交，逾期不予受理。补充说明：单据有效时，单笔超过 5000 元还需总经理审批。",
        "key_points": ["30 日内", "逾期不予受理", "超 5000 元需总经理审批"],
        "expected_sources": src((8, 5, "5.1  报销时限"), (10, 5, "5.3  审批流程")),
        "expected_action": "answer",
        "must_not": ["可以报销"],
        "notes": "两问两答，第二问建立在第一问有单据的前提上",
    },
    {
        "id": "M03", "type": "multi_hop", "answerable": "full",
        "question": "昨天加班到 20:00，今天迟到了 20 分钟，会扣钱吗？",
        "expected_answer": "会。迟到 30 分钟以内每次扣发当日工资 10%。手册未规定「加班可以抵扣次日迟到」，不能自行推定。",
        "key_points": ["扣发当日工资 10%", "未规定加班抵扣迟到"],
        "expected_sources": src((11, 6, "7.1  工作时间"), (12, 6, "7.2  迟到与早退")),
        "expected_action": "answer",
        "must_not": ["加班可以抵扣", "不扣钱"],
        "notes": "测模型会不会编造人情化特权",
    },
    {
        "id": "M04", "type": "multi_hop", "answerable": "full",
        "question": "周六来公司加班，工资怎么算？",
        "expected_answer": "周六属于休息日。休息日加班且不能安排调休的，按小时工资的 2 倍支付；能安排调休的优先调休。",
        "key_points": ["周六为休息日", "2 倍", "不能安排调休才支付"],
        "expected_sources": src((13, 6, "7.3  加班"), (11, 6, "7.1  工作时间")),
        "expected_action": "answer",
        "must_not": ["1.5 倍", "3 倍"],
        "notes": "需要先判断周六是休息日，才能选对倍数",
    },
    {
        "id": "M05", "type": "multi_hop", "answerable": "full",
        "question": "试用期员工请病假，工资也是按 80% 发吗？",
        "expected_answer": "是。病假工资按本人工资 80% 发放；且手册规定试用期员工与正式员工享有同等休假权利。",
        "key_points": ["80%", "试用期享有同等待遇"],
        "expected_sources": src((6, 4, "3.2  病假"), (5, 4, "3.1  年休假")),
        "expected_action": "answer",
        "must_not": ["试用期不发工资"],
        "notes": "「同等待遇」写在年休假那一节，需要跨节引用",
    },
    {
        "id": "M06", "type": "multi_hop", "answerable": "partial",
        "question": "实习生迟到会被扣工资吗？",
        "expected_answer": "手册规定实习生参照执行、另有约定的从其约定；若参照执行，迟到 30 分钟以内扣当日工资 10%。具体以实习协议约定为准。",
        "key_points": ["实习生参照执行", "以约定为准", "迟到扣 10%"],
        "expected_sources": src((2, 3, "1.1  适用范围"), (12, 6, "7.2  迟到与早退")),
        "expected_action": "answer",
        "must_not": ["实习生不适用", "实习生必须扣"],
        "notes": "手册用「参照执行」+「从其约定」，答案必须带条件，不能给绝对结论",
    },
    {
        "id": "M07", "type": "multi_hop", "answerable": "partial",
        "question": "报销单 6000 元，从提交到拿到钱大概要多久？",
        "expected_answer": "审批链为直属主管→部门负责人→财务，单笔超 5000 元还需总经理审批；财务在收到完整单据后 5 个工作日内付款。手册未规定各级审批的时限，因此总时长无法确定。",
        "key_points": ["需总经理审批", "5 个工作日内付款", "未规定审批时限"],
        "expected_sources": src((10, 5, "5.3  审批流程"), (8, 5, "5.1  报销时限")),
        "expected_action": "answer",
        "must_not": ["5 个工作日（把付款时限当总时长）"],
        "notes": "陷阱：「5 个工作日」只是财务付款环节，不是全流程时长",
    },
    {
        "id": "M08", "type": "multi_hop", "answerable": "partial",
        "question": "女员工休产假期间，工资怎么发？",
        "expected_answer": "手册只规定了产假 98 天（难产加 15 天、多胞胎每多一个加 15 天），以及病假工资按 80% 发放；未规定产假期间的工资标准，不能套用病假规则，建议咨询人力资源部。",
        "key_points": ["产假 98 天", "未规定产假工资", "不能套用病假 80%"],
        "expected_sources": src((7, 4, "3.3  婚假与产假"), (6, 4, "3.2  病假")),
        "expected_action": "answer",
        "must_not": ["按病假 80% 发放", "按全额工资发放"],
        "notes": "最容易编造的题：病假 80% 就在隔壁 chunk，模型很可能直接套用",
    },
    {
        "id": "M09", "type": "multi_hop", "answerable": "partial",
        "question": "婚假和年假能连着一起休吗？",
        "expected_answer": "手册分别规定了婚假 3 天（晚婚另加 7 天）和年休假规则，但未规定两者能否连休，需咨询人力资源部。",
        "key_points": ["婚假 3 天", "未规定连休", "建议咨询 HR"],
        "expected_sources": src((7, 4, "3.3  婚假与产假"), (5, 4, "3.1  年休假")),
        "expected_action": "answer",
        "must_not": ["可以连休", "不可以连休"],
        "notes": "知识库对「可以/不可以」都没有依据，任何确定结论都是编造",
    },
    {
        "id": "M10", "type": "multi_hop", "answerable": "partial",
        "question": "出差住酒店超出住宿标准了，超出部分能报吗？",
        "expected_answer": "手册只规定了住宿标准（一线 500、二线 350、其他 250 元每人每晚），未规定超标部分如何处理。报销单需经主管与部门负责人审批，具体以审批结果为准。",
        "key_points": ["住宿标准", "未规定超标处理", "需审批"],
        "expected_sources": src((9, 5, "5.2  差旅标准"), (10, 5, "5.3  审批流程")),
        "expected_action": "answer",
        "must_not": ["超标部分自理", "可以全额报销"],
        "notes": "同上：手册没说，给确定答案就是编造",
    },
]

# --------------------------------------------------------------------------- #
# 应拒答 5 条
# --------------------------------------------------------------------------- #
REFUSAL = [
    {
        "id": "R01", "type": "refusal", "answerable": "none",
        "question": "公司年终奖怎么发？",
        "expected_answer": "知识库中未收录年终奖相关规定，应说明未找到并建议人工跟进（可建单）。",
        "key_points": ["明确说明未收录", "建议人工/HR 跟进"],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["13 薪", "3 个月工资", "按绩效系数发放"],
        "notes": "干扰项：7.2 提到「全勤奖」，但那是处罚条款不是年终奖",
    },
    {
        "id": "R02", "type": "refusal", "answerable": "none",
        "question": "公司有员工股权激励计划吗？",
        "expected_answer": "知识库中未收录股权激励相关内容。",
        "key_points": ["明确说明未收录"],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["期权", "限制性股票", "四年归属"],
        "notes": "",
    },
    {
        "id": "R03", "type": "refusal", "answerable": "none",
        "question": "公司提供年度体检吗？",
        "expected_answer": "知识库中未收录员工体检福利相关规定。",
        "key_points": ["明确说明未收录"],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["每年一次", "三甲医院", "入职满一年后享受"],
        "notes": "",
    },
    {
        "id": "R04", "type": "refusal", "answerable": "none",
        "question": "公司有内部培训或进修机会吗？",
        "expected_answer": "知识库中未收录培训与进修相关制度。",
        "key_points": ["明确说明未收录"],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["每年 N 天培训假", "报销学费", "内部讲师"],
        "notes": "",
    },
    {
        "id": "R05", "type": "refusal", "answerable": "none",
        "question": "离职需要提前多久申请？竞业协议怎么签？",
        "expected_answer": "知识库中未收录离职流程与竞业限制相关规定。",
        "key_points": ["明确说明未收录"],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["提前 30 天", "提前一个月", "竞业期 2 年"],
        "notes": "手册只有第 1/3/5/7 章，无离职相关章节",
    },
]


EVAL_ITEMS = DIRECT + MULTI_HOP + REFUSAL


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def validate(items: list[dict], chunks: list[dict]) -> list[str]:
    """交叉校验：chunk_index 是否存在、page_no 与 section 是否与真实语料一致。"""
    by_index = {c["chunk_index"]: c for c in chunks}
    problems: list[str] = []
    seen_ids: set[str] = set()

    for item in items:
        iid = item["id"]
        if iid in seen_ids:
            problems.append(f"{iid}: id 重复")
        seen_ids.add(iid)

        if item["type"] not in ("direct", "multi_hop", "refusal"):
            problems.append(f"{iid}: 非法 type {item['type']}")
        if item["answerable"] not in ("full", "partial", "none"):
            problems.append(f"{iid}: 非法 answerable {item['answerable']}")
        if item["expected_action"] not in ("answer", "ticket"):
            problems.append(f"{iid}: 非法 expected_action {item['expected_action']}")

        for s in item["expected_sources"]:
            ci = s["chunk_index"]
            real = by_index.get(ci)
            if real is None:
                problems.append(f"{iid}: chunk_index={ci} 不存在")
                continue
            if real["page_no"] != s["page_no"]:
                problems.append(
                    f"{iid}: chunk {ci} 页码应为 {real['page_no']}，标了 {s['page_no']}"
                )
            if real["section_title"] != s["section_title"]:
                problems.append(
                    f"{iid}: chunk {ci} 章节应为 {real['section_title']!r}，"
                    f"标了 {s['section_title']!r}"
                )

        if item["type"] == "refusal" and item["expected_sources"]:
            problems.append(f"{iid}: 应拒答题不应有 expected_sources")
        if item["type"] != "refusal" and not item["expected_sources"]:
            problems.append(f"{iid}: 可答题必须有 expected_sources")

    return problems


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def write_jsonl(items: list[dict]) -> None:
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


TYPE_LABEL = {"direct": "直接事实", "multi_hop": "跨段落多跳", "refusal": "应拒答"}
ACTION_LABEL = {"answer": "直接回答", "ticket": "转人工/建单"}
ANSWERABLE_LABEL = {"full": "完整可答", "partial": "部分可答", "none": "不可答"}


def write_markdown(items: list[dict]) -> None:
    by_type: dict[str, list[dict]] = {"direct": [], "multi_hop": [], "refusal": []}
    for item in items:
        by_type[item["type"]].append(item)

    lines: list[str] = []
    lines.append("# 评估集（草稿）")
    lines.append("")
    lines.append(f"共 **{len(items)} 条**，数据源 `data/eval_set.jsonl`，本文件由脚本生成。")
    lines.append("")
    lines.append("| 类型 | 条数 | 说明 |")
    lines.append("|---|---|---|")
    lines.append(f"| 直接事实 | {len(by_type['direct'])} | 答案落在单个 chunk |")
    lines.append(f"| 跨段落多跳 | {len(by_type['multi_hop'])} | 需组合 2 个以上 chunk |")
    lines.append(f"| 应拒答 | {len(by_type['refusal'])} | 知识库无相关内容，应说明未找到 |")
    lines.append("")
    lines.append("## 字段说明")
    lines.append("")
    lines.append("- `answerable`: `full` 完整可答 / `partial` 部分可答（知识库只覆盖一部分）/ `none` 不可答")
    lines.append("- `expected_action`: `answer` 应直接回答 / `ticket` 应说明未找到并转人工")
    lines.append("- `must_not`: 不该出现的具体数字或说法，用于抓幻觉")
    lines.append("- `key_points`: 判分时要覆盖的关键信息")
    lines.append("")
    lines.append("---")
    lines.append("")

    for tkey in ("direct", "multi_hop", "refusal"):
        group_label = TYPE_LABEL[tkey]
        lines.append(f"## {group_label}（{len(by_type[tkey])} 条）")
        lines.append("")
        for item in by_type[tkey]:
            lines.append(f"### {item['id']}　{item['question']}")
            lines.append("")
            lines.append(f"**标准答案**：{item['expected_answer']}")
            lines.append("")
            lines.append(
                f"**标记**：{ANSWERABLE_LABEL[item['answerable']]} · "
                f"{ACTION_LABEL[item['expected_action']]}"
            )
            lines.append("")
            lines.append(
                "**关键点**：" + " / ".join(f"`{k}`" for k in item["key_points"])
            )
            lines.append("")
            if item["expected_sources"]:
                refs = " ；".join(
                    f"chunk {s['chunk_index']} · p.{s['page_no']} · {s['section_title']}"
                    for s in item["expected_sources"]
                )
                lines.append(f"**来源**：{refs}")
            else:
                lines.append("**来源**：（无，应拒答）")
            lines.append("")
            if item["must_not"]:
                lines.append(
                    "**禁止出现**：" + " / ".join(f"`{m}`" for m in item["must_not"])
                )
                lines.append("")
            if item["notes"]:
                lines.append(f"**备注**：{item['notes']}")
                lines.append("")
            lines.append("---")
            lines.append("")

    lines.append("## 待补充：你负责的 15 条")
    lines.append("")
    lines.append("按一线客服的真实提问方式补 15 条，建议覆盖三类：")
    lines.append("")
    lines.append("| 类别 | 建议条数 | 示例 |")
    lines.append("|---|---|---|")
    lines.append("| 口语化提问 | 6 | 「我刚入职俩月，能请年假不？」 |")
    lines.append("| 模糊指代 | 4 | 「那个假期的事，能拖到明年吗？」（指代不明，测澄清能力） |")
    lines.append("| 多意图混合 | 5 | 「我要离婚了想请几天假，顺便问下报销还没交怎么办」 |")
    lines.append("")
    lines.append("补的时候直接往 `data/eval_set.jsonl` 里加，字段照抄现有结构，")
    lines.append("`id` 用 `H01`–`H15`（H = human）。")
    lines.append("")

    MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    chunks, _ = parse_pdf(PDF, chunk_size=400, overlap=80)

    problems = validate(EVAL_ITEMS, chunks)
    if problems:
        print("[校验失败]")
        for p in problems:
            print("  -", p)
        raise SystemExit(1)

    write_jsonl(EVAL_ITEMS)
    write_markdown(EVAL_ITEMS)

    counter = Counter(item["type"] for item in EVAL_ITEMS)
    print(f"OK  {len(EVAL_ITEMS)} 条")
    print(f"  direct={counter['direct']}  multi_hop={counter['multi_hop']}  refusal={counter['refusal']}")
    print(f"  -> {JSONL_PATH}")
    print(f"  -> {MD_PATH}")


if __name__ == "__main__":
    main()
