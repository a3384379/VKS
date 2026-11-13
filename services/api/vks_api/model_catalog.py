"""Provider defaults; presets are hints, account discovery is authoritative."""

PROVIDERS = [
    {
        "id": "openai",
        "name": "OpenAI",
        "description": "GPT 系列 · Responses API",
        "endpoint": "https://api.openai.com/v1",
        "protocol": "responses",
        "models": [
            {"id": "gpt-6-luna", "name": "GPT-6 Luna", "description": "日常问答 · 轻量高效"},
            {"id": "gpt-6.1-terra", "name": "GPT-6.1 Terra", "description": "快速通用 · 自然表达"},
            {"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol", "description": "复杂分析 · 综合能力"},
            {"id": "gpt-6-astra", "name": "GPT-6 Astra", "description": "高难度推理与分析"},
        ],
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "description": "DeepSeek 系列 · 兼容接口",
        "endpoint": "https://api.deepseek.com",
        "protocol": "chat_completions",
        "models": [
            {"id": "deepseek-flash", "name": "DeepSeek Flash", "description": "日常问答与推理"},
            {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro", "description": "复杂分析"},
        ],
    },
    {
        "id": "qwen",
        "name": "阿里云百炼",
        "description": "Qwen 系列 · OpenAI 兼容",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "protocol": "chat_completions",
        "models": [{"id": "qwen3.8-max", "name": "Qwen 3.8 Max", "description": "预设示例 · 以账户模型列表为准"}],
    },
    {
        "id": "custom",
        "name": "自定义服务",
        "description": "中转服务 / OpenAI 兼容接口",
        "endpoint": "",
        "protocol": "chat_completions",
        "models": [],
    },
]
