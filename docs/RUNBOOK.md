# 运行手册 subbill

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18208/api/health
```

宿主机端口 18208 → 容器 8000。数据库在命名卷 `subbill-data` 的 `subbill.sqlite`。

## 主流程

```bash
# 1 采信自带脏样例（订阅 + 变更），返回 run_id 与四本账
curl -s -X POST http://127.0.0.1:18208/api/subscriptions/import \
  -H 'Content-Type: application/json' -d '{}'

# 2 周期诊断
curl -s http://127.0.0.1:18208/api/calendar | head -c 400

# 3 隔离区与原因码
curl -s http://127.0.0.1:18208/api/quarantine | head -c 400
curl -s -o /tmp/quarantine.csv http://127.0.0.1:18208/api/quarantine.csv

# 4 折算与变更
curl -s http://127.0.0.1:18208/api/proration | head -c 300
curl -s -X POST http://127.0.0.1:18208/api/changes | head -c 300

# 5 账单与对账
curl -s http://127.0.0.1:18208/api/invoices | head -c 400
curl -s http://127.0.0.1:18208/api/reconcile
curl -s http://127.0.0.1:18208/api/overages | head -c 400

# 6 导出
curl -s -o /tmp/invoices.csv http://127.0.0.1:18208/api/export/invoices.csv
curl -s -o /tmp/segments.csv http://127.0.0.1:18208/api/export/segments.csv
curl -s -o /tmp/overages.csv http://127.0.0.1:18208/api/export/overages.csv
curl -s -o /tmp/manifest.json http://127.0.0.1:18208/api/export/manifest.json
```

## 用量与超额

```bash
# 采信时一并带用量（整月汇总）
curl -s -X POST http://127.0.0.1:18208/api/subscriptions/import \
  -H 'Content-Type: application/json' \
  -d '{"usage":"usage_dirty.csv"}'

# 给已有批次补录用用量（按 sha256 去重，重试不会重复累计）
curl -s -X POST http://127.0.0.1:18208/api/changes/usage/import \
  -H 'Content-Type: application/json' -d '{"usage":"usage_dirty.csv"}'

# 超额明细：每条带账单号与段序，可追到套餐分段
curl -s http://127.0.0.1:18208/api/overages | head -c 600
```

额度口径：`段包含额度 = 套餐包含额度 × 段天数 ÷ 期间天数`，整月用量同样按天数比例分摊到各段。
套餐目录取 `sample_data/plans.csv`（`套餐, 计量单位, 包含额度, 超额单价(元)`），没有则用内置目录。

## 换成自己的数据

样例目录挂载在容器 `/app/sample_data`，采信时传文件名即可（只接受纯文件名）：

```bash
curl -s -X POST http://127.0.0.1:18208/api/subscriptions/import \
  -H 'Content-Type: application/json' \
  -d '{"subscriptions":"subscriptions_gbk.csv","changes":"changes_dirty.csv"}'
```

两份文件的列名会被映射到规范字段：订阅表需要 `订阅号`、`月价(元)`、`期间起`、`期间止`、`状态`；变更表需要 `变更号`、`订阅号`、`生效时间`、`类型`。缺必需列会整体隔离并给出 `R_HEADER_MISSING`。

## 重算与幂等

- 同一份输入再采信一次：命中 `batch_key`，直接返回原批次，行数与合计不变。
- 只重算金额：`POST /api/proration`；重算分段、抵扣与超额：`POST /api/changes`。两者都会重新出账并刷新四本账。
- 同一份用量文件重复上传：按 `(run_id, sha256)` 命中，返回 `reused: true`，不重复累计。
- 重算失败（任何异常）：派生表与清单整体恢复旧版，页面与导出继续显示上一版完整账单；批次状态不标 done。
- 想强制新建批次：改任一输入文件的内容（`batch_key` 由输入 sha256 决定）。

## 重建数据库

```bash
docker compose down -v          # 删掉命名卷，下次启动会重建空库
docker compose up -d --build
```

## 排错

| 现象 | 检查 |
|---|---|
| 首页 200 但表格为空 | 还没有批次，点「采信」；或 `/api/runs` 看是否有 `done` 批次 |
| 采信返回 400 `E_FILE_MISSING` | 文件名不在 `sample_data/`；只接受纯文件名 |
| 采信返回 400 `E_BAD_FILE_NAME` | 传了路径分隔符或空串 |
| 账单为 0 | 期间内全是暂停/试用，或月价为 0；看账单标记 `R_INVOICE_ZERO` |
| 对账不平 | 看 `/api/reconcile` 哪一本账 `status=mismatch`，再看对应明细 |
| 超额为 0 但用量很大 | 看是否没采信用量文件；或套餐不在目录里（`R_PLAN_UNKNOWN`，额度按 0 但单价也按 0，需补目录） |
| 用量行进隔离区 | `R_USAGE_UNKNOWN_SUB` 订阅号不在订阅表；`R_USAGE_MISSING` 用量为空或哨兵 |
| 重复上传用量后金额没变 | 这是预期：`reused: true`，同一文件不重复累计；要改数就换文件内容 |
| 导出 404 `E_NO_RUN` | 还没批次；先采信 |
| 容器内 pytest 失败 | `docker compose exec -T app python -m pytest -q`，先看第一个失败 |

## 测试

```bash
docker compose exec -T app python -m pytest -q
docker compose exec -T app python -m pytest tests/test_invariants.py -q
```

## 日志

```bash
docker compose logs -f app
docker compose exec -T app ls -l /data
```
