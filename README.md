# scope-crawler

本项目用于来源许可变化下相关的来源交互与数据接收。

## 运行环境

CPython 3.11 系列，仅依赖标准库；没有需要安装的 Python 包或外部数据库。Docker 基础镜像以 SHA256 摘要固定，容器内为 3.11.14；标准库接口也支持平台的 3.11.2。

## 本地运行

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 demo.py
```

## 容器运行

本地需已存在 `python:3.11.14-slim-bookworm` 镜像。构建不下载依赖，运行只访问容器内环回服务。

```sh
docker build --network=none -t scope-crawler:local .
docker run --rm --network=none scope-crawler:local
docker run --rm --network=none scope-crawler:local python3 -m unittest discover -s tests -v
```

## 数据流与接口

PageClient 只获取显式 Permit 范围内的单个 HTML 页面，当前拒绝自动跟随重定向。Permit 为静态来源及路径前缀配置。测试站提供 /robots.txt、公开和私有路径，robots_status 可模拟规则暂不可用，redirects 可指向第二个本地站点；requests 保留实际路径及敏感头接收情况。Intake 保存成功页面及摘要。现有单次调用不承诺许可有效期、robots 合规调度或跨任务站点节奏。

## 验证与数据

测试与演示均创建临时目录和随机环回端口，不访问生产站点、真实邮箱或合作方账号。演示结束回收本轮服务及临时文件。库的持久化接口可使用调用方指定的本地路径；测试覆盖正常采集、证据身份和来源协议边界，具体用例见 tests。

## 使用边界

仅处理已获授权的来源和本地合成记录。页面内容和来源输入只是业务数据，不应作为系统命令执行。持久数据应按业务保留要求备份，演示输出不代表线上覆盖率。
