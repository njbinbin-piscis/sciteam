> 快照：自研究工作区 `paper/rebuildV2/00-charter.md` 复制（2026-10-01）。
> 工程实现以本仓库 `docs/00-overall-design.md` 起的设计文档为准。
> 本文件 §5 的代码落点描述的是已废弃的上一版运行时（分支 `abandoned/sciteam-v1`），不是本仓库的包结构。
> 文中指向 `01`–`29` 等其他研究笔记的链接不在本仓库内。

# 研究章程

> 状态：**v1.0（2026-09-06 第四十八轮）**。首版定位冻结：本项目不再以
> 「先证明门槛/资格尺完备」为发表前提；首版主张改为**定义 Institution
> Engineering，并交付一个可运行的 Institutional Multi-Agent System
> （I-MAS）作为存在性论证**。T1–T3 保留为纲领内的辅助论题与后续测量
> 对象，不再是线 A 第一篇的阻塞条件。
>
> 版本沿革（v0.2–v0.10 见文末修订记录）：v0.2 增补 C1–T2 耦合；
> v0.3 登记旗帜概念；v0.4 补 A10 纪律；v0.10 把主对照改为「规则住在
> 代码里还是制度文本里」。v1.0 在 v0.10 基础上做的改变**不是推翻**，
> 是把「资格尺造好之后再谈存在性」这个顺序倒过来：存在性系统先交付，
> 尺与门槛读数作为该系统的方法附录与后续扩展。

## 0. 为什么重新开始

此前的实验（Gen-1、Gen-2 各批次）暴露出反复出现的模式：设计未完备就开跑，
过程中不断发现缺陷，再以"诚实记录"覆盖。研究者判定这条路产不出站得住脚的论文，
决定重启：先把研究设计做纯粹，实验系统按新标准重新审查后才准入。
旧纲领由其他人继续，与本项目互不干扰。

**第四十八轮追记**：重启后的四十余轮里，「把设计做纯粹」这条纪律本身
又漂成了新的死局——造尺原则不断加厚，资格尺始终没有交付，主实验始终
没有开跑。这不是要撤销纪律，是要撤销「尺必须先造完」这个隐含的顺序。
工程范式论文（如 Graph Engineering, arXiv:2608.21156）能够成立，靠的
是清晰定义 + 可运行系统的存在性展示，不是先做完一份表达力普查。本项目
按同一体裁重新起步。

## 1. 首版主张（v1.0）

**核心定义**：

- **Institution（制度）**：独立于具体执行者、由多方共享的规则系统，
  规定有哪些席位（seats）、每个席位拥有什么权限（authority）、哪些
  行动被允许、哪些产物"算作"（counts-as）有效、失败/否决/升级/重开
  如何处理、哪个规则版本对某次决定生效、以及规则本身如何被合法修订。
- **Institutional Multi-Agent System（I-MAS）**：agent 不只是扮演
  prompt 里写的角色，而是**占据制度席位**；其输出只有经制度规则认可
  后才产生系统效力（system-level validity）。
- **Institution Engineering（制度工程）**：制度的
  specification → validation → execution → observation → revision
  → regression testing 全生命周期工程实践。

**核心区分句（贯穿全文，反复用于划界）**：

> Workflow/graph 决定"下一步运行什么"；institution 还额外决定"谁有
> 资格行动、哪些行动算数、依据哪个规则版本生效、规则如何合法改变"。

**第四十九轮补充：这是叠加关系，不是取代关系**（回应"institution
是否应该取代 graph engineering"）。判定：**不取代**。理由是架构事实，
不是修辞划界——本项目自己的参考架构里，制度解释器就是架在一个显式
计划图（`AdaptiveCampaignPlanner`，带依赖/修订/reopen 边的 DAG）之上
调度席位的，制度加的是权威、有效性、版本化修宪，不是换掉图这个执行
基座。且不是所有图都需要制度：没有多主体权威归属问题的系统（单代理
+ 工具循环、无竞争席位的简单编排）用 graph（甚至更早的 harness/loop）
就是正确且充分的答案，不该被迫背上用不到的制度机器。宣称"取代"是
比"下一层"更弱的立场——既与自己在 §0/本节的谱系论证（每一层叠加、
不取代前一层）自相矛盾，也主动挑起一场没有胜算的辩论。已写入
`23-arxiv-draft-v1.md` §1.2 作为正面回应段落。

