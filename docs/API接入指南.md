# dnamemory API 接入指南

> 面向外部调用方：如何启动服务、鉴权、调用接口、以及对外暴露时的安全清单。
> 端点全表见 [api.md](api.md)；存储后端见 [Postgres后端使用手册](Postgres后端使用手册.md)。

## 1. 启动服务

复制配置模板并改三处（**host / token / 库路径**），服务即读配置启动：

```powershell
cp dnamemory.env.example dnamemory.env
# 编辑 dnamemory.env：
#   DNAMEMORY_HOST=0.0.0.0          # 对外提供（默认 127.0.0.1 仅本机）
#   DNAMEMORY_TOKEN=<随机长串>       # 必填
#   DNAMEMORY_DB=user_memory.db
dnamemory-server
```

配置文件查找顺序：`--config` > `DNAMEMORY_CONFIG` > `./dnamemory.env` > `~/.dnamemory.env`。
优先级：**命令行 > 真实环境变量 > 配置文件 > 内置默认**（容器/systemd 注入的值不会被文件覆盖）。

生成一个强 token：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 2. 鉴权

所有端点都要求请求头：

```
Authorization: Bearer <token>
```

缺失或错误的 token 返回 **401** 与错误体 `{"code": "E401", "message": "..."}`。

> 当前是单 token 模型：所有调用方共享一个 token。若要给多个调用方分别发 key（可吊销、
> 带名称、调用记审计），需要扩展为多 key 管理——目前尚未实现。

## 3. 调用示例

### 3.1 curl

```bash
# 存活检查
curl -H "Authorization: Bearer $TOKEN" http://HOST:8000/health
# {"ok":true,"version":"0.8.0"}

# 召回（多路检索）
curl -X POST http://HOST:8000/recall \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"预算调整","k":5,"mode":"triple"}'

# 上下文（结构化问答：16 区块）
curl -X POST http://HOST:8000/context \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"我现在住哪里","include_history":true}'

# 结构化写入
curl -X POST http://HOST:8000/facts \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"entity":"用户","key":"city","value":"上海","confidence":0.9}'
```

### 3.2 Python（requests）

```python
import requests

BASE, TOKEN = "http://127.0.0.1:8000", "your-token"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# 1) 写入
r = requests.post(f"{BASE}/entities", headers=H,
                  json={"name": "用户", "kind": "person"}, timeout=30)
r.raise_for_status()

# 2) 上下文问答（返回 16 个区块）
ctx = requests.post(f"{BASE}/context", headers=H,
                    json={"text": "我住在哪里", "include_history": True},
                    timeout=60).json()
for fact in ctx.get("current_state", []):
    print(fact["key"], "=", fact["value"])

# 3) 召回
hits = requests.post(f"{BASE}/recall", headers=H,
                     json={"text": "预算", "k": 5}, timeout=60).json()
for h in hits["items"]:
    print(h["score"], h["name"])
```

### 3.3 JavaScript（fetch）

```javascript
const BASE = "http://127.0.0.1:8000";
const H = { "Authorization": "Bearer your-token",
            "Content-Type": "application/json" };

const ctx = await fetch(`${BASE}/context`, {
  method: "POST", headers: H,
  body: JSON.stringify({ text: "我住在哪里", include_history: true }),
}).then(r => r.json());

console.log(ctx.current_state);
```

> 浏览器直连需把前端所在源加进 `DNAMEMORY_CORS_ORIGINS`（见 §5）。

## 4. 常用端点速查

| 用途 | 端点 |
|---|---|
| 存活 / 统计 | `GET /health`、`GET /stats` |
| 召回 | `POST /recall`（`mode`: time/graph/semantic/dual/triple/quad） |
| 上下文问答 | `POST /context`（16 区块，含 current_state/impacts/patterns…） |
| 时间线 | `POST /timeline` |
| 事实查询 | `GET /facts?entity=&key=`（不带 key 列出该实体全部当前事实） |
| 邻接 | `GET /neighbors?node=&rel=` |
| 图谱快照 | `GET /graph?center=&hops=&limit=` |
| 追溯 | `GET /memory/{id}/explain?kind=`、`GET /memory/{id}/history?dimension=` |
| 结构化写入 | `POST /entities /events /facts /beliefs /intents /impacts /evidence /edges` |
| 文本抽取写入 | `POST /write`、`POST /batch`（需配置 extractor） |
| 治理 | `POST /forget /resolve-conflicts /confirm /resolve-entities /reflect /compress-stable /extract-patterns /derive-impact-links /step-day /restore` |

完整参数与响应见 [api.md](api.md)。

## 5. 对外暴露安全清单

| 项 | 做法 |
|---|---|
| 监听地址 | 默认 `127.0.0.1` 仅本机；对外必须显式设 `DNAMEMORY_HOST=0.0.0.0` |
| Token | 用 `secrets.token_urlsafe(32)` 生成；**不要**留 `change-me`；泄露后立即更换 |
| CORS | 在 `DNAMEMORY_CORS_ORIGINS` 显式列出来源（逗号分隔）。本项目**不通配**、不用 `*`；不在白名单的源拿不到 ACAO 头 |
| HTTPS | 服务本身只提供 HTTP；对外请放在反向代理（Nginx/Caddy）后终止 TLS，不要把明文端口直接暴露公网 |
| 网络层 | 能在内网/白名单/VPN 内使用就不要暴露公网；必要时在代理层加 IP 限制与限流 |
| 库与数据 | 库文件/DSN 由配置文件指定，注意文件权限；PG 部署见 [Postgres后端使用手册](Postgres后端使用手册.md) |
| 审计 | 所有写入与治理操作都会进 `audit_log` 表，可通过 `GET /stats` 观察规模 |

## 6. OpenAPI 与调试

服务运行后 FastAPI 自动提供：

- 交互式文档：`http://HOST:8000/docs`
- OpenAPI schema：`http://HOST:8000/openapi.json`（可直接导入 Postman / 生成客户端）

> `/docs` 与 `/openapi.json` **不校验 token**（FastAPI 内建路由），对外暴露时请在反向代理层限制其访问，或仅在调试期开放。

## 7. MCP 接入

需要把记忆能力挂给支持 MCP 的客户端（Claude Desktop / Claude Code 等）时：

```jsonc
// stdio 模式（本地进程内，无需 token）
{ "mcpServers": { "dnamemory": {
    "command": "dnamemory-mcp", "args": ["--path", "user_memory.db"] } } }

// streamable_http 模式（远程，需 token）
// dnamemory-mcp --transport streamable_http --token <token> --port 8765
```

MCP 提供 10 个工具（recall / write / forget / resolve_conflicts / context /
timeline / explain / stats / write_structured / govern），与 HTTP 端点同源同语义。

## 8. 常见问题

| 现象 | 原因 |
|---|---|
| 401 `E401` | token 缺失或错误；确认配置文件里的 `DNAMEMORY_TOKEN` 与请求头一致 |
| 422 `E422` | 请求体字段非法：枚举值不在允许集内（见 api.md 的枚举校验）或缺少必填字段 |
| 400 `E010` | 文本抽取写入需要 extractor：配置 `DEEPSEEK_API_KEY`（默认自动启用 LLM+兜底链）或启动加 `--fallback-extractor`（确定性兜底） |
| 浏览器报 CORS | 前端所在源未加入 `DNAMEMORY_CORS_ORIGINS` |
| 改了配置没生效 | 已存在的环境变量优先于配置文件；先 `unset` 同名环境变量再重启 |
