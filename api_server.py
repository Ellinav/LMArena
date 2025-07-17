# api_server.py (云端终极版)
# 融合了作者的新功能（ID映射、多模态）与我们所有的云端适配优化

import asyncio
import json
import logging
import os
import sys
import subprocess
import time
import uuid
import re
import threading
import random
import mimetypes
from datetime import datetime
from contextlib import asynccontextmanager

import uvicorn
import requests
from packaging.version import parse as parse_version
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from typing import Optional

# --- 导入自定义模块 ---
from modules import image_generation

# --- 基础配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 全局状态与配置 ---
CONFIG = {}
browser_ws: WebSocket | None = None
response_channels: dict[str, asyncio.Queue] = {}
last_activity_time = None
idle_monitor_thread = None
WARNED_UNKNOWN_IDS = set() # 用于“只警告一次”
main_event_loop = None

# --- 模型映射 ---
MODEL_NAME_TO_ID_MAP = {}
MODEL_ENDPOINT_MAP = {} # 核心：模型到 session/message ID 的映射
DEFAULT_MODEL_ID = "f44e280a-7914-43ca-a25d-ecfcc5d48d09"

class EndpointUpdatePayload(BaseModel):
    model_name: str = Field(..., alias='modelName')
    session_id: str = Field(..., alias='sessionId')
    message_id: str = Field(..., alias='messageId')
    mode: str
    battle_target: Optional[str] = Field(None, alias='battleTarget')

def load_model_endpoint_map():
    global MODEL_ENDPOINT_MAP
    try:
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            content = f.read()
            if not content.strip(): MODEL_ENDPOINT_MAP = {}
            else: MODEL_ENDPOINT_MAP = json.loads(content)
        logger.info(f"成功从 'model_endpoint_map.json' 加载了 {len(MODEL_ENDPOINT_MAP)} 个模型端点映射。")
    except FileNotFoundError:
        logger.warning("'model_endpoint_map.json' 文件未找到。将使用空映射。")
        MODEL_ENDPOINT_MAP = {}
    except json.JSONDecodeError as e:
        logger.error(f"加载或解析 'model_endpoint_map.json' 失败: {e}。将使用空映射。")
        MODEL_ENDPOINT_MAP = {}

def load_config():
    global CONFIG
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
            json_content = re.sub(r'//.*|/\*[\s\S]*?\*/', '', content)
            CONFIG = json.loads(json_content)
        logger.info("成功从 'config.jsonc' 加载配置。")
        logger.info(f"  - 酒馆模式 (Tavern Mode): {'✅ 启用' if CONFIG.get('tavern_mode_enabled') else '❌ 禁用'}")
        logger.info(f"  - 绕过模式 (Bypass Mode): {'✅ 启用' if CONFIG.get('bypass_enabled') else '❌ 禁用'}")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"加载或解析 'config.jsonc' 失败: {e}。将使用默认配置。")
        CONFIG = {}

def load_model_map():
    global MODEL_NAME_TO_ID_MAP
    try:
        with open('models.json', 'r', encoding='utf-8') as f:
            MODEL_NAME_TO_ID_MAP = json.load(f)
        logger.info(f"成功从 'models.json' 加载了 {len(MODEL_NAME_TO_ID_MAP)} 个模型。")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"加载 'models.json' 失败: {e}。将使用空模型列表。")
        MODEL_NAME_TO_ID_MAP = {}

