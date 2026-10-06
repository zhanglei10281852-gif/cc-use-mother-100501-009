# 生态用水履约核算后端

面向枯水期临时增量取水场景的履约核算服务。系统按取水点维护**许可版本、季节额度、
用途限制与优先保障对象**，把逐时计量、退水、入库径流和经批准的跨主体调剂落到统一的
**逐小时桶**，在每次新增申请前逐时评估对下游生态控制断面的影响，并自动识别
**即将违约、已经透支与恢复合规**的区间。全部结论可由 API 或命令行**从台账重算**，
并能逐条回溯到所依据的原始记录。

## 设计原则：只追加台账 + 随时重算

```
事件台账 (SQLite, append-only)
  permit_* / quota_* / section_*      档案类事实（版本化、不可改）
  measurement / return / inflow       逐时原始记录（可暂估）
  *_amended                           补报/更正：只登记差异 delta
  transfer_approved                   经批准的跨主体调剂
  application_submitted / _decided    申请与裁定（含逐时评估依据）
  report_signed                       已签署月报（快照 + 指纹冻结）
  enforcement_recorded                执法复核与申诉结论
        │
        ▼  重放（Projection，可随时重建）
逐时余额 / 断面平衡 / 违约区间 / 月报快照 / 证据链
```

关键规则：

- **双时间口径**：每条运行记录同时携带物理时段 `event_hour`（`YYYY-MM-DDTHH`）与
  会计月份 `observed_month`（`YYYY-MM`），分别回答“水是哪一小时取的”和
  “账记在哪个月”。
- **暂估必须有明确来源**（`estimate=true` 且 `source` 非空）；补报只能发
  `*_amended` 事件登记 `delta_m3` 差异，原始记录永不覆盖，暂估随即转为实测口径。
- **月报一旦签署即冻结**：报告固化签署时的台账序号、完整快照与 SHA-256 指纹；
  此后计量更正、许可暂停、跨主体调剂都不会改写旧报告，只在验签结果中列为
  “签署后差异调整”，归入以后会计月份。
- **原始记录幂等**：`raw_id` 等业务键有唯一约束，重复导入返回既有序号
  （`duplicate=true`），不多扣一立方米；同号不同内容直接拒绝。
- **生态断面影响先评估后裁定**：申请逐时检查用途授权、暂停状态、小时限值、
  季节额度余量，并按取水点—断面的传播滞后（小时）与耗水系数折算净耗水，
  检查断面平衡 `入库 − 净耗水 ≥ 生态流量要求`。入库缺测明确标注
  `inflow_data_missing`，绝不默认合规。
- **每个结论都带证据**：逐时与区间结论附事件 `seq` 列表，`GET /events/{seq}`
  可查看原始记录内容、指纹、是否暂估与来源。

## 运行环境

- Python 3.11+，仅标准库；SQLite 文件即全部持久化状态，无需其他服务。

## 运行测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py
```

## 快速开始

```bash
# 1. 批量导入档案与逐时数据（JSONL；文件内故意重复了一条 M-1）
PYTHONPATH=src python3 -m water_compliance.cli --db water.db import \
  examples/枯水期增量申请场景.jsonl

# 2. 复算任一时点的可用量
PYTHONPATH=src python3 -m water_compliance.cli --db water.db availability \
  --permit P-IND --use industrial --hour 2026-10-05T10

# 3. 新增申请前评估下游断面影响（只评估，不入账）
PYTHONPATH=src python3 -m water_compliance.cli --db water.db evaluate \
  --permit P-IND --use industrial \
  --start 2026-10-05T10 --end 2026-10-05T11 --rate 300
# → decision=rejected：同时触发小时限值超限与 SEC-1 生态流量不足
#   （取水 10 点发生，滞后 2 小时作用于断面 12 点）

# 4. 识别违约/预警/恢复区间及其依据记录
PYTHONPATH=src python3 -m water_compliance.cli --db water.db alerts \
  --start 2026-10-01T00 --end 2026-11-01T00

# 5. 签署、复算并验签月报
PYTHONPATH=src python3 -m water_compliance.cli --db water.db sign \
  --month 2026-10 --signed-by 监督员甲 --doc-ref 月报-202610
PYTHONPATH=src python3 -m water_compliance.cli --db water.db verify --month 2026-10

# 6. 启动 HTTP API
PYTHONPATH=src python3 -m water_compliance.cli --db water.db serve --port 8080
# 或安装后： water-compliance --db water.db serve
```

冒烟脚本（内存台账）：`python3 run_cli.py`。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/permits`、`/permits/revise`、`/permits/suspend`、`/permits/resume` | 许可注册、版本修订、暂停与恢复 |
| POST | `/quotas`、`/quotas/amend` | 季节额度开立与额度调整 |
| POST | `/sections`、`/sections/schedule`、`/sections/link` | 断面、生态流量日程、取水点水文关系 |
| POST | `/measurements`、`/returns`、`/inflows` | 逐时取水/退水/入库（支持暂估） |
| POST | `/measurements/amend`、`/returns/amend`、`/inflows/amend` | 补报与更正（差异调整） |
| POST | `/transfers` | 经批准的跨主体调剂 |
| POST | `/applications/evaluate` | 申请影响评估（不落账） |
| POST | `/applications`、`/applications/decide` | 登记申请并自动裁定 / 人工裁定 |
| POST | `/reports/sign` | 签署月报（冻结快照与指纹） |
| GET | `/reports/{month}`、`/reports/{month}/verify` | 查看月报 / 复算验签 |
| POST | `/enforcement` | 执法复核与申诉结论 |
| GET | `/availability`、`/compliance`、`/alerts` | 可用量、逐时履约、告警区间与证据 |
| GET | `/events`、`/events/{seq}`、`/health` | 台账回溯与健康检查 |

所有写接口返回 `{seq, duplicate, event_type}`；规则拒绝返回 HTTP 422 与中文原因。

## 事件与状态语义

- **许可版本**：`permit_registered` 后可多次 `permit_revised`，按 `valid_from`
  取当时有效版本（用途清单、优先序、小时限值）；`permit_suspended` 区间内取水
  直接判 `withdrawal_while_suspended`，恢复用 `permit_resumed` 闭区间。
- **季节额度**：`quota_opened` 划定季节窗口与总量；`quota_amended` 按生效小时
  增减；`transfer_approved` 按用途在调出/调入双方额度上同步移动。
- **逐时状态**：`compliant` / `near_breach`（余量进入阈值带或断面余量不足 10%）
  / `overdraft`（许可口径）与 `breach`（断面口径）；坏状态之后的连续合规区间带
  `recovered=true` 与 `prior_state`。
- **断面平衡**：有实测退水时净耗水 = 取水 − 退水；缺退水计量时按链接耗水系数
  折算并标注估算；缺入库则状态为 `data_missing`。
- **执法留痕**：`enforcement_recorded` 支持 `open/confirmed/no_finding/
  appeal_upheld/appeal_rejected`，申诉结论可引用原案号，原认定不被删除。

## 模块结构

```
src/water_compliance/
  timebuckets.py  统一小时桶/会计月份
  events.py       事件 schema、校验、指纹与幂等键
  storage.py      SQLite 只追加台账（幂等/冲突约束）
  projection.py   只读重放：版本口径、额度账、断面平衡、区间识别、证据链
  service.py      用例层：申请评估、暂估/更正、月报冻结、执法、复算
  api.py          标准库 HTTP JSON API
  cli.py          命令行（复算/导入/评估/签署/验签/serve）
tests/            契约测试、规则端到端测试、HTTP 集成测试
examples/         枯水期增量申请场景 JSONL
```
