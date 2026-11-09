# PocketAgent Evolve 0.4 全生命周期升级报告

日期：2026-11-09。目标：补上0.3新版执行器遗漏的任务后Skill沉淀，并让未生效草稿直接进入隔离GEPA评测，连接首次发布、已有版本升级和后续复用。

## 完整链路

```text
任务 + 当前可选Skill
  -> 观察/决策/权限/动作账本/工具/再次观察
  -> 独立状态契约验证
  -> 沉淀资格检查 + 脱敏过程去重
  -> Curator整理待评测草稿（LLM失败可回退明确标记的模板）
  -> 可选：按显式提供的任务集和预算启动GEPA
  -> train修改 / dev选取 / 一次性holdout验收
  -> 内容与来源门禁
  -> 首次发布0.1.0，或CAS更新已有版本
  -> 下次指定active Skill执行；已有版本支持回滚
```

任务未提供`optimization`时止于草稿，不会后台无限搜索。`task.learn: false`关闭沉淀；评测执行强制关闭。生成草稿不等于启用，GEPA内部候选池也不等于仓库里的待审核草稿。

## 主要实现

| 改动 | 实现与边界 |
|---|---|
| 新执行器接入沉淀 | `skills/acquisition.py`复用Curator；验证成功、至少一个实际动作、前后成功观察且无失败轨迹才提取。无动作、失败和证据不足会跳过；不是保证“经验一定有用”的智能判官 |
| 持久幂等与去重 | schema v3记录任务沉淀状态，事务预留脱敏过程指纹；重复恢复找回原草稿，同过程重复运行不新增。候选与证据同一DB事务提交 |
| 草稿隔离优化 | 优化器接受未生效种子；首次资格比较同actor空Skill基线，更新比较当前active版本。原种子无须为了搜索而提前上线 |
| 正确区分获取与提升 | 新Skill允许原文不变但通过非退化资格检查；已有版本更新仍要求dev改进。首次发布资格不自动成为“学习增益”声明 |
| 证据与版本门禁 | 校验seed/champion/content/evaluator/permission/suite哈希；冻结集预留后一次性消费；首次发布immutable文件+事务引用，更新使用CAS，旧版本回滚保留 |
| 污染隔离 | 评测不生成草稿；沉淀来源的相同objective/state不能出现在dev/holdout。语义近重复仍需人工审查，不能据此宣称消除所有泄漏 |
| CLI/API串联 | `RunRequest.optimization`显式绑定任务集、预算和发布范围；同步GEPA在线程中执行。状态持久化，resume不重复搜索，不静默更换优化配置 |
| 状态与失败隔离 | 任务成功与学习成功分别记录；沉淀/反思失败不把已完成外部任务改判失败；API任务读取返回learning与optimization阶段 |

## 直接运行

```bash
uv sync --frozen --extra evolve --extra test
uv run --frozen --extra evolve python -m pocket_agent demo-learning
```

演示无需模型凭据或手机，真实调用GEPA0.1.4，执行任务→模板草稿→隔离搜索/验收→首次发布。所有状态在临时目录，结束删除。脚本actor本来就能完成fixture，首次发布没有证明学习提升，不将其当成真实模型成绩。

配置有效模型后，可运行：

```bash
python -m pocket_agent run examples/settings_learning_task.json --run-id learn-settings-001
```

该文件将本地fixture任务、独立suite、评测/actor预算与local-only自动晋升范围放在一个请求里。真实设备需自行映射结构化状态、定义可信契约和可复位任务集；不能套用fixture结果。没有匹配suite时只沉淀草稿，不自动编造验收集。

仅优化草稿：`python -m pocket_agent optimize --skill ACQUIRED_SEED_ID --suite YOUR_SUITE.json`；通过后`approve EVALUATED_CANDIDATE_ID`。后续运行通过`skill_id`明确加载已启用版本。

API仍使用`/v3`路径，新增鉴权的`GET /v3/skills`和`POST /v3/skills/{id}/approve`。读取任务时，`status=completed`代表外部任务完成，发布进度应看`learning.optimization.status`。

## 验证范围

测试覆盖任务后提取、去重、原始草稿禁止直接启用、无动作/失败/评测不沉淀、候选提交后崩溃恢复、真实GEPA的首次发布、无改写的合格种子、后续复用、重复resume不重新搜索、seed篡改、active基线并发变化、回滚、来源任务混入验收集拒绝、learn开关不能绕过跨split重复检查、学习失败不改变任务成功、鉴权API全链路。

锁定源码环境 **63/63通过（88.468秒）**；白名单清洁快照在独立虚拟环境中 **63/63通过（88.367秒）**，`demo-learning`也完成首次发布。依赖版本未增加或升级。最终执行结果见 [VALIDATION_0.4.json](VALIDATION_0.4.json)。CI新增完整学习演示检查，远端结果需以实际Actions运行为准。

0.3阶段真实模型合成冒烟曾因现有凭据认证失败。0.4不重复消费无效凭据；本次使用脚本模型与本地fixture，没有真机任务成功率、真实模型学习增益或完整RSI结论。

## 兼容性与后续边界

数据库v3增量迁移，不删除历史数据。0.4运行身份/TaskSpec变更，拒绝用新代码静默恢复旧身份动作。旧聊天入口的沉淀钩子继续保留；推荐新CLI/API主线。

自动晋升要求显式授权，只覆盖有限UI工具、至少2个holdout任务且每项至少2次的local_fixture。真实设备仅可显式启用评测，不能自动晋升。取消等待不会强制终止已在线程中运行的同步搜索，预算仍有效。

沉淀与优化都有限制：模板草稿可能缺少参数抽象；规则和去重不保证经验有价值；训练/开发样本可能语义近重复；有限搜索不保证全局最优。历史记录会累积，尚无自动归档。仍需用真实模型、可复位设备，比较手写Skill、旧版迭代、轨迹检索与GEPA的等预算效果。
