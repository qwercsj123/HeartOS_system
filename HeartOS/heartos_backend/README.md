# HeartOS Backend (可直接部署)

这个后端给 `noteai_v4_0423.html` 提供统一 API：
- `POST /api/chat`：统一代理多模型厂商
- `POST /api/agents/{agent_id}/run`：多智能体入口
- `POST /api/files/upload`：文件上传
- `GET /api/files/{file_id}`：文件访问
- `GET /api/files`：文件列表
- `GET /health：健康检查
- POST /api/ecgomics/analyze：ECGOmics 分析代理
- POST /api/ecg-reconstruct：CSV 心电重建代理
- POST /api/chest-pain/predict：准心胸痛模型代理

---

## 1) macOS 本机启动

### 前置条件
- Python 3.10+（建议 3.11）

### 启动后端
```bash
cd /Users/chen/Documents/HeartOS_system/HeartOS/heartos_backend
./start.sh
```

常用调试参数：
```bash
# 临时换端口，不修改 .env
APP_PORT=9010 ./start.sh

# 关闭热重载（某些受限目录或沙箱环境下需要）
HEARTOS_RELOAD=0 ./start.sh

# 依赖已经安装好时，跳过 pip install
HEARTOS_SKIP_PIP_INSTALL=1 ./start.sh

# 指定 Python 3.10+ 路径
PYTHON_BIN=/path/to/python3.12 ./start.sh
```

`start.sh` 会自动处理：
- 如果 `.env` 不存在，从 `.env.example` 复制一份
- 如果 `.venv` 是 Windows 复制过来的环境，备份为 `.venv.windows-backup.<时间>`
- 自动选择 Python 3.10+ 并创建 macOS 可用的 `.venv`
- 安装 `requirements.txt`
- 按 `.env` 中的 `APP_HOST` / `APP_PORT` 启动服务

本地调试建议：
```env
APP_AUTH_MODE=local
```

后端默认监听：`http://127.0.0.1:9000`

健康检查：
- 打开 `http://127.0.0.1:9000/health`

---

## 2) Windows 本机部署

### 前置条件
- Python 3.10+（建议 3.11）

### 启动后端
```powershell
cd E:\HeartOS\heartos_backend
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

后端默认监听：`http://127.0.0.1:9000`

健康检查：
- 打开 `http://127.0.0.1:9000/health`

---

## 3) Docker 部署（Windows/Linux 通用）

```powershell
cd E:\HeartOS\heartos_backend
docker compose up -d --build
```

停止：
```powershell
docker compose down
```

---

## 4) 启动前端（HeartOS 与 HandECG 同目录）

```bash
cd /Users/chen/Documents/HeartOS_system/HeartOS
python3 -m http.server 8080
```

打开：
- `http://127.0.0.1:8080/index.html`

---

## 5) 前端如何连接后端

前端默认连接：`http://127.0.0.1:9000`

如果后端地址变了，在浏览器控制台执行：
```js
localStorage.setItem('heartos_backend_base', 'http://你的地址:端口')
location.reload()
```

---

## 6) 稳定性与兼容性建议

- 前端与后端用同一域名或同一网段，减少跨域问题
- CORS 在生产环境改成精确域名，不建议长期 `*`
- 保持 `docker compose` 的 `restart: unless-stopped`
- 上传文件建议控制大小（当前默认 20MB）
- 定期备份 `data/uploads`

---

## 7) 常见问题

### 1. `/health` 打不开
- 检查端口 9000 是否被占用
- 检查 Docker 容器是否 `Up`

### 2. 前端能打开但 AI 不回复
- 检查后端日志是否有上游模型报错
- 检查 API Key 是否有效

### 3. HandECG 打开但没图
- 确认 HeartOS 和 `ecg_digitizer_enhanced.html` 在同目录
- 确认是通过 `http://127.0.0.1:8080/...` 打开，而不是 `file:///...`


## ECGOmics 接入

后端已内置 ECGOmics 代理，默认上游地址：http://110.157.241.24:18023/ECGOmics。

前端在 HandECG 提取完成后会自动调用：POST /api/ecgomics/analyze。
如需修改地址，设置环境变量 APP_ECGOMICS_URL 后重启后端。

## ECG 重建接入

后端已内置 CSV 心电重建代理，默认上游地址：http://219.147.100.43:18007/reconstruct。

调用方式：
```powershell
curl.exe -X POST http://127.0.0.1:9000/api/ecg-reconstruct `
  -H "Authorization: Bearer <token>" `
  -F "file=@D:\Project\Models\3399_20170921085252.csv;type=text/csv"
```

接口会将 CSV 以 multipart/form-data 的 `file` 字段转发给上游，并返回：
```json
{
  "ok": true,
  "upstream": {
    "code": 200,
    "msg": "SUCCESS",
    "data": {
      "fs_in": 192.69,
      "fs_out": 500.0,
      "ecgDataRaw": {},
      "ecgData": {},
      "image": "base64..."
    }
  }
}
```

如需修改地址，设置环境变量 APP_ECG_RECONSTRUCT_URL 后重启后端。

## 准心胸痛模型接入

后端已内置准心胸痛模型代理，默认上游地址：
`http://110.157.241.3:18008/predict_text`

如需修改地址，设置环境变量：
```env
APP_CHEST_PAIN_PREDICT_URL=http://110.157.241.3:18008/predict_text
```

调用方式：
```bash
curl -X POST http://127.0.0.1:9000/api/chest-pain/predict \
  -H "Authorization: Bearer <token>" \
  -F "file=@/path/to/ecg.jpg" \
  -F "use_optimized_rank=true" \
  -F "show_score=true"
```

返回示例：
```json
{
  "ok": true,
  "raw_text": "不稳定性心绞痛: ...",
  "scores": {
    "0": 1.23,
    "1": 0.56,
    "2": 3.45
  },
  "high_risk": ["ST段抬高型心肌梗死"],
  "low_risk": ["肺栓塞"],
  "ranking": [
    {"class_id": 2, "class_name": "ST段抬高型心肌梗死", "score": 3.45}
  ],
  "report": "###\n高风险：...\n低风险：...\n疾病可能性排序：...\n###"
}
```



## 登录鉴权

- 登录接口：POST /api/auth/login`n- 当前用户：GET /api/auth/me`n- 默认账号：dmin / dmin123（请上线后立即修改 .env 中 APP_DEFAULT_PASSWORD 并重启）
- 所有 /api/* 业务接口都需要 Authorization: Bearer <token>。

