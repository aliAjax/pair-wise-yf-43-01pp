# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。

## 结果发布核对与复核

- `result` 的 `release` 动作需绑定 `instrument_id`、`calibration_id`、`method_id` 并给出 `value`、`unit`、`uncertainty`。发布前按结果引用的仪器、校准和方法核验三项：
  - 校准有效期：校准记录须属于该仪器、结果通过、已批准且未到期；
  - 方法覆盖范围：方法须为已验证（未撤回）版本、覆盖该仪器，且测量值在方法 `parameters.range` 内；
  - 本次测量不确定度：须为正数、不低于校准不确定度、不超过方法 `parameters.max_uncertainty`（如设置）。
- 任一项不合规：结果转入 `pending_review`（待复核），全部不合规原因写入 `review_reasons`，不放行。
- 复核员（`reviewer` 角色）执行 `review` 动作，重新选择有效校准与方法并填写 `disposition`（处置意见）；核验通过才发布，否则保持待复核并更新原因。
- 每次发布/复核尝试的仪器-校准-方法组合、不合规原因与处置意见按时间追加到 `release_history`，同时写入审计日志。
- 已发布（`released`）结果不能再执行 `release`/`review`，绑定不可更改；待复核结果可经 `block`/`reanalyze` 退回重新检测。

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

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