async def send_pings():
    """在后台持续发送ping消息以保持WebSocket连接活跃。"""
    while True:
        await asyncio.sleep(30)
        if browser_ws:
            try:
                await browser_ws.send_text(json.dumps({"command": "ping"}))
                logger.debug("Ping sent to client.")
            except Exception:
                logger.debug("Ping发送失败，可能连接已关闭。")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务器生命周期管理。"""
    global main_event_loop, last_activity_time, idle_monitor_thread
    main_event_loop = asyncio.get_running_loop()
    load_config()
    load_model_map()
    load_model_endpoint_map()
    logger.info("服务器启动完成。等待油猴脚本连接...")
    asyncio.create_task(send_pings())
    logger.info("已启动WebSocket心跳维持任务。")
    last_activity_time = datetime.now()
    if CONFIG.get("enable_idle_restart", False):
        idle_monitor_thread = threading.Thread(target=idle_monitor, daemon=True)
        idle_monitor_thread.start()
    image_generation.initialize_image_module(logger, response_channels, CONFIG, MODEL_NAME_TO_ID_MAP, DEFAULT_MODEL_ID)
    yield
    logger.info("服务器正在关闭。")

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- 云端适配API端点 ---
@app.post("/v1/add-or-update-endpoint")
async def add_or_update_endpoint(payload: EndpointUpdatePayload):
    global MODEL_ENDPOINT_MAP
    new_entry = payload.dict(exclude_none=True, by_alias=True)
    model_name = new_entry.pop("model_name")
    if model_name in MODEL_ENDPOINT_MAP and isinstance(MODEL_ENDPOINT_MAP[model_name], list):
        MODEL_ENDPOINT_MAP[model_name].append(new_entry)
        logger.info(f"成功为模型 '{model_name}' 追加了一个新的端点映射。")
    else:
        MODEL_ENDPOINT_MAP[model_name] = [new_entry]
        logger.info(f"成功为模型 '{model_name}' 创建了新的端点映射列表。")
    return {"status": "success", "message": f"Endpoint for {model_name} updated."}

@app.get("/v1/export-map")
async def export_map(request: Request):
    api_key = os.environ.get("API_KEY") or CONFIG.get("api_key")
    if api_key and request.headers.get('Authorization') != f"Bearer {api_key}":
        raise HTTPException(status_code=401, detail="Invalid API Key")
    logger.info("正在导出模型端点映射...")
    return JSONResponse(content=MODEL_ENDPOINT_MAP)

@app.post("/v1/import-map")
async def import_map(request: Request):
    global MODEL_ENDPOINT_MAP
    api_key = os.environ.get("API_KEY") or CONFIG.get("api_key")
    if api_key and request.headers.get('Authorization') != f"Bearer {api_key}":
        raise HTTPException(status_code=401, detail="Invalid API Key")
    try:
        new_map = await request.json()
        if not isinstance(new_map, dict): raise HTTPException(status_code=400, detail="Request body must be a valid JSON object.")
        MODEL_ENDPOINT_MAP = new_map
        logger.info(f"成功从API导入了 {len(MODEL_ENDPOINT_MAP)} 个模型端点映射！")
        return {"status": "success", "message": f"Map imported with {len(MODEL_ENDPOINT_MAP)} entries."}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON in request body.")

# --- WebSocket 端点 (整合了所有稳定版逻辑) ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global browser_ws, WARNED_UNKNOWN_IDS
    await websocket.accept()
    if browser_ws: logger.warning("检测到新的油猴脚本连接，旧的连接将被替换。")
    WARNED_UNKNOWN_IDS.clear()
    logger.info("✅ 油猴脚本已成功连接 WebSocket。")
    try:
        await websocket.send_text(json.dumps({"status": "connected"}))
    except Exception as e:
        logger.error(f"发送 'connected' 状态失败: {e}")
    browser_ws = websocket
    try:
        while True:
            message_str = await websocket.receive_text()
            message = json.loads(message_str)
            if message.get("status") == "pong":
                logger.debug("Pong received from client.")
                continue
            request_id = message.get("request_id")
            data = message.get("data")
            if not request_id or data is None:
                logger.warning(f"收到来自浏览器的无效消息: {message}")
                continue
            if request_id in response_channels:
                await response_channels[request_id].put(data)
            else:
                if request_id not in WARNED_UNKNOWN_IDS:
                    logger.warning(f"⚠️ 收到未知或已关闭请求的响应: {request_id}。将不再为此ID报告后续警告。")
                    WARNED_UNKNOWN_IDS.add(request_id)
    except WebSocketDisconnect:
        logger.warning("❌ 油猴脚本客户端已断开连接。")
    except Exception as e:
        logger.error(f"WebSocket 处理时发生未知错误: {e}", exc_info=True)
    finally:
        browser_ws = None
        if response_channels:
            logger.warning(f"WebSocket 连接断开！正在清理 {len(response_channels)} 个待处理的请求通道...")
        for queue in response_channels.values():
            await queue.put({"error": "Browser disconnected during operation"})
        response_channels.clear()
        logger.info("WebSocket 连接已清理。")
# --- 模型更新端点 ---
@app.post("/update_models")
async def update_models_endpoint(request: Request):
    """
    接收来自油猴脚本的页面 HTML，提取并更新模型列表。
    """
    html_content = await request.body()
    if not html_content:
        logger.warning("模型更新请求未收到任何 HTML 内容。")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "No HTML content received."}
        )
    
    logger.info("收到来自油猴脚本的页面内容，开始检查并更新模型...")
    new_models_list = extract_models_from_html(html_content.decode('utf-8'))
    
    if new_models_list:
        compare_and_update_models(new_models_list, 'models.json')
        # load_model_map() is now called inside compare_and_update_models
        return JSONResponse({"status": "success", "message": "Model comparison and update complete."})
    else:
        logger.error("未能从油猴脚本提供的 HTML 中提取模型数据。")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Could not extract model data from HTML."}
        )

# --- OpenAI 兼容 API 端点 ---
@app.get("/v1/models")
async def get_models():
    """提供兼容 OpenAI 的模型列表。"""
    if not MODEL_NAME_TO_ID_MAP:
        return JSONResponse(
            status_code=404,
            content={"error": "模型列表为空或 'models.json' 未找到。"}
        )
    
    return {
        "object": "list",
        "data": [
            {
                "id": model_name, 
                "object": "model",
                "created": int(asyncio.get_event_loop().time()), 
                "owned_by": "LMArenaBridge"
            }
            for model_name in MODEL_NAME_TO_ID_MAP.keys()
        ],
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    处理聊天补全请求。
    接收 OpenAI 格式的请求，将其转换为 LMArena 格式，
    通过 WebSocket 发送给油猴脚本，然后流式返回结果。
    """
    global last_activity_time
    last_activity_time = datetime.now() # 更新活动时间
    logger.info(f"API请求已收到，活动时间已更新为: {last_activity_time.strftime('%Y-%m-%d %H:%M:%S')}")

    load_config()  # 实时加载最新配置，确保会话ID等信息是最新的
    # --- API Key 验证 ---
    api_key = os.environ.get("API_KEY") or CONFIG.get("api_key")
    if api_key:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise HTTPException(
                status_code=401,
                detail="未提供 API Key。请在 Authorization 头部中以 'Bearer YOUR_KEY' 格式提供。"
            )
        
        provided_key = auth_header.split(' ')[1]
        if provided_key != api_key:
            raise HTTPException(
                status_code=401,
                detail="提供的 API Key 不正确。"
            )

    if not browser_ws:
        raise HTTPException(status_code=503, detail="油猴脚本客户端未连接。请确保 LMArena 页面已打开并激活脚本。")

    try:
        openai_req = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

    # --- 模型与会话ID映射逻辑 ---
    model_name = openai_req.get("model")
    session_id, message_id = None, None
    mode_override, battle_target_override = None, None

    if model_name and model_name in MODEL_ENDPOINT_MAP:
        mapping_entry = MODEL_ENDPOINT_MAP[model_name]
        selected_mapping = None

        if isinstance(mapping_entry, list) and mapping_entry:
            selected_mapping = random.choice(mapping_entry)
            logger.info(f"为模型 '{model_name}' 从ID列表中随机选择了一个映射。")
        elif isinstance(mapping_entry, dict):
            selected_mapping = mapping_entry
            logger.info(f"为模型 '{model_name}' 找到了单个端点映射（旧格式）。")
        
        if selected_mapping:
            session_id = selected_mapping.get("session_id")
            message_id = selected_mapping.get("message_id")
            # 关键：同时获取模式信息
            mode_override = selected_mapping.get("mode") # 可能为 None
            battle_target_override = selected_mapping.get("battle_target") # 可能为 None
            log_msg = f"将使用 Session ID: ...{session_id[-6:] if session_id else 'N/A'}"
            if mode_override:
                log_msg += f" (模式: {mode_override}"
                if mode_override == 'battle':
                    log_msg += f", 目标: {battle_target_override or 'A'}"
                log_msg += ")"
            logger.info(log_msg)

    # 如果经过以上处理，session_id 仍然是 None，则进入全局回退逻辑
    if not session_id:
        if CONFIG.get("use_default_ids_if_mapping_not_found", True):
            session_id = CONFIG.get("session_id")
            message_id = CONFIG.get("message_id")
            # 当使用全局ID时，不设置模式覆盖，让其使用全局配置
            mode_override, battle_target_override = None, None
            logger.info(f"模型 '{model_name}' 未找到有效映射，根据配置使用全局默认 Session ID: ...{session_id[-6:] if session_id else 'N/A'}")
        else:
            logger.error(f"模型 '{model_name}' 未在 'model_endpoint_map.json' 中找到有效映射，且已禁用回退到默认ID。")
            raise HTTPException(
                status_code=400,
                detail=f"模型 '{model_name}' 没有配置独立的会话ID。请在 'model_endpoint_map.json' 中添加有效映射或在 'config.jsonc' 中启用 'use_default_ids_if_mapping_not_found'。"
            )

    # --- 验证最终确定的会话信息 ---
    if not session_id or not message_id or "YOUR_" in session_id or "YOUR_" in message_id:
        raise HTTPException(
            status_code=400,
            detail="最终确定的会话ID或消息ID无效。请检查 'model_endpoint_map.json' 和 'config.jsonc' 中的配置，或运行 `id_updater.py` 来更新默认值。"
        )

    if not model_name or model_name not in MODEL_NAME_TO_ID_MAP:
        logger.warning(f"请求的模型 '{model_name}' 不在 models.json 中，将使用默认模型ID。")

    request_id = str(uuid.uuid4())
    response_channels[request_id] = asyncio.Queue()
    logger.info(f"API CALL [ID: {request_id[:8]}]: 已创建响应通道。")

    try:
        # 1. 转换请求，传入可能存在的模式覆盖信息
        lmarena_payload = convert_openai_to_lmarena_payload(
            openai_req,
            session_id,
            message_id,
            mode_override=mode_override,
            battle_target_override=battle_target_override
        )
        
        # 2. 包装成发送给浏览器的消息
        message_to_browser = {
            "request_id": request_id,
            "payload": lmarena_payload
        }
        
        # 3. 通过 WebSocket 发送
        logger.info(f"API CALL [ID: {request_id[:8]}]: 正在通过 WebSocket 发送载荷到油猴脚本。")
        await browser_ws.send_text(json.dumps(message_to_browser))

        # 4. 根据 stream 参数决定返回类型
        is_stream = openai_req.get("stream", True)

        if is_stream:
            # 返回流式响应
            return StreamingResponse(
                stream_generator(request_id, model_name or "default_model"),
                media_type="text/event-stream"
            )
        else:
            # 返回非流式响应
            return await non_stream_response(request_id, model_name or "default_model")
    except Exception as e:
        # 如果在设置过程中出错，清理通道
        if request_id in response_channels:
            del response_channels[request_id]
        logger.error(f"API CALL [ID: {request_id[:8]}]: 处理请求时发生致命错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/images/generations")