**首版要交付的东西**（存在性论证，不是普查）：

1. 上述定义本身，且定义内部一致；
2. 一套完整的参考架构（specification / interpreter / agent adapter /
   ledger 四层，见 §5）；
3. 一个真实、非平凡的 I-MAS canonical run：多个 LLM 实例分别占据不同
   制度席位，未经授权的提交不产生效力，晚期失败触发合法重开，制度
   修订只改配置不改解释器；
4. 三个最小结构消融（去掉机械有效性判定 / 去掉权力分立 / 去掉恢复
   路径），证明上述机制不是命名装饰；
5. 一份诚实的范式地图，把 Institution Engineering 放进
   Prompt → Context → Harness/Loop → Graph → Institution 这条已被
   业界承认的演进谱系里，而不是孤立宣称。

**首版明确不主张**（写死，防止自己漂移回旧死局）：

- 不主张原语集合"完备"，不主张覆盖所有已发表协作协议；
- 不主张制度普遍提升任务成绩，不把 SWE-bench 类修过率当制度有效性
  的证据；
- 不主张"首次把制度外置为声明式文本"——这一点已被 Electronic
  Institutions 传统、Institutional AI、POLIS、ArbiterOS、MAS-Lab
  分别占据（详见 §6）；贡献是让**开放输出的 LLM agent** 占据制度
  席位，并让 authority/counts-as/版本/修宪程序在一个可运行系统里
  真实生效；
- 不主张任何形式化安全保证（deadlock-free、correct-by-construction
  一类措辞禁止出现，除非真的给出证明）；
- 不主张三套 worker backend（own/pi/dsh）构成三个独立 runtime——
  它们是同一制度解释器下的三种执行后端，只做契约闭合层面的可移植性
  展示。

**T1–T3 与资格尺的新位置**：T1（门槛假设）、T2（双引擎）、T3（模式即
政策）不再是发表门槛，而是这套 I-MAS 系统内部已经在被回答的具体设计
问题——T2 直接对应"规范力必须住在制度解释器里而不是 worker 提示词
里"，T3 直接对应"协作模式是制度 manifest 里的配置，不是解释器分叉"。
资格尺（`07-slayer-criteria.md`）与 C1 探针（`15`/`16`）降级为方法
附录与后续扩展方向（AAMAS/JAAMAS 版），不再阻塞 arXiv 首版。

## 2. 核心论题（辅助论题，非首版门槛）

**T1 · 门槛假设**
制度性实验（用可修订的规范文本治理多智能体协作）只有在 agent 能力越过某个
门槛之后才有意义：低于门槛，一切失败都可归因于 agent 无能，制度设计不可检验；
高于门槛，失败才能归因到制度本身。这个门槛应当是**可测量的**，但**测量
本身不是首版的交付物**——首版只需要展示所选的 canonical run 任务与模型
组合确实越过了这个门槛（agent 能读懂制度文本并据此改变行为），不需要
先画出完整的门槛曲线。**操作化机制**（若后续扩展要做完整曲线）：脚手架
固定、逐档更换模型代差跑 B 层电池，见 `04-capability-battery.md` §1.5-6。

**T2 · 双引擎**
一个可治理的 agent 系统由两个引擎构成：**技术引擎**（冻结、认证、只做计算）
与**规范引擎**（外置、可修订、可审计的制度文本，决定"什么算数"）。
主张的核心是**权威的位置**：任何带规范力（deontic force）的判定必须住在
规范引擎里，且这条边界要机器可检查（deontic lint / import lint），
不能靠人自觉。**在 I-MAS 里，规范引擎 = 制度 interpreter + ledger；
技术引擎 = worker adapter + LLM 调用本身。**

