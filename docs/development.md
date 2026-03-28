# 测试与开发

## 运行测试

```bash
pytest -q
```

如遇模块导入路径问题，可使用：

```bash
PYTHONPATH=. pytest -q tests/test_config.py tests/test_web_entrypoints.py tests/test_task_manager.py
```

## 开发建议
- 修改配置结构时同步更新 `config.example.yaml`。
- Web 路由变更时建议补充 `tests/test_web_entrypoints.py`。
- 推荐/过滤逻辑变更时建议补充 `tests/test_task_manager.py`。
