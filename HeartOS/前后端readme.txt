打开终端A（后端）：
cd E:\HeartOS\heartos_backend
powershell -ExecutionPolicy Bypass -File .\start.ps1
看到 Uvicorn running on http://0.0.0.0:9000 说明后端启动成功。这个窗口不要关。

打开终端B（前端静态服务）：
cd E:\HeartOS
python -m http.server 8080  这个窗口也不要关。

浏览器打开：
前端页面：http://127.0.0.1:8080/index.html
后端健康：http://127.0.0.1:9000/health

监控方法（最实用）

后端是否在线
浏览器访问 http://127.0.0.1:9000/health，返回 status: ok 即正常。

前端是否在线
浏览器能打开 http://127.0.0.1:8080/noteai_v4_0423.html 即正常。

ECGOmics 调用是否真的发生
在 HeartOS 上传 XML 后点击“心电图数字化”，看终端A是否出现：

POST /api/ecgomics/analyze
这条日志最关键。
浏览器侧接口状态
按 F12 -> Network，过滤 ecgomics，看请求：

URL: /api/ecgomics/analyze
Status: 200 为成功


服务器端启动指令
uvicorn app.main:app --host 0.0.0.0 --port 18005

python -m http.server 18006