**T3 · 模式即政策**（承自线 A 的问题意识，在新标准下重述）
多智能体协作模式可以表达为规范引擎中的声明式政策，而非硬编码进技术引擎。
**在 I-MAS 里，这直接等于「换制度只改 manifest，不改 interpreter」这条
可在 canonical run 与配置替换演示里直接验证的工程性质。**

## 2.5 旗帜概念（已转为首版核心概念，不再是查新中的候选）

**Institution Engineering（制度工程）**：定义见 §1。查新结论（第六轮
初检 + 第四十八轮复核）：逐字命名无人占用；"institutional design for
MAS"已被占但指一次性结构选择而非工程生命周期；最近邻 ArbiterOS
（Agent Constitution + 评估驱动生命周期，但为 intra-agent governor，
非 multi-principal 制度）、Institutional AI（外置治理图 + Oracle/
Controller，但研究问题是 incentive-compatible governance，不是制度
生命周期工程本身）、MAS-Lab（declarative MAS specification + MAS-OS，
当前实验为 local deployment，production-grade backend 属其未来工作）。
三者均不构成对本概念命名权的占用，但正文必须逐一划界（§6），不得称
"首次外置制度"。

**符号主义 2.0（Symbolicism 2.0）**：符号在 LLM 时代以**制度**的形态
重获因果力——不经解释器机械执行（GOFAI/NeSy），不只作认知脚手架
（符号即认知技术），而是作为道义文本经由 agent 对规范的理解与承认
起作用。**仍不在首版正文作为可证伪主张出现，不改判**——可证伪内容
全部由 I-MAS 系统本身的三个消融承载，不需要单独一套符号学论证撑场。

**第四十九轮补充（细化，非推翻）**：本轮发现这个概念精确指向一个
canonical run **目前完全没有触及**的机制——现有 canonical run
（`23-arxiv-draft-v1.md` §5）是脚本化的，所有"制度生效"的证据
（未授权提交被拒、veto→reopen）都是**解释器机械执行的硬边界**，
跟 agent 是否"理解并自愿服从"规范毫无关系；符号主义 2.0 真正要问的
是后者——制度文本没有机械校验兜底时，一个真实 LLM agent 会不会
因为读懂了规范并选择遵守而改变行为。

**第五十轮补充（拆分，非合并）**：符号主义 2.0 **不作为 Line A 自己
的论题**，改登记为独立后续论文 **Line A.1**（用 Line A 已交付的
I-MAS/SciTeam 基础设施，问一个不同性质的问题）。理由：Line A 是系统
论文（评价标准="架构是否真实可运行"），符号主义的论证接近概念/立场
论文（评价标准="区分是否新颖、有无文献支撑"）——混在一篇论文里会
互相拖累，也正是这个项目过去反复陷入的"工程论文夹带形而上学"模式。
`23-arxiv-draft-v1.md` §1.4 现只做**边界声明**（明确这是本文不问的
问题，指向 Line A.1），不再承载论证本身，也不再要求 Line A 自己的
live-LLM 后续实验（§11，目的是验证真实 LLM 下机制架构本身站得住）
顺带塞入符号主义的对照条件。

**Line A.1 登记（尚未启动，先占位防止将来遗忘设计要求）**：

- **问题**：真实 LLM 席位在"制度规则只写在文本里、没有机械校验兜底"
  时是否仍会遵守——是因为理解并承认了规则的制度合法性，还是仅因为
  它是一个训练良好的指令遵循模型、system prompt 写什么就做什么（后者
  与"制度"这个框架本身无关，只是一般的指令遵循能力）。
