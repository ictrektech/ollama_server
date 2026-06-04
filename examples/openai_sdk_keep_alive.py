from openai import OpenAI


client = OpenAI(
    base_url="http://localhost:11535/v1/",
    api_key="ollama",
)


# 常驻模型：keep_alive=-1 表示模型加载后尽量常驻。
resp = client.chat.completions.create(
    model="qwen3:0.6b",
    messages=[{"role": "user", "content": "介绍一下李白"}],
    max_tokens=2048,
    extra_body={
        "keep_alive": -1,
        # "keep_alive": "5m", # 控制保活时长
        # "keep_alive": "1h",
        "options": {
            "num_ctx": 32768,
        },
    },
)

print(resp.choices[0].message.content)


# 停止模型：keep_alive=0 表示请求结束后立即卸载模型。
resp = client.completions.create(
    model="qwen3:0.6b",
    prompt="",
    extra_body={
        "keep_alive": 0,
    },
)

print(resp)
