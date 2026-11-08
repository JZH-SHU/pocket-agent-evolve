# PocketAgent Evolve 0.3 升级报告

日期：2026-11-08。本文记录本次实际代码升级，区别于前期调研路线。项目定位调整为：**可验证、可恢复、基于执行证据改进技能的移动Agent内核**。

## 已完成的改造

| 原问题 | 本次实现 | 取舍与边界 |
|---|---|---|
| 只出现观察工具名就判成功 | 声明式OutcomeContract，严格检查目标状态、不变量和操作后观察 | 需要任务作者定义可信状态条件；无契约的旧任务改为unverified |
| Executor仅MemorySaver、恢复易重复点击 | 统一LangGraph持久执行图，稳定任务身份、SQLite checkpoint、任务/设备租约 | 保留SQLite单机部署；不宣称分布式exactly-once |
| 副作用调用统一重试 | 读写重试语义分离；写前intent、确认结果缓存、unknown停止 | 远端不可查询且无幂等键时仍有无法消除的模糊窗口 |
| 自写旧MCP传输 | 官方MCP SDK2.3适配器，现代/legacy协议兼容，分页与结构化结果 | 保留明确legacy路径；手机服务schema和安装环境仍需验证 |
| Skill评测与实际执行器分离 | BenchmarkRunner调用同一DurableExecutor；复位与oracle不交给actor | 本地fixture只是集成测试；设备评测需显式开启与复位契约 |
| 少量固定爬山迭代 | GEPA0.1.4真实库、自定义执行/反馈adapter、actor/reflection预算 | GEPA搜索不保证胜过人工Skill或轨迹检索，需等预算实验 |
| 每次评测后手动启用、文件/DB非原子更新 | 受限自动晋升、不可变内容、事务active引用、champion CAS、回滚 | 自动晋升仅local_fixture；不等于授权无人值守的真实手机行为 |
| 测试集可被外层反复试探 | train/dev/holdout分组检查，一次性冻结suite消费 | 语义近重复仍需人工审查，后续轮次应提供新测试集 |
| 无明确开源交付边界 | 新README、工程说明、MIT、第三方声明、安全说明、uv.lock与CI | 运行数据、私有Skill、3D资源与机器专属研究文件不上传 |

## 新的主路径

```text
CLI / 带鉴权的后台API
  → 持久任务与原始执行身份
  → observe → decide → Policy/ledger → backend → observe
  → 独立状态契约 → Run/Artifact证据
  → GEPA train/dev搜索 → 一次性冻结测试
  → 版本门禁 → 事务晋升 / 回滚
```

`python -m pocket_agent` 与 `pocket_agent.api` 是0.3主入口。旧`main.py/app.py/agent`保留兼容；历史聊天/3D不是新版本效果证据。新版API全体任务读取/写入路由有Bearer鉴权，只有health公开；HTTP断开不会隐式重新提交任务。

## 技术依据与采用程度

- **[GEPA（ICLR2026）](https://arxiv.org/abs/2507.19457v2)**：实际接入官方库，候选在共享执行图运行。没有重写/假冒其搜索算法。
- **[Skill-V（2026-10预印本）](https://arxiv.org/abs/2610.11781v1)**：借鉴版本化可验证契约和非退化门禁；完整论文包含7B actor训练/GRPO，本项目没有复现该训练系统。
- **[SkillEvolBench（2026）](https://arxiv.org/abs/2605.24117v1)**：借鉴acquisition/frozen deployment分离。其压缩轨迹基线经常更强，故后续必须加入轨迹检索对照。
- **[MCP当前协议与官方SDK](https://modelcontextprotocol.io/docs/2026-07-28/learn/versioning)**：使用SDK进行新旧协议适配，不只修改initialize日期字符串。
- **[LangGraph持久化与故障语义](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)**：利用持久图，同时由应用处理外部副作用未知结果。
- **MobileGym/MobileWorld/AnTrap、GUI-Owl、ACE/ReMe**：保留为下一阶段专项验证。没有为了清单齐全一次性引入，未宣称跑完外部benchmark或得到论文成绩。

## 实际验证与限制

完整回归覆盖：目标状态正反例、严格类型、操作后观察、确认动作后崩溃恢复（包括真实os._exit跨进程退出、测试中推进租约时钟）、已执行/未执行两种响应丢失、恢复身份变化、原子intent、MCP读写重试、官方SDK现代/legacy与本地HTTP服务、真实GEPA引擎、冻结测试重用拒绝、候选篡改、版本晋升与不可变回滚、API鉴权/后台执行，以及原有记忆/存储/Skill回归。

本地锁定环境完整回归 **51/51通过，23.156秒**；清洁快照在独立虚拟环境中也 **51/51通过，24.761秒**。测试结果与锁定版本见 [VALIDATION_0.3.json](VALIDATION_0.3.json)。CI已配置Linux/Windows与Python3.11/3.12矩阵；配置存在不等于每个平台已通过。

另尝试过一次使用现有配置的真实模型冒烟，只发送公开合成设置任务：返回`OpenAIAuthenticationError`。因此**不能声称真实模型任务已成功，更没有真实模型学习增益或真机成功率**。该错误不被隐藏成脚本模型的成功。后续需要有效模型凭据与可重置设备，才能进行真实性能实验。

本地脚本Demo成功只能证明图、状态与判分闭环可运行；GEPA的脚本测试证明真实库的搜索/发布集成，不证明模型真的学习。

## 重要行为变化

1. Python最低版本改为3.11，与MCP SDK2要求一致；Termux轮子与真实设备兼容性仍需单独验证。
2. 不再凭“工具调用过/步骤标completed”认定任务成功。
3. 结果未知的写动作不自动重试。强制结束进程后恢复可能需要等待90秒租约过期。
4. 沿旧checkpoint恢复要求任务、actor、Skill、runtime与工具schema匹配；不静默在代码/模型变化后重跑旧动作。
5. 冻结测试被消费后，不允许在同一suite上继续挑版本；需要新验收集。
6. Skill自动晋升范围明确限制，真实设备仍走人工审批；候选不能通过删frontmatter绕过证据门禁。

## 下一阶段的成功标准

先在真实MCP测试设备建立可复位任务集，固定模型、App版本、工具schema、seed和预算；比较人工Skill、同任务族压缩轨迹检索与GEPA。分别报告模拟/设备、同App/跨App、全部尝试/有效环境成功率，以及不确定性、误动作和每成功任务成本。候选未胜过简单基线时保留负结果，不晋升。

视觉回退只在定位错误占主导时试点；ACE/ReMe只在经验生命周期成为瓶颈时择一试验。暂不增加重复编排框架、多Agent数量或完整GUI强化学习训练。

## 开源交付

发行名称：PocketAgent Evolve。许可：MIT，保留原项目贡献者归属；外部依赖、模型和数据的许可独立。`scripts/prepare_release.py`采用文件白名单，检查明显凭据与机器路径，生成内容哈希清单；排除.env、数据库及sidecar、记忆、截图日志、生成Skill、外部Skill包、3D素材和本地研究缓存。

本报告由AI辅助实现与整理。所有“已完成”对应实际代码/检查；未运行的真实环境和未复现的论文收益均明确列出。
