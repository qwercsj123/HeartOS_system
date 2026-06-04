# HeartOS 部署傻瓜版

以后部署最简单的方式：

```bash
./use-deploy-config.sh local
```

或者：

```bash
./use-deploy-config.sh server
```

脚本会自动复制对应模板到正式配置文件。

以后部署只改 2 个文件：

1. `heartos_backend/.env`
2. `deploy-config.js`

不要改这些文件：

- `index.html`
- `start.sh`
- `start.ps1`
- `docker-compose.yml`

它们现在都会自动读取配置。

---

## 一、本地启动

### 0. 一键切到本地配置

```bash
cd /Users/chen/Documents/heartos/HeartOS_system/HeartOS
./use-deploy-config.sh local
```

### 1. 后端配置

编辑 `heartos_backend/.env`，最少确认这几项：

```env
APP_HOST=0.0.0.0
APP_PORT=9010
APP_CORS_ORIGINS=http://127.0.0.1:8080,http://localhost:8080
APP_PUBLIC_BASE_URL=http://127.0.0.1:9010
APP_AUTH_MODE=local
```

### 2. 前端配置

编辑 `deploy-config.js`：

```js
window.HEARTOS_BACKEND_BASE_URL = 'http://127.0.0.1:9010';
```

### 3. 启动

后端：

```bash
cd /Users/chen/Documents/heartos/HeartOS_system/HeartOS/heartos_backend
./start.sh
```

前端：

```bash
cd /Users/chen/Documents/heartos/HeartOS_system/HeartOS
python3 -m http.server 8080
```

### 4. 打开页面

- 前端：`http://127.0.0.1:8080/index.html`
- 健康检查：`http://127.0.0.1:9010/health`

---

## 二、服务器部署

假设：

- 前端地址：`http://219.147.100.43:18009`
- 后端地址：`http://219.147.100.43:18008`

### 0. 一键切到服务器配置

```bash
cd /Users/chen/Documents/heartos/HeartOS_system/HeartOS
./use-deploy-config.sh server
```

### 1. 后端配置

编辑 `heartos_backend/.env`：

```env
APP_HOST=0.0.0.0
APP_PORT=18008
APP_CORS_ORIGINS=http://219.147.100.43:18009
APP_PUBLIC_BASE_URL=http://219.147.100.43:18008
APP_AUTH_MODE=local
```

如果服务器要接外部账号系统，改成：

```env
APP_AUTH_MODE=upstream
APP_AUTH_UPSTREAM_BASE=https://你的认证服务地址
```

### 2. 前端配置

编辑 `deploy-config.js`：

```js
window.HEARTOS_BACKEND_BASE_URL = 'http://219.147.100.43:18008';
```

### 3. 启动

后端：

```bash
cd /Users/chen/Documents/heartos/HeartOS_system/HeartOS/heartos_backend
./start.sh
```

前端：

```bash
cd /Users/chen/Documents/heartos/HeartOS_system/HeartOS
python3 -m http.server 18009
```

---

## 三、Docker 部署

Docker 现在也读 `heartos_backend/.env`。

### 1. 先改 `heartos_backend/.env`

例如：

```env
APP_HOST=0.0.0.0
APP_PORT=18008
APP_CORS_ORIGINS=http://219.147.100.43:18009
APP_PUBLIC_BASE_URL=http://219.147.100.43:18008
APP_AUTH_MODE=local
```

### 2. 再改 `deploy-config.js`

```js
window.HEARTOS_BACKEND_BASE_URL = 'http://219.147.100.43:18008';
```

### 3. 启动 Docker

```bash
cd /Users/chen/Documents/heartos/HeartOS_system/HeartOS/heartos_backend
docker compose up -d --build
```

---

## 四、每次部署只检查这 5 行

只看后端 `.env`：

```env
APP_HOST=
APP_PORT=
APP_CORS_ORIGINS=
APP_PUBLIC_BASE_URL=
APP_AUTH_MODE=
```

只看前端 `deploy-config.js`：

```js
window.HEARTOS_BACKEND_BASE_URL = '...';
```

---

## 五、最常见报错怎么查

### 1. 前端提示“暂时无法连接服务”

先检查：

- 后端是否启动
- `deploy-config.js` 是否指向正确后端
- `.env` 里的 `APP_CORS_ORIGINS` 是否写成了“前端页面地址”

注意：

- `APP_CORS_ORIGINS` 写的是前端地址，不是后端地址

例子：

- 前端页面是 `http://219.147.100.43:18009`
- 那么 `APP_CORS_ORIGINS` 就写 `http://219.147.100.43:18009`

### 2. 明明改了配置，但页面还是连旧地址

浏览器控制台执行：

```js
localStorage.setItem('heartos_backend_base', 'http://你的后端地址:端口')
location.reload()
```

### 3. 怎么确认后端活着

浏览器打开：

```text
http://你的后端地址:端口/health
```

能返回：

```json
{"status":"ok","name":"HeartOS Backend","version":"1.4.1"}
```

就说明后端正常。

---

## 六、以后别再乱改的原则

以后只改：

- `heartos_backend/.env`
- `deploy-config.js`

以后不要改：

- `index.html`
- `start.sh`
- `start.ps1`
- `docker-compose.yml`

除非你是在改程序功能，不是在部署。
