# heatstar-backend

热创星系统后端 API，负责提案、审核、组织权限、积分评优、文件上传和导出服务。

## 项目简介

本仓库是热创星全员持续改善平台的后端服务。它为 `heatstar-admin` 管理后台和 `heatstar-app` 员工端提供统一 HTTP API，使用 SQLite 持久化业务状态，支持精益提案、六源改善、工具开发、OPL 单点课程、两级评审、账号会话、人员导入、附件上传，以及 Excel、Word、PPT 导出。

## 技术栈

- Python 3 标准库 HTTP 服务
- SQLite WAL 持久化
- PBKDF2-SHA256 口令散列和不透明会话令牌
- OOXML 文档生成：Excel、Word、PowerPoint
- systemd 服务配置与 Nginx 反向代理配置

## 关联仓库

| 项目 | 说明 | GitHub |
| --- | --- | --- |
| heatstar-backend | 后端服务 | [heatstar-backend](https://github.com/jiangyi3265/heatstar-backend) |
| heatstar-admin | 管理后台 | [heatstar-admin](https://github.com/jiangyi3265/heatstar-admin) |
| heatstar-app | 员工端 | [heatstar-app](https://github.com/jiangyi3265/heatstar-app) |

## 快速启动

```bash
python app.py --host 127.0.0.1 --port 8765
```

首次运行前建议设置以下环境变量。`HEATSTAR_DATA_DIR` 不设置时默认写入当前目录下的 `data/`，该目录不会提交到 Git。

```bash
HEATSTAR_SUPER_ADMIN_PASSWORD=<your-admin-bootstrap-password>
HEATSTAR_DEFAULT_PASSWORD=<your-user-initial-password>
HEATSTAR_DATA_DIR=./data
```

如需导入初始业务数据，可复制 `seed_state.example.json` 为 `seed_state.json` 后自行填充；真实数据文件默认被 `.gitignore` 排除。

## 项目结构

- `app.py`：HTTP API 入口、路由、CORS、上传与状态读写。
- `auth.py`：账号、会话、密码修改与重置。
- `proposals.py`：提案编号、审核规则和业务校验。
- `organization.py`：部门、工段、班组和人员归属规则。
- `company_workflow.py`：部门评审、公司评审和精益专员流程。
- `export_*.py`：台账、Word 归档表和 PPT 六源案例导出。
- `templates/`：导出模板文件。
- `test_*.py`、`test_*.mjs`：后端和前后端同步规则测试。

## 简历描述示例

参与热创星持续改善平台后端建设，完成提案提交、组织权限、两级评审、积分评优和多格式导出等核心能力，并通过统一 API 支撑管理后台与员工端协同使用。
