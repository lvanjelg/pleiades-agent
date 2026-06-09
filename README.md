# agentresearch

## curl commands
### qwen/qwen3.5-9b

### simple
curl http://localhost:1234/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen/qwen3.5-9b",
    "system_prompt": "You answer only in rhymes.",
    "input": "What is your favorite color?"
}'

### ephemeral mcp
curl http://localhost:1234/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen/qwen3.5-9b",
    "input": "What is the top trending model on hugging face?",
    "integrations": [
        {
            "type": "ephemeral_mcp",
            "server_label": "huggingface",
            "server_url": "https://huggingface.co/mcp",
            "allowed_tools": [
                "model_search"
            ]
        }
    ]
}'

### custom tool calling
curl http://localhost:1234/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen/qwen3.5-9b",
    "input": "What is the weather like in Boston today?",
    "tools": [
        {
            "type": "function",
            "name": "get_current_weather",
            "description": "Get the current weather in a given location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city and state, e.g. San Francisco, CA"
                    },
                    "unit": {
                        "type": "string",
                        "enum": [
                            "celsius",
                            "fahrenheit"
                        ]
                    }
                },
                "required": [
                    "location",
                    "unit"
                ]
            }
        }
    ],
    "tool_choice": "auto"
}'