# 0.4 工程说明与面试阅读

0.4补齐任务后沉淀→候选种子→GEPA→首次发布/已有版本更新。实现与命令见 [0.4升级报告](UPGRADE_REPORT_0.4.md)。下文0.3可靠执行设计继续适用。

## 经验获取与优化的衔接

`DurableExecutor`在可信完成后调用`skills/acquisition.py`，从真实observe/action/observe记录适配Curator材料。规则检查动作、成功观察与完整性；SQLite按任务保存沉淀决定，并对脱敏过程去重。候选与provenance在同一事务写入；崩溃后能找回已提交草稿。已满足目标、失败、证据不足与重复过程不产生新草稿。长轨迹仅向草稿提供最多30个真实步骤（含末次观察），完整轨迹另存。

`TaskSpec.learn`默认开启，CLI/API可用`false`关闭；`BenchmarkRunner`强制关闭，所以训练、开发和冻结验收不会反过来生成日常经验。Curator只接收执行材料，不接收隐藏契约。基于规则的“值得沉淀”仍只是候选资格，不保证复用价值。

`GEPASkillOptimizer`接受active或未生效种子。首次发布比较无Skill基线；更新比较当前active版本。候选无须先激活，原种子足够好时也可以直接取得首次资格。发布重查seed、champion、正文、评测器、权限与冻结集来源，事务切换immutable内容引用。原始沉淀草稿的直接approve被DB证据门禁拒绝。

`RunRequest.optimization`可显式绑定任务集和预算；`continue_learning`为CLI/API共用的串联阶段。它在工作线程中调用同步GEPA，引擎内部建立自己的事件循环。HTTP断开不拥有后台任务；正在运行的同步搜索受预算约束，取消等待不会强制杀死线程。自动发布仍只允许本地fixture。阶段启动被持久化；已启动但没有结束记录的阶段不会在resume中自动重跑，需检查证据后显式用新验收集再尝试。

两个候选概念不同：GEPA内部保留搜索历史及优势候选；SkillRepository保存任务沉淀草稿与最终评测候选。搜索预算限制单次成本，不是固定候选数上限；长期Artifact/数据库归档仍未自动化。

## 项目主线

PocketAgent Evolve 将“能调用手机工具”推进到“能核验目标、恢复执行、比较技能版本”。新主入口是 `python -m pocket_agent` 和 `pocket_agent.api:create_app`。旧聊天模块仍在，3D资源不属于本次清洁开源发行。

## 核心代码阅读顺序

1. `pocket_agent/agents/contracts.py`：JSON Pointer、严格类型比较、列表元素子集匹配、postconditions/invariants。它不执行生成代码。
2. `pocket_agent/runtime/executor.py`：持久 observe→decide→act 图、稳定任务身份、租约、恢复边界、隐藏oracle与实际usage记录。
3. `pocket_agent/runtime/ledger.py`：写入前保留intent，确认后保存结果；重复恢复复用结果，未知结果不重新派发。
4. `pocket_agent/runtime/backends.py`：持久本地fixture与实际MCP设备适配；fixture不是Android模拟器。
5. `pocket_agent/mcp/sdk.py`：官方SDK2的连接、协议自动兼容、分页和类型归一化。Policy仍在Manager边界。
6. `pocket_agent/evaluation/harness.py`：分组隔离、复位、同一执行器的重复试验与统计。
7. `pocket_agent/skills/optimization.py`：实际GEPA适配器、train/dev搜索、一次性冻结测试、预算与候选证据。
8. `pocket_agent/skills/repository.py`：内容不可变文件、数据库active引用切换、基线CAS、版本回滚和发布门禁。

## 恢复语义

checkpoint以sync durability保存图进度，确保动作前决策已落盘；action ledger保存外部动作意图与已知结果。两者不与远端设备共享一个事务。

