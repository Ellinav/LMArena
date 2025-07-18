import json
import time
import uuid

# 全局变量，用于从主程序api_server.py接收response_channels字典
response_channels = None

def initialize_converter(channels):
    """从主程序接收response_channels的引用"""
    global response_channels
    response_channels = channels

def convert_openai_to_lmarena_payload(
    openai_req: dict, 
    session_id: str, 
    message_id: str, 
    model_name_to_id_map: dict, 
    default_model_id: str,
    mode_override: str = None, 
    battle_target_override: str = None
) -> dict:
    """
    将OpenAI格式的请求转换为LMArena油猴脚本可以理解的负载。
    """
    model_name = openai_req.get("model", "")
    target_model_id = model_name_to_id_map.get(model_name, default_model_id)
    
    openai_messages = openai_req.get("messages", [])
    
    # 构建LMArena需要的message_templates
    message_templates = []
    for msg in openai_messages:
        new_msg = {
            "role": msg.get("role"),
            "content": msg.get("content")
        }
        
        # --- 【【【核心修复逻辑】】】 ---
        # 根据角色，为每条消息添加 LMArena 需要的 participantPosition 字段
        # 这是模拟 LMArena 网站自身行为的关键
        role = new_msg.get("role")
        if role == "user" or role == "assistant":
            # 用户和助手的消息，立场都设为 'a'
            new_msg["participantPosition"] = "a"
        elif role == "system":
            # 系统的消息，立场设为 'b' (根据旧版代码逻辑)
            new_msg["participantPosition"] = "b"
        # ---------------------------------
            
        message_templates.append(new_msg)

    # 构建最终的负载
    lmarena_payload = {
        "message_templates": message_templates,
        "target_model_id": target_model_id,
        "session_id": session_id,
        "message_id": message_id
    }

    if mode_override:
        lmarena_payload["mode"] = mode_override
    if battle_target_override:
        lmarena_payload["battle_target"] = battle_target_override
        
    return lmarena_payload


async def stream_generator(request_id: str, model_name: str):
    """
    用于流式响应的异步生成器。
    它会从队列中不断获取数据，并按照Server-Sent Events (SSE)格式进行包装。
    """
    try:
        while True:
            data_chunk = await response_channels[request_id].get()
            
            if data_chunk == "[DONE]":
                break
            
            if isinstance(data_chunk, dict) and 'error' in data_chunk:
                error_payload = {
                    "error": {
                        "message": data_chunk['error'],
                        "type": "bridge_error",
                        "code": 500
                    }
                }
                yield f"data: {json.dumps(error_payload)}\n\n"
                break
            
            response_json = {
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": data_chunk},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(response_json)}\n\n"
            
        final_chunk = {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    finally:
        if request_id in response_channels:
            del response_channels[request_id]


async def non_stream_response(request_id: str, model_name: str):
    """
    用于非流式响应。
    它会累积所有的数据块，最后一次性返回一个完整的OpenAI格式的响应。
    """
    full_content = ""
    try:
        while True:
            data_chunk = await response_channels[request_id].get()
            if data_chunk == "[DONE]":
                break
            if isinstance(data_chunk, dict) and 'error' in data_chunk:
                full_content = f"Error from bridge: {data_chunk['error']}"
                break
            full_content += data_chunk
    finally:
        if request_id in response_channels:
            del response_channels[request_id]

    response_json = {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": full_content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(full_content.split()),
            "total_tokens": len(full_content.split()),
        },
    }
    return response_json