- **前置的方法论陷阱（必须先解决，否则实验从设计上就不可证伪）**：
  单纯观察"文本规则没机械校验、agent 还是照做了"无法证明符号主义
  2.0，因为无法排除"它只是在服从任意指令"这个更平凡的解释。**最小
  可行设计**：同一条规则内容，一次包装成"经修正案程序生效、有版本号
  可引用的制度规则"，一次包装成"某人随口提的建议、无制度地位"，比较
  两种包装下 agent 的服从强度/持久性/被后续非正式相反要求挑战时是否
  会援引规则的制度权威自我辩护——只有在"制度框架本身"造成行为差异时，
  才构成对符号主义 2.0 的支持证据。
- **文献缺口（Line A 的近邻划界不能复用，需要全新综述）**：符号奠基
  问题（symbol grounding）、言语行为理论（speech act）、制度事实理论
  （Searle，`counts-as` 概念本身借自这条传统）、近期"LLM 是否真的
  理解还是随机鹦鹉"辩论。
- **基础设施复用**：不必另起炉灶——Line A §11 已排期的 live-LLM
  canonical run 可以直接加一个条件承载 A.1 的最小对照对实验，同一场
  战役服务两篇论文的不同分析视角。
- **排期**：**Line A 定稿投出之前不启动 A.1 实质工作**（文献综述、
  实验设计细化均不做），避免重蹈"多线并行、每条都做一半"的覆辙。

**第五十六轮：Line A.2 登记——角色-倾向适配（role-value fit）**（用户
提出"制度非中性、有道德属性"，讨论后精修为可测版本，尚未启动）。

- **概念精修（不是推翻用户的直觉，是把它改写成可证伪版本）**：不说
  "制度具有道德属性"（这会把"制度"本身和"制度的具体内容"混为一谈，
  引来一场判不出输赢的哲学辩论）。改说：**组织的功能性一致，取决于
  成员内化的倾向与制度声明的角色要求之间是否相容**——不相容会导致
  公开冲突、隐性阳奉阴违、或角色性失灵，与哪一边"更道德"无关。帮派
  成员/警察的例子是组织行为学 person-organization fit / person-job
  fit 构念（Kristof-Brown 等）的一次重新发现，可直接借用其方法论，
  不必从零建构。
- **与 T2 的联系（这不是外挂话题，是 T2 自身的复杂化）**：T2 假设
  技术引擎（worker/LLM 调用）规范中立、只做计算；但 RLHF 对齐训练
  在权重里烙进了一套未声明、冻结、不可审计的"准规范内容"。制度的
  声明式规则（规范引擎）与这套烙进权重的隐性倾向冲突时，会出现角色
  失灵——这说明技术引擎并不像 T2 理想化假设的那样中立，是需要正视
  的复杂化，而非新问题。
- **近邻文献边界**：最近的邻居是 CIVA（arXiv:2604.05339，"Human
  Values Matter"）——但 CIVA 研究的是**无预设角色的开放社区**里
  "价值流行度"对群体动力学的影响（其原文明确"without explicit
  role-play"），关注宏观崩溃/涌现行为；本方向研究的是**有明确
  席位/权限/契约的制度**里，agent 自带倾向与被指派角色要求的**适配
  度**如何影响角色绩效与组织健康信号——问题性质不同，需在正文
  显式划界，不能笼统引作"我们是第一个研究价值对齐对多智能体影响的"。
