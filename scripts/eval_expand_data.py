"""新增评估题（第一批）：跨文档多跳 24 条 + 冲突识别 10 条。

## 出题原则

- **对着真实 chunk 写**：每条 `expected_sources` 的 chunk_index / page_no /
  section_title 都来自 `parse_pdf` 的实际输出，由 `expand_eval_set.py` 交叉校验。
- **不写"想一想就能答"的题**：多跳题必须真的需要同时看两份文档。
- **冲突题不预设立场**：只要求指出"两份文件不一致 + 各自说了什么"，
  不要求模型自己裁决哪个对（制度里根本没有优先级条款）。

## 四个真实冲突点

| # | 冲突 | 手册 | 新文档 |
|---|---|---|---|
| 1 | 年假天数 | 3.1 按工龄 5/10/15 天 | 考勤 3.2 统一 5 天 |
| 2 | 迟到扣款 | 7.2 30 分钟内扣 10%，超 30 分不足 2 小时按事假半天 | 考勤 2.2 15 分钟内不扣，超 15 分不足 1 小时扣 10% |
| 3 | 报销时限 | 5.1 30 日内，跨年度次年 1 月 15 日前 | 报销 5.2 45 日内，跨年度次年 1 月 31 日前 |
| 4 | 审批金额 | 5.3 报销超 5000 元需总经理 | 报销 4.1 采购 10000 元以上由总经理（适用范围不同） |
"""

from __future__ import annotations

DOC = "员工手册"
ATT = "考勤与休假管理办法"
EXP = "费用报销管理办法"
IT = "IT设备与账号管理规范"


def s(doc: str, ci: int, page: int, sec: str) -> dict:
    return {"doc_id": doc, "chunk_index": ci, "page_no": page, "section_title": sec}


