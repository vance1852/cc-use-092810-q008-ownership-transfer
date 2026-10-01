# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `src/asset_title/`：参与方、电池资产权利链（所有者/保管人/责任人分离）、质押与召回限制、多方签署的所有权转让、原子交割、撤销与退回反向事实，以及财务、运维、审计共用的时点与权利链查询；
- `fixtures/`：离线验收使用的评估协议与结构化观测；
- `tests/`：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

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
PYTHONPATH=src python3 -m asset_title.acceptance --workspace .
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、组件质量和所有权流转流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m asset_title.api --database asset-title.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。