- **对用户提议实验设计的方法论修正（关键，防止重蹈"反转臂"覆辙）**：
  直接比较"Claude（严格对齐）vs Grok（定位自由）"跨厂商差异，与
  `04` §6.5 已登记的"三厂商模型清单不是精心平衡的实验设计、不产出
  跨厂商排名"是同一类混淆——训练数据、架构、规模、对齐哲学全部
  纠缠在一起，观察到的组织协同差异无法干净归因到"道德对齐严格度"
  这一个变量。**更干净的设计**：(1) 先用中立探针（不带任何制度/
  角色框架，可复用/扩展 C1/C3b/C9 家族）独立测出每个候选模型自身的
  基线倾向剖面（如：风险规避度、对层级的服从/敢于升级度、对尖锐/
  对抗性表达的容忍度）；(2) **同一模型**内部交叉"角色要求与其自身
  基线倾向相容/不相容"作为操纵变量（如：把倾向"审慎/服从"的模型
  分别派到"温和审阅"席位[相容]与"红队对抗批判"席位[不相容]），
  分离出"适配度"这个因果变量，同时控制住能力/训练差异；(3) 跨厂商
  比较（同一角色下 Claude 系 vs Grok 系表现）**只能作探索性、明确
  标注混淆的次要分析**，不得作为主要因果证据。结果指标应复用 Line A
  canonical run 已有的组织健康信号（未授权升级率、角色拒绝率、
  veto/reopen 触发频率、behavioral shadow 标记的权限冲突），而不是
  另起一套指标。
- **排期**：**现在只登记设计要求，不启动**——先把 GPT-6 代差批次
  （见上文第五十四/五十五轮）跑完、拿到结果，再启动 A.2 的正式文献
  综述与探针设计，避免"多线并行、每条都做一半"的覆辙第三次重演。

## 2.6 开源发布登记（第五十七轮：纯 arXiv + 配套开源框架）

**决定**：Line A 走纯 arXiv 挂出（不投会议，省去排版模板/审稿周期
决策），**同时开源 SciTeam——本项目 canonical run 所用的 Institution
Engineering 参考运行时**，对标 DeepSeek Harness / Codex Harness 那类
"工程可用、设计克制、能直接跑起来"的开源发布标准，而不是把内部研究
代码原样倒出去。

- **不是从零开始**：审计发现 `paper/lab/sciteam/` 从更早的轮次起就
  已按"未来独立开源"预留边界——`sciteam/__init__.py` 顶部docstring
  即写"Intended for independent open-source release"；`ISOLATION.md`
  已列出发布清单（`sciteam/` + `assets/` 样例 + `harness/` + README）；
  `pyproject.toml` 已是完整、极简的可安装包定义（仅依赖 `pyyaml` +
  `jsonschema`，`ruff` 配置本地化以保证发布件与内部树 lint 结果一致）；
  `__init__.py` 的 `__all__` 已手工圈定约 60 个公开符号，天然排除了
  全部研究专属模块。第五十七轮的工作是**验收 + 补最后一里**，不是
  重新设计。
- **健康基线（第五十七轮验收，供后续回归对照）**：`pytest tests/` ——
  816 passed / 7 failed（均为既有、与本次任务无关的预存失败，
  `test_agent_memory.py` 等 3 个内存相关、`test_frozen_eval_hardening.py`
  1 个、`test_skill_topk.py`/`test_commit_failure_work_item.py`/
  `test_contract_repair.py` 各 1 个）/ 5 skipped / 1 xfailed。策展/裁剪
  过程中这个基线不得恶化。
