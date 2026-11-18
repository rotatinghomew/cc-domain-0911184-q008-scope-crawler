# scope-crawler

本项目用于来源许可变化下相关的来源交互与数据接收。

## 运行环境

CPython 3.11 系列，业务运行仅依赖标准库，无外部数据库。构建工具精确锁定在 requirements-build.txt；先执行 python3 -m pip install -r requirements-build.txt。Docker 基础镜像以 SHA256 摘要固定，容器内为 3.11.14；标准库接口也支持平台的 3.11.2。

## 本地运行

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 -m build --wheel --no-isolation
python3 demo.py
```

## 容器运行

首次构建拉取摘要固定的官方 Python 镜像并安装锁定的构建工具；运行不访问外网，只使用容器内环回服务。

```sh
docker build -t scope-crawler:local .
docker run --rm --network=none scope-crawler:local
docker run --rm --network=none scope-crawler:local python3 -m unittest discover -s tests -v
```

## 数据流与接口

采集由 `Collector` 驱动，所有状态落在调用方指定的 SQLite 文件中，进程重启后限制与任务继续生效：

- **双闸门，缺一不可**：`PermitRegistry` 按来源 + 路径前缀 + 有效时间窗口判定合作许可（支持撤销/恢复）；`RobotsGuard` 独立判定站点 robots 约定（解析 `Crawl-delay`）。两者必须同时满足，任何一个不通过都给出独立原因码，互不兜底。robots.txt 网络不可达或返回 408/429/5xx 时按“不可用即禁止”处理（`robots_unavailable`），只有 404/410 才表示站点未声明限制。
- **逐跳重定向**：`GuardedFetcher` 不自动跟随跳转，每一跳都重新过双闸门与站点节奏；任一跳越出许可/robots 范围即停止。`Cookie`、`Authorization`、`Proxy-Authorization` 只在同一来源内携带，跨来源跳转自动剥离，跳数链与剥离动作全程留痕。
- **站点整体节奏**：`SitePacing` 以来源为单位持久化上次请求时间与约定间隔，多任务共享同一份额度；重试走同一入口，不重新获得额度。被闸门拦截的请求不发出、不占额度。
- **撤销的三种处置**：排队中撤销 → 不再发出请求（任务 `blocked`）；在途撤销 → 响应正文丢弃，仅保留含丢弃摘要与跳数链的处置记录（`revoked_in_flight`），不产生可用正文；已接收页面 → 按抓取时固化在页面记录中的许可保留条款处理，到期只脱敏正文（`retention_redacted`），digest、状态码、抓取时刻与许可快照等抓取事实不被追溯篡改。
- **恢复与解释**：软阻断任务保留在库中，许可或 robots 恢复后由后续 `tick()` 自动重新推进仍有效任务（带 `not_after` 的任务过期后不再推进），无需重启进程、也不靠重启清空限制。`explain(url)` 返回每个页面的任务状态、原因码、中文解释与完整事件链；重定向任务会自动关联最终 URL 的入库页面。
- 原因码集中定义在 `scope/reasons.py`，覆盖许可、robots、重定向、节奏、重试与保留条款各类停止依据。测试站 `serve()`/`serve_pair()` 提供单站与双来源环回服务，可模拟 robots 状态、跨源重定向与 5xx 失败路径。

## 模块

| 模块 | 职责 |
| --- | --- |
| `scope/permission.py` | `Grant` 许可模型、`PermitRegistry` 撤销/恢复/时间窗口判定 |
| `scope/robots.py` | robots 拉取缓存、fail-closed 闸门、Crawl-delay 解析 |
| `scope/pacing.py` | 站点级访问间隔与额度占用，状态持久化 |
| `scope/fetcher.py` | 逐跳重定向抓取、双闸门、跨源凭据剥离 |
| `scope/store.py` | 任务/页面/事件/站点状态的 SQLite 持久化 |
| `scope/collector.py` | 调度、撤销处置、保留脱敏、运维解释 |
| `scope/reasons.py` | 原因码与中文解释 |
| `scope/clock.py` | UTC 时钟与时间序列化，支持注入测试时钟 |

## 验证与数据

测试与演示均创建临时目录和随机环回端口，不访问生产站点、真实邮箱或合作方账号。演示结束回收本轮服务及临时文件。库的持久化接口可使用调用方指定的本地路径；测试覆盖正常采集、证据身份和来源协议边界，具体用例见 tests。

## 使用边界

仅处理已获授权的来源和本地合成记录。页面内容和来源输入只是业务数据，不应作为系统命令执行。持久数据应按业务保留要求备份，演示输出不代表线上覆盖率。
