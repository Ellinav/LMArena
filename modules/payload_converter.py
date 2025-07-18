import asyncio
import json
import time
import uuid
import re

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
    
    message_templates = []
    for msg in openai_messages:
        new_msg = {
            "role": msg.get("role"),
            "content": msg.get("content")
        }
        
        role = new_msg.get("role")
        if role == "user" or role == "assistant":
            new_msg["participantPosition"] = "a"
        elif role == "system":
            new_msg["participantPosition"] = "b"
            
        message_templates.append(new_msg)

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

# --- 以下是移植自旧版api_server.py的、经过优化的响应处理逻辑 ---

async def _process_lmarena_stream(request_id: str):
    """
    核心内部生成器：处理来自浏览器的原始数据流，并产生结构化事件。
    事件类型: ('content', str), ('finish', str), ('error', str)
    """
    queue = response_channels.get(request_id)
    if not queue:
        yield 'error', 'Internal server error: response channel not found.'
        return

    buffer = ""
    timeout = 360  # 默认超时时间
    text_pattern = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)"')
    finish_pattern = re.compile(r'[ab]d:(\{.*?"finishReason".*?\})')

    try:
        while True:
            try:
                raw_data = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                yield 'error', f'Response timed out after {timeout} seconds.'
                return

            if isinstance(raw_data, dict) and 'error' in raw_data:
                yield 'error', raw_data.get('error', 'Unknown browser error')
                return
            if raw_data == "[DONE]":
                break

            buffer += raw_data

            # 从缓冲区中持续解析出所有可用的文本块
            while (match := text_pattern.search(buffer)):
                try:
                    # 使用json.loads来正确处理转义字符，例如 \" -> "
                    text_content = json.loads(f'"{match.group(1)}"')
                    if text_content:
                        yield 'content', text_content
                except (ValueError, json.JSONDecodeError):
                    # 如果解析失败，可能是无效的转义序列，跳过
                    pass
                # 移除已处理的部分
                buffer = buffer[match.end():]
            
            # 检查是否有结束信号
            if (finish_match := finish_pattern.search(buffer)):
                try:
                    finish_data = json.loads(finish_match.group(1))
                    yield 'finish', finish_data.get("finishReason", "stop")
                except (json.JSONDecodeError, IndexError):
                    pass
                buffer = buffer[finish_match.end():]

    finally:
        if request_id in response_channels:
            del response_channels[request_id]


async def stream_generator(request_id: str, model_name: str):
    """将内部事件流格式化为 OpenAI SSE 响应。"""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    finish_reason_to_send = 'stop'

    async for event_type, data in _process_lmarena_stream(request_id):
        if event_type == 'content':
            response_json = {
                "id": response_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model_name,
                "choices": [{"index": 0, "delta": {"content": data}, "finish_reason": None}]
            }
            yield f"data: {json.dumps(response_json)}\n\n"
        elif event_type == 'finish':
            finish_reason_to_send = data
        elif event_type == 'error':
            error_payload = {
                "error": {"message": str(data), "type": "bridge_error", "code": 500}
            }
            yield f"data: {json.dumps(error_payload)}\n\n"
            finish_reason_to_send = 'error'
            break # 发生错误，提前终止

    # 发送最后一个包含结束原因的数据块
    final_chunk = {
        "id": response_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model_name,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason_to_send}],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"


async def non_stream_response(request_id: str, model_name: str):
    """聚合内部事件流并返回单个 OpenAI JSON 响应。"""
    full_content = []
    finish_reason = "stop"

    async for event_type, data in _process_lmarena_stream(request_id):
        if event_type == 'content':
            full_content.append(data)
        elif event_type == 'finish':
            finish_reason = data
        elif event_type == 'error':
            return {"error": {"message": str(data), "type": "bridge_error"}}

    final_content = "".join(full_content)
    response_json = {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": final_content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    return response_json
