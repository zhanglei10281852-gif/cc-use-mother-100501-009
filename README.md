# 生态用水履约核算后端

面向枯水期临时增量取水场景的履约核算服务。系统以**只追加事件账本 + 逐时重算引擎**为核心，
把取水许可、季节额度、生态下泄要求和实际计量统一到同一时间口径（北京时间整点小时、左闭右开），
在每次新增申请前评估对下游生态控制断面的影响，并自动识别即将违约、已经透支与恢复合规的区间。

零外部依赖，仅使用 Python 3.11+ 标准库；提供 HTTP API 与命令行两种使用方式。

## 核心原则

| 要求 | 实现方式 |
| --- | --- |
| 统一时间口径 | 许可、生态基流、计量全部按 `[整点, 整点)` 小时桶维护（`timeutil.py`），杜绝月底才发现透支 |
| 按取水点管理许可版本/季节额度/用途/优先保障对象 | 许可为不可变版本链，新版本自动截断旧版本；季节规则支持跨年枯水期（11-01～次年03-31） |
| 逐时计量、退水、经批准调剂落到时段 | 核算引擎逐小时归集配额（季节均摊＋临时额度＋调入－调出）与实际取水 |
| 新增申请前评估断面影响 | `assess_impact` 按汇流滞后与系数模拟申请增量，先留存评估编号才能提交申请 |
| 缺测暂估须有明确来源 | `source=estimated` 必须填 `estimate_source`（调度令/台账/邻站推算等） |
| 补报只能产生差异调整 | 更正事件指向被替代记录形成更正链；原值永不删除 |
| 更正、暂停、调剂不得重写已签月报 | 月报正文内容寻址冻结；之后的变化只生成 `difference` 差异调整 |
| 重复导入不能多扣额度 | 每条写命令绑定业务幂等键，同键同内容返回首次事件，同键异内容报 409 |
| 自动识别违约/透支/恢复 | 逐时累计余额分类：`compliant / near_breach / overdraft`，断面 `breach` |
| 执法复核与申诉留痕 | 两类只追加事件，申诉结论不覆盖原始复核 |
| 任一时段可复算、可溯源 | 全部数字现场重算；每行/每告警携带 `basis` 记录标识，`evidence` 回溯到事件序号原文 |

## 架构

```
events.py      领域事件定义（17 类，frozen dataclass）
ledger.py      只追加事件账本：哈希链（防篡改）+ 幂等键 + JSONL 持久化
projection.py  读模型投影：事件重放 → 许可版本/暂停/计量更正链/调剂/申请/报告/执法
engine.py      逐时核算引擎（纯函数）：配额归集、履约状态、断面基流、申请影响评估
reporting.py   月报冻结快照（内容寻址文档库）与差异调整
services.py    应用服务：业务校验、幂等命令、评估留存、告警与证据（唯一写入口）
api.py         标准库 HTTP API（ThreadingHTTPServer）
cli.py         命令行：serve/recompute/section/alerts/assess/report-*/evidence/import/demo-init
demo.py        枯水期完整演示场景（工业园/灌区/城市供水，984 条命令）
```

## 运行

```bash
# 测试
PYTHONPATH=src python3 -m unittest discover -s tests -v

# 初始化演示场景并启动服务
PYTHONPATH=src python3 -m water_compliance.cli --home ./data demo-init
PYTHONPATH=src python3 -m water_compliance.cli --home ./data serve --port 8080

# 或设置数据目录环境变量
export WATER_COMPLIANCE_HOME=./data
```

数据目录含 `events.jsonl`（哈希链事件）与 `documents.jsonl`（月报/评估快照）。
不指定 `--home` 时使用内存库，仅用于测试。

## HTTP API 摘要

写入均为 `POST` JSON；所有写入幂等。读取为 `GET`。

