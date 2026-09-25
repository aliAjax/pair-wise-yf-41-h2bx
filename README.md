# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。
- `merge`：震群归并单，状态为`pending`（待复核）、`confirmed`（已归并）、`cancelled`（已撤销）。

## 震群归并

1. 分析员`POST /api/merge`提交归并单：`primary_event_id`（主事件）、`source_event_ids`（待并入事件）、`region`（台站区域）、`expected_versions`（各事件编号及其版本）。
2. 提交与复核确认时都会校验：任一编号版本不符、事件已被其他待复核归并单引用、或事件区域与归并区域不符，即返回`409`且不改任何数据；响应`details.conflicts`列出冲突编号、当前版本和原因。
3. 复核员对归并单执行`confirm`后：主事件接手全部台站报告并按振幅中位数重算震级；旧编号状态变为`merged`仍可查询，后续台站补报（`report`动作）自动落到主事件；处理人与时间写入各事件时间线。
4. 对归并单执行`cancel`可撤销：已确认的被回滚，报告（含归并期间的后到补报）回到原编号，原编号恢复原状态并重新接收补报；`pending`状态直接作废，不影响事件。
5. `GET /api/merge`查看归并列表，可按`?status=`过滤；原单事件流程不受影响。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