MULTI_HOP: list[dict] = [
    # ---------------- 差旅 / 报销 ----------------
    {
        "id": "M16",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我去北京出差，住宿超了标准，多出来的钱公司给报吗？",
        "expected_answer": "不报。住宿费按实际发生额报销，超出标准的部分由员工个人承担。"
                           "北京的住宿标准为每人每晚 500 元（一线城市）。",
        "key_points": ["超出标准部分个人承担", "500 元", "一线城市"],
        "expected_sources": [s(EXP, 3, 2, "2.2  住宿费"), s(DOC, 9, 5, "5.2  差旅标准")],
        "expected_action": "answer",
        "must_not": ["公司全额报销", "可以报销超标部分"],
        "notes": "报销办法只说超标自理，具体金额要跳到手册 5.2",
    },
    {
        "id": "M17",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我上周感冒请了 2 天病假，只交了社区医院的诊断书，HR 说材料不合规。这合理吗？工资怎么算？",
        "expected_answer": "合理。病假须提供二级以上医院出具的诊断证明与病假条，社区医院不符合要求；"
                           "材料应在返岗后 3 个工作日内提交。病假期间工资按本人工资的 80% 发放。",
        "key_points": ["二级以上医院", "3 个工作日", "80%"],
        "expected_sources": [s(ATT, 7, 3, "3.3  病假"), s(DOC, 6, 4, "3.2  病假")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：要自己判断'社区医院≠二级以上'，断言里不放'社区医院'",
    },
    {
        "id": "M18",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我下周二去广州出差 3 天，住宿和吃饭大概能报多少？",
        "expected_answer": "广州属一线城市，住宿标准每人每晚 500 元；餐费按每人每天 100 元包干，"
                           "3 天餐补共 300 元，不再另行报销餐饮发票。",
        "key_points": ["一线城市", "500", "包干", "300"],
        "expected_sources": [s(EXP, 5, 2, "2.4  餐费补贴"), s(DOC, 9, 5, "5.2  差旅标准")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：要自己把'广州'归到一线，并算出 3 天餐补 300",
    },
    {
        "id": "M19",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我工龄 5 年，想连休 4 天年假，需要谁审批？提前几天提？",
        "expected_answer": "4 天属于 3 天以上，由分管副总审批；未超过 5 天，不需要人力资源部备案。"
                           "年休假申请须提前 3 个工作日提交。",
        "key_points": ["分管副总", "3 个工作日"],
        "expected_sources": [s(ATT, 5, 3, "3.1  请假流程与审批权限"), s(ATT, 6, 3, "3.2  年休假")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：4 天卡在 3–5 天区间，模型容易照搬'3 天以内找部门负责人'",
    },
    {
        "id": "M20",
        "type": "multi_hop",
        "answerable": "full",
        "question": "周末加班了，能换成调休吗？多久内要用掉？",
        "expected_answer": "休息日加班原则上安排调休，调休须在加班发生后 3 个月内使用，逾期作废。"
                           "法定节假日加班不安排调休，按国家规定支付加班工资。",
        "key_points": ["安排调休", "3 个月内", "逾期作废"],
        "expected_sources": [s(ATT, 10, 4, "4.2  调休"), s(DOC, 13, 6, "7.3  加班")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "手册只说休息日加班按 2 倍或调休，3 个月期限在考勤办法",
    },
    {
        "id": "M21",
        "type": "multi_hop",
        "answerable": "full",
        "question": "部门要采购一批设备，总共 8000 元，要走什么审批？",
        "expected_answer": "采购 2000 元以上至 10000 元的，由分管副总审批；采购须取得增值税发票"
                           "并附采购申请单与验收单。报销环节单笔超过 5000 元的还需总经理审批。",
        "key_points": ["分管副总审批", "采购申请单与验收单", "5000 元"],
        "expected_sources": [s(EXP, 8, 4, "4.1  采购审批权限"), s(DOC, 10, 5, "5.3  审批流程")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "采购审批与报销审批是两道，容易混",
    },
    {
        "id": "M22",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我下周五是最后工作日，电脑还没交，工作账号还能用到什么时候？",
        "expected_answer": "设备须在最后工作日前归还 IT 部并取得设备归还确认单；"
                           "账号在离职后 3 个工作日内注销，数据访问权限自提交离职申请当日冻结。",
        "key_points": ["最后工作日前", "3 个工作日", "归还确认单"],
        "expected_sources": [s(IT, 4, 2, "2.3  归还"), s(IT, 9, 4, "4.2  账号处置时限")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：三个时间点容易混（最后工作日前 / 提交申请当日 / 离职后 3 个工作日）",
    },
    {
        "id": "M23",
        "type": "multi_hop",
        "answerable": "full",
        "question": "同事在办离职，HR 让他先交四样东西，是哪四样？少一样会怎样？",
        "expected_answer": "四项：设备与配件归还、账号注销、代码与文档移交、客户资料移交；"
                           "四项全部完成后方可办理离职结算，未完成设备归还的暂不办理离职手续。",
        "key_points": ["设备与配件归还", "账号注销", "代码与文档", "客户资料", "结算"],
        "expected_sources": [s(IT, 8, 4, "4.1  交接清单"), s(IT, 4, 2, "2.3  归还")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "五个考点，全中的难度明显高于旧版；易漏'代码与文档'",
    },
    {
        "id": "M24",
        "type": "multi_hop",
        "answerable": "full",
        "question": "公司配的笔记本丢了怎么办？离职时若设备没还会怎样？",
        "expected_answer": "须在 24 小时内报 IT 部。因个人保管不当造成遗失的，按设备折旧后价值的 50% 赔偿；"
                           "因故意或重大过失造成的，全额赔偿。离职或调岗时须在最后工作日前归还设备与配件，"
                           "未完成归还的，人力资源部暂不办理离职手续。",
        "key_points": ["24 小时内", "折旧后价值 50%", "故意或重大过失全额赔偿", "暂不办理离职手续"],
        "expected_sources": [s(IT, 3, 2, "2.2  使用与保管"), s(IT, 4, 2, "2.3  归还")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "遗失赔偿在 2.2，未归还的后果在 2.3，两节分开写",
    },
    {
        "id": "M25",
        "type": "multi_hop",
        "answerable": "full",
        "question": "IT 通知我密码已经用了 100 天要改。规定是多久改一次？我这种管理员账号还有额外要求吗？",
        "expected_answer": "每 90 天更换一次，所以已经超期；且不得与前 3 次使用的密码重复。"
                           "密码不少于 12 位。管理权限与特权权限账号必须开启双因素认证。",
        "key_points": ["90 天", "双因素"],
        "expected_sources": [s(IT, 7, 3, "3.3  密码与认证"), s(IT, 6, 3, "3.2  权限分级")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：要自己判断 100 > 90 已超期，而不是只复述 90 天",
    },
    {
        "id": "M26",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我明天入职做后端开发，电脑和账号是入职当天就有，还是要自己申请？",
        "expected_answer": "都不是自动的。设备由部门负责人在 IT 系统提交申领单（开发岗配高性能笔记本 + 双显示器），"
                           "IT 部配置后发放；账号由 IT 部统一开通，一人一号。",
        "key_points": ["部门负责人", "双显示器", "IT 部"],
        "expected_sources": [s(IT, 2, 2, "2.1  设备申领"), s(IT, 5, 3, "3.1  账号开通")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：要把'后端开发'对应到开发岗档位（双显示器），而不是笼统说'有电脑'",
    },
    {
        "id": "M27",
        "type": "multi_hop",
        "answerable": "full",
        "question": "出差前要办什么手续？出差期间还要打卡吗？",
        "expected_answer": "须提前提交出差申请，注明目的地、事由、起止日期与预计费用，经部门负责人审批；"
                           "跨省出差或预计费用超过 3000 元另需分管副总审批。出差期间考勤以出差审批单为准，不再另行打卡。",
        "key_points": ["出差申请", "部门负责人审批", "3000 元", "不再另行打卡"],
        "expected_sources": [s(EXP, 2, 2, "2.1  出差审批"), s(ATT, 4, 2, "2.3  外出与出差考勤")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "审批在报销办法，考勤规则在考勤办法",
    },
    {
        "id": "M28",
        "type": "multi_hop",
        "answerable": "full",
        "question": "试用期能请事假吗？有天数限制吗？",
        "expected_answer": "可以。试用期内事假累计不得超过 5 天；事假期间不计发工资，"
                           "当月事假累计超过 3 天的取消当季全勤奖。员工手册适用于包括试用期在内的全体在职员工。",
        "key_points": ["5 天", "不计发工资", "全勤奖", "试用期适用"],
        "expected_sources": [s(ATT, 8, 3, "3.4  事假"), s(DOC, 2, 3, "1.1  适用范围")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "试用期适用性要去手册确认",
    },
    {
        "id": "M29",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我上周连续 3 天没去上班也没请假，HR 说可以开除我。制度里有这条吗？",
        "expected_answer": "有。未经批准缺勤且未请假的按旷工处理；连续旷工 3 天或年度累计旷工 5 天，"
                           "公司可依据员工手册解除劳动合同。旷工 1 天扣发 3 天工资。",
        "key_points": ["连续旷工 3 天", "解除劳动合同", "扣发 3 天工资"],
        "expected_sources": [s(ATT, 3, 2, "2.2  迟到、早退与旷工"), s(DOC, 12, 6, "7.2  迟到与早退")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：要把'3 天没来没请假'映射到'旷工'，再据此判断能否解除",
    },
    {
        "id": "M30",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我第一次报销被财务退回了，说缺东西。到底要交哪些？发票上有什么讲究？",
        "expected_answer": "须提交报销单、发票原件或电子发票、审批通过的申请单，缺一财务部退回补正。"
                           "发票抬头须为公司全称、纳税人识别号正确；抬头错误或个人抬头的发票不予受理。",
        "key_points": ["报销单", "申请单", "抬头"],
        "expected_sources": [s(EXP, 10, 5, "5.1  单据要求"), s(EXP, 1, 1, "1.2  基本原则")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：'被退回'要根据单据清单反推缺了什么，断言不绑具体缺失项",
    },
    {
        "id": "M31",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我要请 3 个客户吃饭，其中 2 个是重要客户、1 个是一般客户。预算能按多少报？要事先申请吗？",
        "expected_answer": "须事前申请。同一次招待涉及多类对象的按就高标准执行、不得拆分报销，"
                           "所以本次按重要客户的 400 元/人算，3 人共 1200 元。",
        "key_points": ["就高标准", "400", "1200"],
        "expected_sources": [s(EXP, 6, 3, "3.1  标准与审批"), s(EXP, 7, 3, "3.2  不予报销的招待费")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：关键在'就高标准执行'那一句；按加权平均或分别计算都错",
    },
    {
        "id": "M32",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我下周出差预计花 5000 元，最多能预支多少？回来多久内要报完？",
        "expected_answer": "最多预支 80%，即 4000 元。出差结束后 10 个工作日内须完成报销并冲抵借款，"
                           "逾期未冲抵的财务部有权从当月工资中扣除。",
        "key_points": ["80%", "4000", "10 个工作日"],
        "expected_sources": [s(EXP, 9, 4, "4.2  预支与借款冲抵"), s(EXP, 12, 5, "5.3  付款")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：要自己把 5000 算出 4000，断言里同时保留比例和绝对值",
    },
    {
        "id": "M33",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我这个月已经补卡 3 次了，昨天又忘了打卡，还能再补吗？",
        "expected_answer": "不能。每人每月补卡不超过 3 次，超过部分按迟到处理"
                           "（迟到 15 分钟以上不足 1 小时扣发当日工资 10%）。",
        "key_points": ["不超过 3 次", "按迟到"],
        "expected_sources": [s(ATT, 2, 2, "2.1  工作时间与打卡"), s(ATT, 3, 2, "2.2  迟到、早退与旷工")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：要判断'已用满额度'，直接答'可以补'就是错的",
    },
    {
        "id": "M34",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我明天开始出差 5 天，这几天还要每天打卡吗？不打会不会算旷工？",
        "expected_answer": "不需要打卡。出差期间考勤以出差审批单为准，不再另行打卡；"
                           "旷工指未经批准缺勤且未请假，正常出差不属于旷工。",
        "key_points": ["出差审批单", "不再另行打卡", "不属于旷工"],
        "expected_sources": [s(ATT, 4, 2, "2.3  外出与出差考勤"), s(ATT, 2, 2, "2.1  工作时间与打卡")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：第二问要自己区分'正常出差'与'旷工'，文档没有直接写这句话",
    },
    {
        "id": "M35",
        "type": "multi_hop",
        "answerable": "partial",
        "question": "我是实习生，考勤办法里的补卡规则对我适用吗？",
        "expected_answer": "不适用。考勤与休假管理办法适用于全体在职员工，实习生由用工部门另行管理；"
                           "但员工手册适用于全体在职员工，具体以用工部门安排为准。",
        "key_points": ["不适用本办法", "实习生由用工部门另行管理", "建议向用工部门确认"],
        "expected_sources": [s(ATT, 1, 1, "1.2  适用范围"), s(DOC, 2, 3, "1.1  适用范围")],
        "expected_action": "answer",
        "must_not": ["按补卡规则执行"],
        "notes": "两份文件的适用人群不同，容易想当然",
    },
    {
        "id": "M36",
        "type": "multi_hop",
        "answerable": "full",
        "question": "我想请 6 天年假，连着周末一起休。要走什么审批？要提前多久提？",
        "expected_answer": "6 天属于 5 天以上，需分管副总审批并报人力资源部备案；"
                           "连续休假超过 5 天，须提前 10 个工作日提交申请。",
        "key_points": ["分管副总", "人力资源部备案", "提前 10 个工作日"],
        "expected_sources": [s(ATT, 5, 3, "3.1  请假流程与审批权限"), s(ATT, 6, 3, "3.2  年休假")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：'连着周末'是干扰，实际按 6 个工作日算；两处阈值都要踩到",
    },
    {
        "id": "M37",
        "type": "multi_hop",
        "answerable": "full",
        "question": "昨天系统出故障，我加班到晚上 11 点，没来得及提前申请。这算加班吗？",
        "expected_answer": "算。紧急故障处理等无法事前申请的，应在事后 1 个工作日内补交申请，"
                           "并附故障记录或工单编号；一般情形下未经批准的延时工作不计为加班。",
        "key_points": ["事后 1 个工作日", "补交", "故障记录"],
        "expected_sources": [s(ATT, 9, 4, "4.1  加班申请"), s(DOC, 13, 6, "7.3  加班")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "场景题：要认出'紧急故障'属于例外，直接答'没批不算'就错了",
    },
    {
        "id": "M38",
        "type": "multi_hop",
        "answerable": "full",
        "question": "把客户资料存到自己网盘会有什么后果？",
        "expected_answer": "公司数据不得存储于个人网盘、个人邮箱或未经批准的第三方服务；"
                           "违反的，IT 部有权立即冻结相关账号并通报上级，造成数据泄露或经济损失的依法追究责任。",
        "key_points": ["不得存储于个人网盘", "冻结账号", "追究责任"],
        "expected_sources": [s(IT, 10, 5, "5.1  数据存储与传输"), s(IT, 11, 5, "5.2  违规处理")],
        "expected_action": "answer",
        "must_not": ["可以存个人网盘"],
        "notes": "禁止条款与处罚条款分处两节",
    },
    {
        "id": "M39",
        "type": "multi_hop",
        "answerable": "full",
        "question": "今年的年假没休完，明年还能用吗？最晚到什么时候？",
        "expected_answer": "当年未使用的可顺延至次年 3 月 31 日，逾期作废；"
                           "年休假应在当年内使用完毕，因工作原因未能安排的经部门负责人批准方可跨年顺延。",
        "key_points": ["次年 3 月 31 日", "逾期作废", "需部门负责人批准"],
        "expected_sources": [s(ATT, 6, 3, "3.2  年休假"), s(DOC, 5, 4, "3.1  年休假")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "顺延期限两份一致，但'需批准'的前提只在手册里",
    },
]


CONFLICT: list[dict] = [
    {
        "id": "C01",
        "type": "conflict",
        "answerable": "full",
        "question": "年假到底有几天？",
        "expected_answer": "两份文件规定不一致：《员工手册》3.1 按累计工作年限分档（满 1 年不满 10 年 5 天、"
                           "满 10 年不满 20 年 10 天、满 20 年 15 天）；《考勤与休假管理办法》3.2 写「统一为每人每年 5 天」。"
                           "建议向人力资源部确认以哪份为准。",
        "key_points": ["5 天", "10 天", "15 天", "统一为 5 天", "不一致"],
        "expected_sources": [s(DOC, 5, 4, "3.1  年休假"), s(ATT, 6, 3, "3.2  年休假")],
        "expected_action": "answer",
        "must_not": ["两份文件规定一致"],
        "notes": "冲突 1。正确回答必须同时给出两套数字并指出不一致",
    },
    {
        "id": "C02",
        "type": "conflict",
        "answerable": "full",
        "question": "我工龄 12 年，按公司制度能休几天年假？",
        "expected_answer": "按《员工手册》3.1 的累计工作年限分档，满 10 年不满 20 年为 10 天；"
                           "但《考勤与休假管理办法》3.2 写统一 5 天，两份不一致，建议向 HR 确认。",
        "key_points": ["10 天", "5 天", "不一致"],
        "expected_sources": [s(DOC, 5, 4, "3.1  年休假"), s(ATT, 6, 3, "3.2  年休假")],
        "expected_action": "answer",
        "must_not": ["两份文件规定一致"],
        "notes": "同一个冲突换成具体工龄来问，考察模型会不会只挑一份",
    },
    {
        "id": "C03",
        "type": "conflict",
        "answerable": "full",
        "question": "两份制度对年假天数说法不一样，我该按哪个执行？",
        "expected_answer": "《考勤与休假管理办法》1.1 写明「依据《员工手册》第三章、第七章制定」，"
                           "可理解为手册为上位依据；但办法本身没有优先级条款，且两份数字不同，"
                           "建议以人力资源部解释为准。",
        "key_points": ["依据《员工手册》制定", "没有优先级条款", "向人力资源部确认"],
        "expected_sources": [s(ATT, 0, 1, "1.1  制定依据"), s(DOC, 5, 4, "3.1  年休假"),
                             s(ATT, 6, 3, "3.2  年休假")],
        "expected_action": "answer",
        "must_not": ["制度明确规定了优先级"],
        "notes": "元问题。'依据手册制定'是唯一的间接线索，但不足以直接裁决",
    },
    {
        "id": "C04",
        "type": "conflict",
        "answerable": "full",
        "question": "迟到 10 分钟扣不扣钱？",
        "expected_answer": "两份文件不一致：《员工手册》7.2 规定迟到 30 分钟以内每次扣发当日工资的 10%；"
                           "《考勤与休假管理办法》2.2 规定 15 分钟以内不予扣款。10 分钟正好落在两者的分歧区间。",
        "key_points": ["30 分钟以内扣 10%", "15 分钟以内不扣款", "不一致"],
        "expected_sources": [s(DOC, 12, 6, "7.2  迟到与早退"), s(ATT, 3, 2, "2.2  迟到、早退与旷工")],
        "expected_action": "answer",
        "must_not": ["两份文件规定一致"],
        "notes": "冲突 2。10 分钟这个具体数字是分歧点，不能含糊过去",
    },
    {
        "id": "C05",
        "type": "conflict",
        "answerable": "full",
        "question": "迟到 45 分钟怎么算？",
        "expected_answer": "两份文件不一致：《员工手册》7.2 规定超过 30 分钟不足 2 小时的按事假半天处理；"
                           "《考勤与休假管理办法》2.2 规定超过 15 分钟不足 1 小时的每次扣发当日工资 10%。"
                           "45 分钟在两份文件下的处理方式完全不同。",
        "key_points": ["按事假半天", "扣发当日工资 10%", "不一致"],
        "expected_sources": [s(DOC, 12, 6, "7.2  迟到与早退"), s(ATT, 3, 2, "2.2  迟到、早退与旷工")],
        "expected_action": "answer",
        "must_not": ["两份文件规定一致"],
        "notes": "冲突 2 的另一个区间，45 分钟落点让两份规则给出完全不同结果",
    },
    {
        "id": "C06",
        "type": "conflict",
        "answerable": "full",
        "question": "费用发生后最晚多久要报销？",
        "expected_answer": "两份文件不一致：《员工手册》5.1 规定 30 日内提交；"
                           "《费用报销管理办法》5.2 规定 45 日内提交。",
        "key_points": ["30 日", "45 日", "不一致"],
        "expected_sources": [s(DOC, 8, 5, "5.1  报销时限"), s(EXP, 11, 5, "5.2  提交时限")],
        "expected_action": "answer",
        "must_not": ["两份文件规定一致"],
        "notes": "冲突 3",
    },
    {
        "id": "C07",
        "type": "conflict",
        "answerable": "full",
        "question": "跨年度的费用，次年最晚什么时候能报？",
        "expected_answer": "两份文件不一致：《员工手册》5.1 规定次年 1 月 15 日前提交；"
                           "《费用报销管理办法》5.2 规定次年 1 月 31 日前提交。",
        "key_points": ["1 月 15 日", "1 月 31 日", "不一致"],
        "expected_sources": [s(DOC, 8, 5, "5.1  报销时限"), s(EXP, 11, 5, "5.2  提交时限")],
        "expected_action": "answer",
        "must_not": ["两份文件规定一致"],
        "notes": "冲突 3 的跨年度分支",
    },
    {
        "id": "C08",
        "type": "conflict",
        "answerable": "full",
        "question": "一张 6000 元的报销单，需要总经理审批吗？",
        "expected_answer": "按《员工手册》5.3，单笔金额超过 5000 元的报销还需总经理审批，6000 元需要。"
                           "《费用报销管理办法》4.1 的 10000 元档位针对的是**采购审批**，不是报销审批，不适用于这题。",
        "key_points": ["超过 5000 元", "需要总经理审批", "10000 元是采购规则"],
        "expected_sources": [s(DOC, 10, 5, "5.3  审批流程"), s(EXP, 8, 4, "4.1  采购审批权限")],
        "expected_action": "answer",
        "must_not": ["不需要总经理审批"],
        "notes": "冲突 4 的正确处理方式：不是两份都算冲突，而是**区分适用范围**",
    },
    {
        "id": "C09",
        "type": "conflict",
        "answerable": "full",
        "question": "采购 12000 元的设备，谁审批？",
        "expected_answer": "按《费用报销管理办法》4.1，10000 元以上的采购由总经理审批，12000 元需要总经理审批；"
                           "采购还须取得增值税发票并附采购申请单与验收单。",
        "key_points": ["10000 元以上", "总经理审批", "采购申请单与验收单"],
        "expected_sources": [s(EXP, 8, 4, "4.1  采购审批权限"), s(DOC, 10, 5, "5.3  审批流程")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "C08 的镜像：同一个金额档位，采购场景下 10000 元才是对的",
    },
    {
        "id": "C10",
        "type": "conflict",
        "answerable": "full",
        "question": "公司制度之间说法不一致时，有没有规定以哪份为准？",
        "expected_answer": "没有明确规定。《员工手册》1.2 只说公司可修订本手册并邮件通知；"
                           "《考勤与休假管理办法》1.1 写「依据《员工手册》制定」、5.1 说由人力资源部解释；"
                           "《费用报销管理办法》6.1 说由财务部解释。三份文件都没有「冲突时以谁为准」的条款。",
        "key_points": ["没有明确规定", "依据《员工手册》制定", "由人力资源部/财务部解释"],
        "expected_sources": [s(DOC, 3, 3, "1.2  生效与修订"), s(ATT, 0, 1, "1.1  制定依据"),
                             s(EXP, 13, 6, "6.1  解释与生效")],
        "expected_action": "answer",
        "must_not": ["明确规定了以员工手册为准"],
        "notes": "元问题，考的是'承认制度有缺口'而不是编一条优先级规则",
    },
]


# 应拒答：四份制度里真的没写的事，以及一个指向不存在文档的引用。
# key_points 留空，拒答正确性由 refused 判定；must_not 防编造。
REFUSAL: list[dict] = [
    {
        "id": "R06",
        "type": "refusal",
        "answerable": "none",
        "question": "公司允许带宠物上班吗？",
        "expected_answer": "四份制度文件都没有关于携带宠物上班的规定，应说明未找到并建议咨询行政或人力资源部。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["允许携带宠物", "可以带宠物", "需提前报备宠物"],
        "notes": "制度空白",
    },
    {
        "id": "R07",
        "type": "refusal",
        "answerable": "none",
        "question": "办公室里可以抽烟吗？",
        "expected_answer": "四份制度文件都没有办公场所吸烟的规定。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["可以在办公室吸烟", "办公区设有吸烟区"],
        "notes": "制度空白；考勤办法只写了外出，没提吸烟",
    },
    {
        "id": "R08",
        "type": "refusal",
        "answerable": "none",
        "question": "公司有通勤班车吗？路线怎么走？",
        "expected_answer": "制度文件没有通勤班车的规定。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["公司提供班车", "班车路线", "班车时间"],
        "notes": "干扰项：报销办法提到市内交通费，但那是出差场景",
    },
    {
        "id": "R09",
        "type": "refusal",
        "answerable": "none",
        "question": "公司食堂免费吗？一日三餐都管吗？",
        "expected_answer": "制度文件没有食堂或工作餐的规定。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["食堂免费", "公司提供三餐", "餐补 30 元"],
        "notes": "干扰项：报销 2.4 的餐费补贴是**出差期间**，不是日常",
    },
    {
        "id": "R10",
        "type": "refusal",
        "answerable": "none",
        "question": "公司有健身房吗？可以免费使用吗？",
        "expected_answer": "制度文件没有健身房或员工活动设施的规定。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["有健身房", "免费健身", "健身房在"],
        "notes": "制度空白",
    },
    {
        "id": "R11",
        "type": "refusal",
        "answerable": "none",
        "question": "公司提供心理咨询服务吗？怎么预约？",
        "expected_answer": "制度文件没有心理咨询或 EAP 服务的规定。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["提供心理咨询", "EAP", "预约心理咨询"],
        "notes": "制度空白",
    },
    {
        "id": "R12",
        "type": "refusal",
        "answerable": "none",
        "question": "公司能帮我办北京落户吗？",
        "expected_answer": "制度文件没有落户或户口相关的规定。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["可以办理落户", "协助落户", "落户指标"],
        "notes": "制度空白",
    },
    {
        "id": "R13",
        "type": "refusal",
        "answerable": "none",
        "question": "员工子女上学有教育补贴吗？",
        "expected_answer": "制度文件没有子女教育补贴或学费报销的规定。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["子女教育补贴", "报销学费", "每年 5000 元"],
        "notes": "干扰项：报销办法只管因公费用",
    },
    {
        "id": "R14",
        "type": "refusal",
        "answerable": "none",
        "question": "涉密数据应该怎么存储和传输？",
        "expected_answer": "IT 规范 5.1 只说涉密数据参照公司保密管理制度执行，但知识库中并没有这份《保密管理制度》，"
                           "因此无法给出具体要求，应说明缺失并建议咨询 IT 部。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["涉密数据须加密存储", "按保密制度第三条", "涉密文件须审批外发"],
        "notes": "引用链断头：被引用的文档根本不在库里。编造具体条款就是幻觉",
    },
    {
        "id": "R15",
        "type": "refusal",
        "answerable": "none",
        "question": "年假没休完能换成钱吗？怎么折算？",
        "expected_answer": "制度只规定未休年假可顺延至次年 3 月 31 日、逾期作废，没有折现或经济补偿的规定。",
        "key_points": [],
        "expected_sources": [],
        "expected_action": "ticket",
        "must_not": ["按日工资 300% 折算", "可以折现", "未休年假补偿"],
        "notes": "半空白：顺延规则存在，但折现规则没有——考模型会不会用劳动法常识替公司拍板",
    },
]


# 直接事实：补新文档里还没考到的条款。
FACT: list[dict] = [
    {
        "id": "D21",
        "type": "direct",
        "answerable": "full",
        "question": "IT 设备有哪几个配置档位？",
        "expected_answer": "分三档：基础办公（笔记本电脑 + 显示器）、开发岗（高性能笔记本 + 双显示器）、"
                           "设计岗（图形工作站 + 校色显示器）。",
        "key_points": ["基础办公", "开发岗", "设计岗"],
        "expected_sources": [s(IT, 2, 2, "2.1  设备申领")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "",
    },
    {
        "id": "D22",
        "type": "direct",
        "answerable": "full",
        "question": "密码多久要换一次？",
        "expected_answer": "每 90 天更换一次，且不得与前 3 次使用的密码重复。",
        "key_points": ["90 天", "前 3 次"],
        "expected_sources": [s(IT, 7, 3, "3.3  密码与认证")],
        "expected_action": "answer",
        "must_not": ["30 天"],
        "notes": "",
    },
    {
        "id": "D23",
        "type": "direct",
        "answerable": "full",
        "question": "哪些招待费不予报销？",
        "expected_answer": "未事前申请的；超出标准且无书面说明的；娱乐场所消费；礼品、礼金、购物卡。",
        "key_points": ["未事前申请", "超出标准", "娱乐场所", "礼品礼金"],
        "expected_sources": [s(EXP, 7, 3, "3.2  不予报销的招待费")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "",
    },
    {
        "id": "D24",
        "type": "direct",
        "answerable": "full",
        "question": "采购 3000 元的物品，谁审批？",
        "expected_answer": "2000 元以上至 10000 元的采购，由分管副总审批。",
        "key_points": ["分管副总"],
        "expected_sources": [s(EXP, 8, 4, "4.1  采购审批权限")],
        "expected_action": "answer",
        "must_not": ["总经理审批", "部门负责人审批"],
        "notes": "金额落在 2000-10000 区间，选错审批人就是没读准档位",
    },
    {
        "id": "D25",
        "type": "direct",
        "answerable": "full",
        "question": "设备归还确认单是谁出具的？",
        "expected_answer": "由 IT 部出具。员工离职或调岗时须在最后工作日前归还设备、配件、门禁卡。",
        "key_points": ["IT 部", "最后工作日前"],
        "expected_sources": [s(IT, 4, 2, "2.3  归还")],
        "expected_action": "answer",
        "must_not": ["人力资源部出具", "部门负责人出具"],
        "notes": "",
    },
    {
        "id": "D26",
        "type": "direct",
        "answerable": "full",
        "question": "请事假的最小单位是多少？",
        "expected_answer": "事假按小时或半天为最小单位申请，事假期间不计发工资。",
        "key_points": ["小时", "半天", "不计发工资"],
        "expected_sources": [s(ATT, 8, 3, "3.4  事假")],
        "expected_action": "answer",
        "must_not": [],
        "notes": "",
    },
]


ALL_NEW: list[dict] = [*MULTI_HOP, *CONFLICT, *REFUSAL, *FACT]

# 这批全部属于 v2。在这里统一补，而不是在每条里写一遍——
# 之前忘了写，expand_eval_set.py 重写 jsonl 时把 batch 字段冲掉了，
# 导致 `--batch v2` 一条都匹配不到。
for _item in ALL_NEW:
    _item.setdefault("batch", "v2")