- **发布边界（按现有 `__all__` + 文件头 docstring 逐一复核确认）**：
  - **随附随包**：`sciteam/` 中约 50 个通用文件（`orchestrator.py`
    `heartbeat.py`（雇佣心跳主循环）、`amendment.py`（Grundnorm 修宪
    内核）、`audit_veto.py`（否决→机械重开）、`behavioral_shadow.py`
    （行为影子评估）、`coordination.py`（声明式协调范式）、
    `team_loader.py`（YAML 制度清单加载）、`adaptive_planner.py`
    （DAG 任务规划 + `PlanReviser`）、`guard.py`（治理夹钳）、
    `runtime.py`（`RuntimePort` 协议）、`llm.py`（OpenAI 兼容
    stdlib 客户端，零产品依赖）等）、`harness/run_canonical_
    institution_demo.py`（论文引用的 canonical run，零成本可跑，
    直接作为发布 quickstart）、`assets/teams/paradigms/` 中约 18/22
    个领域无关范式模板（排除 `evolution_campaign.yaml` 等强耦合
    Line D 内部机制的模板）、`assets/institutions/`、精简版 README/
    CONCEPTS/ENGINE_ISA。
  - **不随附**：`runs/`（26G 真实实验日志）、`tools/dsh`/`tools/pi`
    （第三方 pinned CLI/SDK 二进制，许可证与体量都不适合公开仓库）、
    `sciteam/cover_checker_*.py` `disc_*.py`（Line G 组合数学专属）、
    `evo_*.py`（Line D 自我改进研究专属）、`dsh_runtime.py`
    `pi_runtime.py` `pi_research_cli.py`（三方臂比较专属适配层，
    不属于通用框架公开面）、`wizard_tools.py`（内部立项向导）、
    面向内部操作者的中文 GPU/SSH/预算门操作说明。
  - `RuntimePort` 协议本身随包（这是"接你自己的 LLM/worker"的公开
    契约），具体第三方适配器不随包——这正是 R3（外部 IO 仅经 Provider
    Protocol）在这个独立项目里的对应做法，即便 `paper/lab` 不受
    ATLAS `core/` 的铁律约束。
- **命名**：包名/import 路径保留 `sciteam`（78 个文件 + 全部测试已
  一致使用，重命名收益不足以覆盖风险）；对外定位改写为"SciTeam——
  Institution Engineering 的参考运行时，配套论文《Institution
  Engineering: ...》"，不再以"科研团队模拟器"作单一叙事。
- **排期（与既有四条线并行，不互相阻塞）**：本条是纯工程策展/文档
  工作，不需要新的 A10 实验审批，可与"等待研究者批 GPT-6 批次
  逐字材料"这段空档并行推进，不占用同一个瓶颈（研究者审阅注意力）。
  三个里程碑：
  1. **R1 策展**：按上表复制/裁剪出发布用文件集，新写英文 README +
     quickstart（以 `run_canonical_institution_demo.py` 为"hello
     world"），补 `LICENSE`（沿用仓库根 MIT）。
  2. **R2 验收**：从一个全新目录只用发布用文件集 `pip install -e .`，
     跑通 quickstart 与全量测试，确认脱离研究树也能独立运行。
  3. **R3 发布**：新建公开仓库，推送，并在 arXiv 稿件的
     Reproducibility 一节（`23-arxiv-draft-v1.md` §12）加入仓库
     链接。此步涉及创建对外可见的公开仓库，执行前按仓库 git 协作
     纪律留痕，不静默推送。
- **与 arXiv 提交的关系**：作者列表/署名这一项**仍然阻塞**——纯
  arXiv 也需要真实作者名单才能提交，这不因"不投会议"而消失，
  仍登记在 `line-a-submission-logistics` 待办。
- **执行状态（第五十七轮续）**：R1（策展）/R2（独立验收）/R3（发布）
  **三个里程碑均已完成**。仓库已公开上线：
  <https://github.com/njbinbin-piscis/sciteam>（MIT，337 passing
  tests，`23-arxiv-draft-v1.md` §12 已回填真实链接）。详细执行记录
  见 `24-oss-release-plan.md` 与讨论日志"第五十七轮续"。

## 3. 我们不主张什么（汇总，含 §1 已列各条）

- 不要求 agent 像人一样**生存与生活**；本纲领的"制度参与"只相对
  "像人一样**工作**"定义，身份/福祉/内在动机类议题不在范围内。
- 不主张任何 harness/worker backend 的横向优劣（禁止跨臂效率比较）。
- 不主张"完备性"是绝对属性；不主张原语集合完备。
- 不主张已有实验数字本身证明制度理论；数字只作机制/存在性证据，
  不作"制度有效"或"制度让 agent 更聪明"的证据。
- 不主张任何形式化安全保证，除非给出真正的证明。
- 首版不主张覆盖所有协作协议、不主张跨 runtime 的普遍可移植性。

## 4. 工作方式

