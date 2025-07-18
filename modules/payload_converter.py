import json
import time
import uuid

# 全局变量，用于从主程序api_server.py接收response_channels字典
# 这样我们就不需要到处传递这个参数了
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
    
    # 从OpenAI请求中提取消息列表
    openai_messages = openai_req.get("messages", [])
    
    # 构建LMArena需要的message_templates
    message_templates = []
    for msg in openai_messages:
        # 只需要role和content两个核心字段
        message_templates.append({
            "role": msg.get("role"),
            "content": msg.get("content")
        })

    # 构建最终的负载
    lmarena_payload = {
        "message_templates": message_templates,
        "target_model_id": target_model_id,
        "session_id": session_id,
        "message_id": message_id
    }

    # 如果有battle模式的覆盖信息，也一并加入
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
            # 从与这个请求ID关联的队列中等待数据
            data_chunk = await response_channels[request_id].get()
            
            # 检查是否是结束信号
            if data_chunk == "[DONE]":
                break
            
            # 检查是否是错误信号
            if isinstance(data_chunk, dict) and 'error' in data_chunk:
                # 在流中报告错误并终止
                error_payload = {
                    "error": {
                        "message": data_chunk['error'],
                        "type": "bridge_error",
                        "code": 500
                    }
                }
                yield f"data: {json.dumps(error_payload)}\n\n"
                break
            
            # 构造OpenAI兼容的流式数据块
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
            # 使用SSE格式发送
            yield f"data: {json.dumps(response_json)}\n\n"
            
        # 发送最后一个数据块，包含finish_reason
        final_chunk = {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        # 发送最终的结束信号
        yield "data: [DONE]\n\n"

    finally:
        # 确保无论如何都从字典中移除这个请求的队列，防止内存泄漏
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

    # 构造OpenAI兼容的完整响应
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
            # 用法统计在这里无法精确计算，返回估算值
            "prompt_tokens": 0,
            "completion_tokens": len(full_content.split()), # 简单估算
            "total_tokens": len(full_content.split()),
        },
    }
    return response_json