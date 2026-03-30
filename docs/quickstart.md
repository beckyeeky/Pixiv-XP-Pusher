# 快速上手（详细版）

## 1. 环境准备

```bash
git clone https://github.com/beckyeeky/Pixiv-XP-Pusher.git
cd Pixiv-XP-Pusher
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. 配置文件

```bash
cp config.example.yaml config.yaml
```

至少填写：
- `pixiv.refresh_token`
- `notifier.types`
- 对应通知器必填字段

可选：
- 运行 `python get_token.py` 获取/更新 refresh token。

## 3. 首次验证

```bash
python main.py --once
```

## 4. 常驻调度

```bash
python main.py --now
```

## 5. Web 控制台

```bash
uvicorn web.app:app --host 0.0.0.0 --port 8000
```

访问：`http://127.0.0.1:8000`