async def images_generations(request: Request):
    """
    处理文生图请求。
    该端点接收 OpenAI 格式的图像生成请求，并返回相应的图像 URL。
    """
    global last_activity_time
    last_activity_time = datetime.now()
    logger.info(f"文生图 API 请求已收到，活动时间已更新为: {last_activity_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 模块已经通过 `initialize_image_module` 初始化，可以直接调用
    response_data, status_code = await image_generation.handle_image_generation_request(request, browser_ws)
    
    return JSONResponse(content=response_data, status_code=status_code)

# --- 内部通信端点 ---
@app.post("/internal/start_id_capture")
async def start_id_capture():
    """
    接收来自 id_updater.py 的通知，并通过 WebSocket 指令
    激活油猴脚本的 ID 捕获模式。
    """
    if not browser_ws:
        logger.warning("ID CAPTURE: 收到激活请求，但没有浏览器连接。")
        raise HTTPException(status_code=503, detail="Browser client not connected.")
    
    try:
        logger.info("ID CAPTURE: 收到激活请求，正在通过 WebSocket 发送指令...")
        await browser_ws.send_text(json.dumps({"command": "activate_id_capture"}))
        logger.info("ID CAPTURE: 激活指令已成功发送。")
        return JSONResponse({"status": "success", "message": "Activation command sent."})
    except Exception as e:
        logger.error(f"ID CAPTURE: 发送激活指令时出错: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send command via WebSocket.")

if __name__ == "__main__":
    api_port = int(os.environ.get("PORT", 7860))
    logger.info(f"🚀 LMArena Bridge API 服务器正在启动...")
    logger.info(f"   - 监听地址: http://0.0.0.0:{api_port}")
    uvicorn.run("api_server:app", host="0.0.0.0", port=api_port)