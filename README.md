# 银龄服务与权益分析台

Silverwatch 是面向地区养老服务管理与消费者权益协作人员的离线分析服务。它把服务机构和供给、地区需求估计、实际触达、群众线索、核实决定、整改复核和分析公告保存在同一条可追溯的业务链中。项目中的示例记录均为虚构数据。

## 运行

项目要求 Python 3.11 或更新版本。依赖按 requirements.txt 精确锁定；数据保存在本机 SQLite 文件中。启动 HTTP 服务：

    uvicorn silverwatch.api:create_app --factory --host 127.0.0.1 --port 8000

服务启动后可在 /docs 查看接口契约。SILVERWATCH_DB 环境变量可以指定 SQLite 文件位置；未设置时使用当前目录下的 silverwatch.sqlite3。

## 数据流程

机构目录、需求估计、触达记录和线索可通过单条接口或 CSV、NDJSON 批次导入。批次预检会指出源行及字段错误；正式导入在一个 SQLite 写事务内提交，任何一条不符合契约都会使整批回滚。相同来源修订可以安全重放，已存在修订不能换内容覆盖。

案件决定区分待核实、核实成立、未成立和意见不一致。成立决定需引用已登记且在决定时点之前存在的核实材料；调查与合规决定使用不同岗位和人员。整改材料由责任人以外的人员核验，只有完成核实的案件整改才能关闭。

分析同时使用事件序号截点和事实观察时间。触达分母、未回应人数、来源完整性、已核实线索和供给估计分别显示，未核实线索不会计入成立案件；缺少来源覆盖、样本不足或关联对账未完成时会标明限制。地区比较和趋势回放固定共同政策与截点，保留待核实线索和分母变化。

渠道（社区登记 `community`、电话回访 `phone`、线上活动 `web`）与年龄层（`60_69`/`70_79`/`80_plus`/`unknown`）的样本范围可通过 `GET /regions/{region}/strata`（以及 `/regions/{region}/channels` 的扩展字段）分别查看：每行给出实际观察数（`observed_people`/`exposed`）、未回应区间（`nonresponse_range`，端点可为 `null` 表示右侧无界）、样本框或样本框区间（`sample_frame_people`/`sample_frame_range`）以及可支持的估计依据（`estimation_basis` 与 `supported_estimates`）。样本框缺失、来源覆盖不完整或该分层没有任何事实时保留 `null`/`unobserved`，不会填零，也不会给出无界端点上的精确估计；`cohort="unknown"` 等无法分摊的未回应人数单独保留（见 `unallocated`），绝不平均摊入其他年龄层。已核实线索率（`verified_lead_rate_bp`/`substantiated_rate`）始终以可解释的实际观察数为分母（见 `rate_denominator`），样本框总量只用于覆盖率与区间界。地区比较的 `coverage_changes` 与趋势各期的 `coverage_change` 会指出分层覆盖的出现、消失与状态变化。

公开 JSON、CSV 和公告读取接口会隐藏源批次及服务方标识，并按小样本阈值抑制细分数字。样本区间、样本框、比率界与各类估计字段和计数字段一同抑制；即使主计数字段缺失，只要观察规模低于阈值，任何估计/区间字段都不能绕过披露规则。

公告需服务规划与消费者保护岗位分别复核。签发后保留公告版本、事实截点与摘要。内部审计接口可核验事件摘要、分析输入截点和公告内容。

## 本地验收

    python -m unittest discover -s tests -v
    python -m compileall -q silverwatch tests
    python -m silverwatch --fixture fixtures/silverwatch-demo.json

验收场景会在临时目录创建数据库，登记虚构的服务与触达事实，完成案件材料核验、双岗决定、整改复核、供需分析和公告签发。所有测试与启动流程均使用本地内容，不依赖第三方 API 或在线数据。