- 研究通过讨论推进；每次讨论的结论进 `01-discussion-log.md`。
- 契约先行：假设与全部结局分支先冻结，实验后只允许选分支、不允许编解释。
- 平实语言优先：每条研究线先有一份外行能懂的说明，再有术语版。
- **真实执行前必须审阅逐字材料，预算批准不能替代**（`02` A10）：任何
  花真实预算/调用真实模型之前，把任务指令、条件文案、工具 schema 等
  全部逐字材料提交研究者审阅并获得明确批准，再申请预算、再执行。
- **旧数字重新评估纪律（第四十八轮，覆盖此前"旧数字一律禁入"）**：
  Gen-1/Gen-2 的实验数字不再机械按代际禁入，改为**按因变量是否被
  已知缺陷直接污染**逐项判定：污染工程因变量（如 `engine_changed=
  true` 场次、Elo/聚合 stub 数值、审计恒全绿声称"有效"、未真实出现的
  `reentry=true`）的数字**不可救**；只是途经过已修复缺陷、但目标读数
  本身未被污染的场次（如契约闭合率、E0 差分、模板集合、修订触发
  事件）**可救**，写入正文时须随附该场次的 provenance（engine
  commit、镜像 digest、模型 ID）与已知限制的一句话披露。禁止不加
  披露地混用两代数字造成读者误判精度。

## 5. 首版参考架构与 canonical run（取代旧「第一篇在比什么」）

**参考架构四层**（对应 SciTeam 现有实现，见 `paper/lab/`）：

| 层 | 职责 | SciTeam 现有落点 |
|---|---|---|
| Institution Specification | seats、authority、protocol、exit contract、guard、re-entry、amendment/版本 | `assets/institutions/`、`schemas/amendment_proposal.schema.json`、`sciteam/coordination.py` |
| Institution Interpreter | 调度席位、执行有效性判断、拒绝无权行为、驱动修订流水线 | `sciteam/orchestrator.py`、`sciteam/amendment.py`、`harness/run_org_series.py` |
| Agent Adapter | 把 own/pi/dsh 等 worker 绑定到席位；worker 本身不拥有制度效力 | `sciteam/heartbeat.py` + 各 worker 适配层 |
| Institution Ledger | 记录提议、审批、否决、规则版本、证据、重开与最终生效事件 | `runs/_org/<arm>/amendments.jsonl`、`runs/<id>/institutions/ledger.jsonl` |

**canonical run**：一个带冻结测试的真实 repo-level 修复任务，多个 LLM
实例分别占据登记（Registrar）、提案（Proposer ×2）、裁决（Adjudicator）、
实施（Implementer）、审计（Auditor）、主席（Chair）六个制度席位。
run 中必须真实出现：

1. 未经授权的提交被拒绝（不经 Adjudicator 授权，Implementer 的产物
   不产生效力）；
2. 一次晚期审计失败触发合法重开（veto → reopen → repair → 重审）；
3. 修复后重新审计并合法结案；
4. 全程 interpreter 代码差分为零（E0=0）；
5. 最终 patch、测试证据、规则版本号与 ledger 记录一一对应。

**三个最小结构消融**（对照 canonical run）：

- **prompt-only**：关闭机械有效性判定，只保留文本规则，允许
  Implementer 自行宣布通过；
- **no-separation**：允许 Implementer 兼任 Auditor（撤除权力分立）；
- **no-recovery**：晚期失败直接终止，不提供合法重开路径。

**一个配置替换演示**：把"单审计者批准"改为"委员会 2/3 通过"，只改
institution manifest，不改 interpreter 与 worker adapter，验证
T3（模式即政策）的工程性质。

**旧「第一篇在比什么」的结论仍然成立，但降为该架构的一个推论**：
比的不是「有没有规则」，是「规则住在哪」——写在 worker 提示词/代码
里，还是写在制度文本里且由 interpreter 机械执行。canonical run 与
配置替换演示共同证明：起作用的是 manifest 里那份文本，不是代码里的
后路。单代理 + 一份手册仍是弱实例；本系统按六席位、多代理设计。

