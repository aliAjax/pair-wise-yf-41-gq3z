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

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 关联质量关口

事件执行`associate`时按统一口径评估报文，结果一次性固化为`association`快照（采用报文、存疑报文、质量分、口径指标、固化版本与时间），此后复核、发布、修订都改不动；重复`associate`或在动作载荷中夹带快照均被拒绝。

- 同一台站重复上报：只保留时间偏移绝对值更小的一份，持平保留先到者，另一份计入去重。
- 存疑：保留报文的时间偏移绝对值超过120秒或距离超过3公里时标记`suspect`，不计入有效报文。
- 质量分：`100 × 有效报文数 / 原始报文数`。
- 复核闸门：有效报文少于3条，或有效报文平均距离超过2公里时，`review`被拒绝；仅管理员可在`override_reason`中写明依据后放行，放行信息随版本留痕。
- 事件列表按`?stage=candidate|review|published`区分候选、待复核（associated/reviewed）、已发布（published/revised/withdrawn）。
- `GET /api/entities/<id>/detail`返回摘要、固化质量信息与历史版本；`GET /api/entities/<id>/versions`仅返回版本时间线；`GET /api/audit?entity_id=<id>`按实体过滤审计。

演示页面在三栏分区之外，还展示报文级判定（有效/存疑/去重）、管理员放行依据与每个历史版本的摘要。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