| 崩溃位置 | 恢复行为 |
|---|---|
| 决策后、动作前 | 继续持久化的动作节点，不重新生成另一条决策 |
| ledger已确认、图节点未完成checkpoint | 返回已保存结果，不再次点击 |
| 远端可能已执行、本地没有结果 | 保留unknown，禁止盲重试 |
| unknown后显式resume | 先重新观察；目标已满足可完成，否则继续停在unknown |
| actor/skill/任务/runtime/tool schema变化 | 拒绝沿旧身份恢复；需要独立的新任务或迁移方案 |

租约默认90秒，执行中续约，同一设备限制并发。进程被强制终止后须等待租约过期。正常关闭会释放租约。它是单SQLite部署的最小工作者协调，不是分布式调度平台。CLI/API支持显式恢复，不会在重启时自动重放所有旧任务。

## 任务契约与观察

`equals`严格区分布尔与数字；`contains`在列表中寻找包含指定字段的元素；`exists`检查路径；`unchanged`对比初始状态。路径必须是JSON Pointer，缺失字段不能当成null通过。

模型只接收任务描述、Skill文本、公开观察、工具schema和近期轨迹，不接收最终判分契约。任务作者仍需保证契约确实表达目标，服务返回的状态可信。商业App只有截图、缺少可靠状态时，不能宣称已得到任意任务的确定性oracle。

真实MCP观察优先读取`structuredContent`，或解析文本中的JSON对象；非结构化/失败观察拒绝通过。不同服务schema不同，需按实际字段写契约，不能直接照抄fixture的`/elements`。

## GEPA与验证门禁

GEPA真正运行候选搜索；本项目实现其evaluation/feedback adapter。每次候选都调用同一DurableExecutor。前置工具权限固定，优化器只能修改Skill正文，不能修改任务oracle。

搜索只用train/dev。最终dev胜出版本再与基线比较冻结holdout，并在数据库消耗suite hash；重复试探同一测试集会被拒绝。分组/重复样例检查可防明显泄漏，但不能自动识别语义近重复，因此App/模板划分仍需任务作者审查。

自动晋升要求显式`--auto-promote`，只覆盖local_fixture、有限UI工具、至少2个holdout任务且每项至少2次。该门槛验证控制流，不构成统计显著性或真实Android能力证明。设备评测有显式reset契约，并要求`--allow-device-eval`；真实设备自动晋升尚未启用。

Skill发布先写内容寻址文件，校验内容后再事务切换active引用；预期champion hash不匹配则拒绝发布。崩溃可能留下未引用文件，但不会把半写文件激活。回滚切换引用，不改写历史版本内容。

## 如何做有价值的实验

固定模型版本、任务、工具schema、上下文上限、reset seed、步数和超时，比较人工Skill、同任务族压缩轨迹检索、GEPA候选。搜索成本和部署成本分开报；失败和重试也计费。未知usage写null。

先用少量任务检查接口和判分，再冻结未见任务。重复运行按任务配对分析；同任务3次不是3个独立任务。模拟、真实Android和跨App结果分开报告。infra_error预先定义，同时给全部尝试与有效环境成功率，不能只剔除不利样本。

## 面试追问

- **为什么不换框架？** LangGraph已有所需机制；实际改进来自目标验证、持久动作语义和反馈质量。
- **为什么不是完整RSI？** 当前是有边界的外部Skill优化；尚未证明改进机制本身持续变得更强，更未训练基础模型。
- **为什么不直接RAG？** 原始轨迹检索是必须比较的强基线；Skill抽象可能丢失有效上下文。
- **checkpoint能保证exactly-once吗？** 不能；图与设备没有共享事务，必须处理unknown。
- **自动评测如何防作弊？** 隐藏oracle、固定工具权限、独立状态读取、冻结集一次性使用、源代码与候选哈希绑定。
- **有什么负结果？** 模型调用可能认证失败；新技术未必提升；没有真机实验就不报真机分数。保留负结果比挑选成功录像更可信。
- **何时再加视觉或记忆框架？** 错误分析证明定位或经验更新是主要瓶颈，且专项实验优于简单基线时。