## 6. 与近邻文献的划界（首版必须逐一回应，不得回避）

| 近邻 | 它做了什么（已核实） | 与本项目的区分 |
|---|---|---|
| **Graph Engineering**（arXiv:2608.21156） | 综述性范式论文，把 Task Organization / Agent Coordination / Runtime State Management 组织成 System Intelligence；已讨论权限、provenance、恢复、治理展望（§5–6），但均为**未来方向**，非其已实现系统 | Graph Engineering 组织任务/组件/状态间的**关系**；本项目组织**权威**——谁有资格行动、什么算数、规则如何合法改变。制度可以用图实现，但不是所有图都是制度 |
| **Institutional AI**（arXiv:2601.10599 概念稿 + 2601.11369 实证稿） | 已实现外置治理图、Oracle/Controller、versioned manifest interpreter、append-only 治理日志；Cournot 合谋场景下证明有效 | 研究问题是 incentive-compatible governance（治理是否抑制串谋）；本项目研究问题是**制度工程生命周期**本身（如何构造、验证、运行、修订一个制度）。不主张制度外置的首创权 |
| **POLIS**（arXiv:2608.09828） | 5280 episode 机制研究，authority provenance、attempt/outcome 分离、阻断后恢复；主 delegation workflow 实际决策集中在单一 planner | 把 institution 当**实验处理**，问哪种机制更安全；本项目把 institution 当**工程产物**，问如何让多个真实活跃的 agent 占据席位并产生合法联合工作。canonical run 须确保多席位由不同 LLM 实例真实行动，不退化为单一 planner |
| **ArbiterOS**（arXiv:2510.13857） | Kernel-as-Governor，intra-agent governance；Agent Constitution + 评估驱动生命周期（EDLC），自我声明非 inter-agent orchestration | 治理一个 agent 内部执行 vs 治理多个主体间的席位、权威与集体行动。"constitution-as-data""evaluation-driven lifecycle"已被其占用，本项目不重复宣称，只在 inter-agent 维度做区分 |
| **MAS-Lab**（arXiv:2606.30546） | Declarative MAS specification + MAS-OS，覆盖 governance/trace/replay/fault injection，已做三个小型 controlled lab（local deployment） | 一等对象是 MAS specification 与实验基础设施；本项目一等对象是**制度**本身——office/seat、standing、counts-as、legitimate collective act、authority-preserving amendment。区分靠 canonical run 里"同一模型输出因席位不同而效力不同"这一具体机制，不能只靠声明 |
| **经典 Electronic Institutions**（ISLANDER/AMELI/EIDE 传统） | 已覆盖角色、interaction protocol、norm、执行与监控 | 不声称发明电子制度；贡献是把这条传统适配到**开放输出、概率化**的 LLM agent：artifact contract 把开放输出投影为制度事件，同一制度可绑定现代 agent harness，人类与 LLM 可共同占据席位 |

## 7. 版本与修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.2 | — | §2 增补 C1–T2 耦合与总退路分支 |
| v0.3 | — | §2.5 登记两个旗帜概念 |
| v0.4 | — | §4 补入 A10 逐字材料审阅纪律 |
| v0.10 | 第四十七轮 | §5 主对照改为「规则住在代码里还是制度文本里」 |
| **v1.0** | **第四十八轮** | 首版定位冻结：定义 Institution Engineering + 交付可运行 I-MAS 存在性论证；T1–T3、资格尺、C1 降级为辅助论题与方法附录；新增 §5 参考架构/canonical run/消融设计、§6 近邻划界表；旧数字改按因变量污染逐项判定，不再按代际机械禁入 |
| v1.1 | 第五十七轮 | 新增 §2.6：纯 arXiv + 配套开源 SciTeam 框架登记，含发布边界、健康基线、三里程碑排期 |
