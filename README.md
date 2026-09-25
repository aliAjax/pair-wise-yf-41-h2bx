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

- `station`：观测台站；`event`：地震事件及其多个修订版本；`merge`：震群归并单（`pending`→`confirmed`/`cancelled`）。

## 震群归并

同一震群被拆成多个事件后，用归并单把台站报告收回主事件：

1. 分析员通过`POST /api/merges`提交`primary_event_id`（主事件）、`source_event_ids`（待并入事件）、`region`（台站区域）和`expected_versions`（各编号当前版本）。
2. 预检只做规则判断、不改动数据：编号版本不符、事件已挂在另一张待复核归并单上、事件跨区域（`event.data.location`不等于`region`）或已归并/撤回，均返回`409`，响应体`conflicts`逐项给出冲突编号、原因与当前版本。
3. 复核员对归并单执行`confirm`：主事件在同一事务内接手全部台站报告并按振幅中位数重算震级（无振幅时回退为各事件震级中位数）；待并入事件变为`merged`、写入`merged_into`，旧编号仍可`GET`查询；处理人和时间写入各事件及归并单的审计时间线。
4. 复核员执行`cancel`即可撤销：待复核撤销不动事件；已确认撤销按确认时快照恢复报告归属，归并期间落到旧编号的补报（带`routed_from`标记）一并退回原编号。撤销后新的补报按恢复后的归属接收。
5. 归并期间台站可对事件执行`ingest_report`补报；旧编号若已归并，补报自动路由到主事件（主事件不可为已归并/已撤回）。

规则全部在`src/rules.py`（预检、计划、震级计算为纯函数），多事件原子更新和快照在`src/repository.py`，用例编排在`src/service.py`，HTTP层只负责把`MergeConflictError.conflicts`透出。原有单事件状态机不受影响。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（`merges`为归并单别名）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。事件支持`ingest_report`，归并单支持`confirm`/`cancel`（撤销需带`reason`）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