- 档案：`POST /users` `/sections` `/points`
- 许可：`POST /points/{id}/permit-versions` `/points/{id}/suspensions`，`POST /resumptions`
- 计量：`POST /meter-records` `/return-records` `/meter-amendments` `/return-amendments`
- 断面：`POST /section-inflows` `/section-inflow-amendments`，`GET /sections/{id}/recompute`
- 调剂：`POST /transfers` `/transfer-revocations`
- 临时申请（先评估）：`POST /impact-assessments` → `POST /requests` → `POST /requests/{id}/decision`
- 月报：`POST /points/{id}/reports`，`GET /points/{id}/reports/YYYY-MM[/difference]`
- 执法：`POST /reviews` `/appeals`
- 复算/告警/溯源：`GET /points/{id}/recompute` `/points/{id}/alerts`，`POST /evidence`
- 审计：`GET /events`（哈希链全文），`GET /health`

时间一律使用带偏移的整点串，如 `2026-11-01T00:00+08:00`；区间右端为开区间。

### 典型链路

```bash
# 1) 新增临时增量申请前先评估断面影响
curl -s -X POST localhost:8080/impact-assessments -H 'Content-Type: application/json' -d '{
  "point_id":"P-IND","valid_from":"2026-11-09T08:00+08:00",
  "valid_to":"2026-11-10T08:00+08:00","volume_m3":2400}'
# → feasible=false 时批准接口默认拒绝；确需批准须 override=true 并留存理由

# 2) 复算任一时段
curl -s "localhost:8080/points/P-IND/recompute?start=2026-11-01T00:00%2B08:00&end=2026-11-05T00:00%2B08:00"

# 3) 告警区间及其依据记录
curl -s "localhost:8080/points/P-IND/alerts?start=...&end=..."

# 4) 依据回溯到原始事件
curl -s -X POST localhost:8080/evidence -H 'Content-Type: application/json' \
  -d '{"record_ids":["meter-ind-059","permit:P-IND:V1"]}'
```

## 命令行

```bash
python -m water_compliance.cli --home ./data recompute P-IND --start 2026-11-01T00:00+08:00 --end 2026-11-05T00:00+08:00
python -m water_compliance.cli --home ./data section   S1     --start ... --end ...
python -m water_compliance.cli --home ./data alerts    P-IND  --start ... --end ...
python -m water_compliance.cli --home ./data assess    P-IND  --start ... --end ... --volume-m3 2400
python -m water_compliance.cli --home ./data report-sign  P-IND --year 2026 --month 11 --signed-by 王
python -m water_compliance.cli --home ./data report-diff  P-IND --year 2026 --month 11
python -m water_compliance.cli --home ./data evidence meter-ind-059 permit:P-IND:V1 T-001
python -m water_compliance.cli --home ./data import scenario.json
python -m water_compliance.cli --home ./data events
```

导入文件为 `{"commands":[{"op":"record_meter", ...}, ...]}`，`op` 对应 `ComplianceService` 方法名，
可安全重复导入。

## 核算口径

- 每小时配额 = 季节总额 ÷ 季节小时数（无季节规则覆盖时按年度额度均摊）
  ＋ 已批准临时增量均摊 ＋ 调入 － 调出；许可暂停期间配额为 0。
- 余额按月累计（月报口径），跨月重置；`balance < 0` 即**已经透支**；
  近期取水强度高于配额强度且结余不足以支撑 24 小时即**即将违约**；
  透支后首个合规连续段标记**恢复合规**。
- 控制断面每小时净值 = 天然来水 ＋ 退水 － Σ(各取水口取水 × 汇流系数，按滞后小时取源时刻)；
  低于逐时生态基流要求即 `breach`。申请影响评估在该口径上叠加申请增量并统计新增破坏小时。

## 不可变性与篡改检测

- `events.jsonl` 每行携带前一行哈希，启动加载时全链校验；任何历史改动都会触发 `EventChainBroken`。
- 月报/评估正文按内容 SHA-256 存于 `documents.jsonl`，事件仅保存指纹；正文不可变。
- 更正链：原始暂估 → 实测补报 → 再次复核更正，始终只有最新值生效，链上全部记录均可溯源。
