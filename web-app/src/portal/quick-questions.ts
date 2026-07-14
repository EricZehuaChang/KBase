// 使用端问答页空会话状态下展示的快捷问题 chips（M5-1 F2 spec §3.1）。
// 四题改写自 eval/questions.jsonl 演示题库里评测通过率较高的四类问法
// （见 eval/report-tiers.md），分别覆盖住宿费标准、市内交通包干、公务卡
// 记账、文号检索四种高频提问模式——目的是让新用户一眼看出"这个知识库能
// 问什么"，而不是对着空白输入框发呆。文案是产品向表达，与题库原文措辞
// 略有出入（如去掉逗号、简化句式），不是同一条问题的逐字复制。
export interface QuickQuestion {
  id: string;
  text: string;
}

export const QUICK_QUESTIONS: QuickQuestion[] = [
  { id: "q1", text: "北京出差部级干部住宿费标准是多少？" },
  { id: "q2", text: "市内交通费是怎么包干计算的？" },
  { id: "q3", text: "公务卡消费记录多笔支出怎么列示？" },
  { id: "q4", text: "新兵办发〔2014〕76号文件是关于什么内容的？" },
];
