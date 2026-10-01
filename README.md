# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `src/battery_title/`：电池所有权、现场保管权、运维责任的三权分属登记，质押/召回等关联限制，
  以及“卖方冻结—买方接受/融资方解押/质量确认—原子交割”的权属转让流程；
- `fixtures/`：离线验收使用的评估协议与结构化观测；
- `tests/`：领域规则、错误边界、事务、权限、HTTP API、并发竞争和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m battery_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m battery_assurance.acceptance --workspace .
PYTHONPATH=src python3 -m component_quality.acceptance
PYTHONPATH=src python3 -m battery_title.acceptance --workspace .
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、组件质量和权属交割（含质押解押、
竞争裁决、依据版本作废、失败保留同意与交割后退回）流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m battery_title.api --database battery-title.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 权属流转（battery_title）

法律所有权、现场保管权、运维责任可分属不同主体，三权登记在只追加的权属事实流
`title_events` 上（事件溯源），当前状态是事实的折叠结果。

- **发起冻结**：卖方发起转让时，系统在单个 `BEGIN IMMEDIATE` 事务内冻结资产清单及其依据版本、
  三权权利声明、保管位置，并自动汇集资产上全部生效中的质押/召回/留置/锁定限制形成完整快照
  （`proposal_sha256` 固化）；卖方必须是当前所有人，且申报版本必须等于台账当前版本。
- **汇集同意**：买方接受自动作为必达条件；融资方解押（release）、质量或召回确认
  （quality_clearance）按冻结条件逐项汇集，每条签署记录给出时的依据版本快照 `basis_key`。
- **原子生效**：买方接受、必要的解押与质量确认全部有效、且本提案是竞争领跑者时，
  交割在单事务内为每个资产写入 `title_changed` 事实并推进版本；条件不齐返回 409
  `settlement_blocked` 并在 `details` 中列出每个被阻断原因，已完成的同意全部保留，权属不变。
- **依据版本**：资产台账/证据升版（`revise_asset`，以及限制变动）使引用旧版本的未完成签署失效，
  交割终局失败（`failed`），需重新发起；签署阶段也会拒绝向已失效提案追加同意。
- **幂等**：发起、签署均要求幂等键，同键同体重放返回首次结果、同键异报冲突；交割幂等由状态机
  保证，重试只回读既有事实，绝不产生第二次转让。
- **竞争交易**：同一资产上所有 `proposed` 提案按 `(created_at, transfer_id)` 构成确定总序，
  最早提案为领跑赢家；非领跑者交割被阻断，赢家原子生效的同一事务内其余竞争提案置 `failed`
  （`lost_contest:<赢家>`），多连接并发下亦无双花。
- **失败/撤销/退回**：交割失败保留提案与全部同意，只置 `failed` 并记录阻断原因，不提前改变权属；
  交割前撤销置 `cancelled`，同样不写权属事实；交割后退回为每个资产生成与 `title_changed`
  对称的 `title_reversed` 反向事实（恢复三权、保管位置，并随退回恢复交割时解除的限制），
  从不删除或改写历史。
- **查询**：`GET /assets/{id}/title?as_of=...` 返回某时点的所有者/保管人/运维责任人/保管位置/
  生效限制；`GET /assets/{id}/chain` 返回整条权利链；`GET /transfers/{id}` 返回冻结提案、
  各条同意及其当前有效性、被阻断原因；财务、运维、审计角色按权限读取，审计可独立校验
  `/audit/chain` 哈希链。
