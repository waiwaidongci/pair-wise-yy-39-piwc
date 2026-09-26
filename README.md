# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/plan`，查看当前预案版本、确认进度和待确认角色
- `POST /api/items/{id}/plan`，应急管理员保存新预案版本（处置步骤+撤离范围）
- `POST /api/items/{id}/plan/confirm`，巡检员或坝工确认当前预案版本
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 应急预案闸门

- 应急管理员保存预案时自动生成递增版本号；再次保存后，旧版本上的确认全部失效，页面会显示哪些角色还未确认当前版本。
- 达到阈值的缺陷（`escalation_required`）只有在巡检员、坝工都确认当前预案版本后，才能从`defect_confirmed`转入`repair`；普通缺陷仍走原流程。
- 每次预案改版（`plan_save`）、确认（`plan_confirm`）和放行（`release`）都会写入哈希链审计；缺陷详情返回`plan`（版本、确认进度、待确认角色）和`repair_blockers`（阻挡原因）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
