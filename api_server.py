import asyncio, json, logging, os, sys, re, threading, random
from datetime import datetime
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from typing import Optional

# --- 导入自定义模块 (如果您的项目有) ---
# from modules import image_generation

# --- 基础配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 全局状态与配置 ---
CONFIG = {}
browser_ws: WebSocket | None = None
response_channels: dict[str, asyncio.Queue] = {}
last_activity_time = None
idle_monitor_thread = None
main_event_loop = None
MODEL_NAME_TO_ID_MAP = {}
MODEL_ENDPOINT_MAP = {}

# --- Pydantic 模型 ---
class EndpointUpdatePayload(BaseModel):
    model_name: str = Field(..., alias='modelName')
    session_id: str = Field(..., alias='sessionId')
    message_id: str = Field(..., alias='messageId')
    mode: str
    battle_target: Optional[str] = Field(None, alias='battleTarget')

class DeletePayload(BaseModel):
    model_name: str = Field(..., alias='modelName')
    session_id: str = Field(..., alias='sessionId')

# --- 核心加载函数 ---
def load_config():
    global CONFIG
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
            json_content = re.sub(r'//.*|/\*[\s\S]*?\*/', '', content)
            CONFIG = json.loads(json_content)
        logger.info("成功从 'config.jsonc' 加载配置。")
    except Exception as e:
        logger.error(f"加载 'config.jsonc' 失败: {e}。将使用空配置。")
        CONFIG = {}

def load_model_map():
    global MODEL_NAME_TO_ID_MAP
    try:
        with open('models.json', 'r', encoding='utf-8') as f:
            MODEL_NAME_TO_ID_MAP = json.load(f)
        logger.info(f"✅ [稳定版] 成功从 'models.json' 加载了 {len(MODEL_NAME_TO_ID_MAP)} 个模型。这是唯一的模型数据源。")
    except Exception as e:
        logger.error(f"加载 'models.json' 失败: {e}。模型列表将为空。请确保该文件存在且格式正确。")
        MODEL_NAME_TO_ID_MAP = {}

def load_model_endpoint_map():
    global MODEL_ENDPOINT_MAP
    try:
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            content = f.read()
            MODEL_ENDPOINT_MAP = json.loads(content) if content.strip() else {}
        logger.info(f"成功从 'model_endpoint_map.json' 加载了 {len(MODEL_ENDPOINT_MAP)} 个模型端点映射。")
    except Exception as e:
        logger.warning(f"加载或解析 'model_endpoint_map.json' 失败: {e}。将使用空映射。")
        MODEL_ENDPOINT_MAP = {}

# --- 心跳与闲置重启 ---
async def send_pings():
    while True:
        await asyncio.sleep(30)
        if browser_ws:
            try:
                await browser_ws.send_text(json.dumps({"command": "ping"}))
            except Exception:
                pass

def idle_monitor():
    # 此处省略具体实现，与您提供的代码一致
    pass

# --- FastAPI 生命周期 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_event_loop, last_activity_time, idle_monitor_thread
    main_event_loop = asyncio.get_running_loop()
    load_config()
    load_model_map()
    load_model_endpoint_map()
    logger.info("服务器启动完成。等待油猴脚本连接...")
    asyncio.create_task(send_pings())
    logger.info("已启动WebSocket心跳维持任务。")
    # ... 其他您需要的启动逻辑 ...
    yield
    logger.info("服务器正在关闭。")

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- WebSocket 端点 ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global browser_ws
    await websocket.accept()
    if browser_ws:
        logger.warning("检测到新的油猴脚本连接，旧的连接将被替换。")
    
    logger.info("✅ 油猴脚本已成功连接 WebSocket。")
    try:
        await websocket.send_text(json.dumps({"status": "connected"}))
    except Exception: pass
    
    browser_ws = websocket
    try:
        while True:
            message_str = await websocket.receive_text()
            message = json.loads(message_str)
            if message.get("status") == "pong": continue
            request_id = message.get("request_id")
            data = message.get("data")
            if request_id and data is not None and request_id in response_channels:
                await response_channels[request_id].put(data)
    except WebSocketDisconnect:
        logger.warning("❌ 油猴脚本客户端已断开连接。")
    finally:
        browser_ws = None
        for queue in response_channels.values():
            await queue.put({"error": "Browser disconnected"})
        response_channels.clear()
        logger.info("WebSocket 连接已清理。")

# --- 云端适配API端点 ---
@app.post("/v1/add-or-update-endpoint")
async def add_or_update_endpoint(payload: EndpointUpdatePayload):
    """接收并添加端点，增加了智能去重功能"""
    global MODEL_ENDPOINT_MAP
    new_entry = payload.dict(exclude_none=True, by_alias=True)
    model_name = new_entry.pop("modelName")
    
    # 【【【 核心修改：智能去重逻辑 】】】
    
    # 1. 如果是为新模型添加第一个ID，直接创建列表
    if model_name not in MODEL_ENDPOINT_MAP:
        MODEL_ENDPOINT_MAP[model_name] = [new_entry]
        logger.info(f"成功为新模型 '{model_name}' 创建了新的端点映射列表。")
        return {"status": "success", "message": f"Endpoint for {model_name} created."}

    # 2. 如果是为已存在的模型添加ID
    if isinstance(MODEL_ENDPOINT_MAP.get(model_name), list):
        endpoints = MODEL_ENDPOINT_MAP[model_name]
        new_session_id = new_entry.get('sessionId')

        # 检查这个 Session ID 是否已经存在于该模型的列表中
        is_duplicate = any(ep.get('sessionId') == new_session_id for ep in endpoints)
        
        if not is_duplicate:
            # 如果不是重复ID，则追加到列表
            endpoints.append(new_entry)
            logger.info(f"成功为模型 '{model_name}' 追加了一个新的端点映射。")
            return {"status": "success", "message": f"New endpoint for {model_name} appended."}
        else:
            # 如果是重复ID，则忽略并打印日志
            logger.info(f"检测到重复的 Session ID，已为模型 '{model_name}' 忽略本次添加。")
            return {"status": "skipped", "message": "Duplicate endpoint ignored."}
            
    # 如果数据结构有问题（不是列表），记录错误
    logger.error(f"为模型 '{model_name}' 添加端点时发生错误：数据结构不是预期的列表。")
    raise HTTPException(status_code=500, detail="Internal data structure error.")

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
    global browser_ws
    await websocket.accept()
    if browser_ws:
        logger.warning("检测到新的油猴脚本连接，旧的连接将被替换。")
    
    logger.info("✅ 油猴脚本已成功连接 WebSocket。")
    try:
        await websocket.send_text(json.dumps({"status": "connected"}))
    except Exception: pass
    
    browser_ws = websocket
    try:
        while True:
            message_str = await websocket.receive_text()
            message = json.loads(message_str)

            # 处理心跳响应
            if message.get("status") == "pong":
                continue

            # 处理服务器指令
            if message.get("command") in ["reconnect", "refresh"]:
                continue

            # 处理聊天/图像生成请求
            request_id = message.get("request_id")
            data = message.get("data")
            if request_id and data is not None and request_id in response_channels:
                await response_channels[request_id].put(data)

    except WebSocketDisconnect:
        logger.warning("❌ 油猴脚本客户端已断开连接。")
    finally:
        browser_ws = None
        for queue in response_channels.values():
            await queue.put({"error": "Browser disconnected"})
        response_channels.clear()
        logger.info("WebSocket 连接已清理。")

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

@app.get("/v1/get-model-list")
async def get_model_list():
    """
    提供一个简单的模型名称列表，供油猴脚本UI使用。
    """
    if not MODEL_NAME_TO_ID_MAP:
        logger.warning("请求模型列表失败，因为 MODEL_NAME_TO_ID_MAP 为空。")
        raise HTTPException(
            status_code=404,
            detail="服务器端模型列表为空。请确保 'models.json' 文件存在且内容正确。"
        )
    
    # 直接返回模型名称列表
    model_names = sorted(list(MODEL_NAME_TO_ID_MAP.keys()))
    logger.info(f"已向客户端提供 {len(model_names)} 个模型的列表。")
    return JSONResponse(content=model_names)

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

security = HTTPBasic()

class DeletePayload(BaseModel):
    model_name: str = Field(..., alias='modelName')
    session_id: str = Field(..., alias='sessionId')

def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    """处理浏览器弹出的密码窗口，验证API Key"""
    server_api_key = os.environ.get("API_KEY") or CONFIG.get("api_key")
    if server_api_key:
        # credentials.password 就是用户在弹窗中输入的密码
        if credentials.password == server_api_key:
            return credentials.username
    # 如果没有设置API Key，或者密码错误，则拒绝访问
    # （对于未设置key的情况，为安全起见，此管理页面仍然需要一个象征性的密码）
    raise HTTPException(
        status_code=401,
        detail="Incorrect API Key or credentials",
        headers={"WWW-Authenticate": "Basic"},
    )

@app.post("/v1/delete-endpoint")
async def delete_endpoint(payload: DeletePayload, username: str = Depends(get_current_user)):
    """根据模型名和Session ID删除一个指定的端点，并在列表为空时移除模型条目"""
    global MODEL_ENDPOINT_MAP
    model_name = payload.model_name
    session_id_to_delete = payload.session_id

    # 检查模型是否存在，并且其值是一个列表
    # (兼容旧数据，值也可能是单个字典)
    if model_name in MODEL_ENDPOINT_MAP:
        endpoints = MODEL_ENDPOINT_MAP[model_name]
        original_len = 0
        
        # 统一处理，确保我们操作的是列表
        endpoint_list = []
        if isinstance(endpoints, list):
            endpoint_list = endpoints
        elif isinstance(endpoints, dict):
            endpoint_list = [endpoints]

        original_len = len(endpoint_list)

        # 新的过滤逻辑，能同时兼容 'sessionId' 和 'session_id' 两种键名
        filtered_endpoints = [
            ep for ep in endpoint_list 
            if ep.get('sessionId', ep.get('session_id')) != session_id_to_delete
        ]
        
        # 如果列表长度变短，说明删除成功
        if len(filtered_endpoints) < original_len:
            
            # 【【【 核心修复逻辑 】】】
            # 如果删除后列表为空，则从字典中移除整个模型条目
            if not filtered_endpoints:
                del MODEL_ENDPOINT_MAP[model_name]
                logger.info(f"模型 '{model_name}' 的最后一个端点已删除，该模型条目已移除。")
            else:
                MODEL_ENDPOINT_MAP[model_name] = filtered_endpoints
                logger.info(f"成功从模型 '{model_name}' 中删除了 Session ID 为 ...{session_id_to_delete[-6:]} 的端点。")
            
            return {"status": "success", "message": "Endpoint deleted."}

    # 如果上述条件都不满足，则说明未找到要删除的条目
    logger.warning(f"删除失败：未找到模型 '{model_name}' 或对应的 Session ID。")
    raise HTTPException(status_code=404, detail="Endpoint not found")
    
@app.get("/", response_class=HTMLResponse)
async def root():
    """提供一个简单的HTML状态页面"""
    ws_status = "✅ 已连接" if browser_ws and browser_ws.client_state.name == 'CONNECTED' else "❌ 未连接"
    
    # 计算已映射的模型数量和总ID数量
    mapped_models_count = len(MODEL_ENDPOINT_MAP)
    total_ids_count = 0
    for model, endpoints in MODEL_ENDPOINT_MAP.items():
        if isinstance(endpoints, list):
            total_ids_count += len(endpoints)
        elif isinstance(endpoints, dict):
            total_ids_count += 1
            
    html_content = f"""
    <!DOCTYPE html>
    <html lang="zh">
    <head>
        <meta charset="UTF-8"><title>LMArena Bridge Status</title>
        <style>
            body {{ display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background-color: #121212; color: #e0e0e0; font-family: sans-serif; }}
            .status-box {{ background-color: #1e1e1e; border: 1px solid #383838; border-radius: 10px; padding: 2em 3em; text-align: center; box-shadow: 0 4px 15px rgba(0,0,0,0.2); }}
            h1 {{ color: #76a9fa; margin-bottom: 1.5em; }}
            p {{ font-size: 1.2em; line-height: 1.8; }}
        </style>
    </head>
    <body>
        <div class="status-box">
            <h1>LMArena Bridge Status</h1>
            <p><strong>油猴脚本连接状态:</strong> {ws_status}</p>
            <p><strong>已映射模型种类数:</strong> {mapped_models_count}</p>
            <p><strong>已捕获ID总数:</strong> {total_ids_count}</p>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page(username: str = Depends(get_current_user)):
    """生成并返回一个美观的、带删除功能的HTML管理页面"""
    
    html_content = """
    <!DOCTYPE html>
    <html lang="zh">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>LMArena Bridge - ID 管理后台</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 2em; }
            h1, h2 { color: #76a9fa; border-bottom: 1px solid #333; padding-bottom: 10px; }
            .container { max-width: 1200px; margin: auto; }
            .model-group { background-color: #1e1e1e; border: 1px solid #383838; border-radius: 8px; margin-bottom: 2em; padding: 1em 1.5em; transition: opacity 0.5s, transform 0.5s; }
            .endpoint-entry { background-color: #2a2b32; border-left: 3px solid #4a90e2; padding: 1em; margin-top: 1em; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1em; }
            .endpoint-details { font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; font-size: 0.9em; word-break: break-all; line-height: 1.6; }
            .delete-btn { background-color: #da3633; color: white; border: none; padding: 8px 12px; border-radius: 6px; cursor: pointer; font-weight: bold; transition: background-color 0.2s; }
            .delete-btn:hover { background-color: #b92521; }
            .no-ids { font-style: italic; color: #888; padding: 1em; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>LMArena Bridge - ID 管理后台</h1>
    """

    if not MODEL_ENDPOINT_MAP:
        html_content += "<p class='no-ids'>当前没有已捕获的ID。</p>"
    else:
        for model_name, endpoints in sorted(MODEL_ENDPOINT_MAP.items()):
            html_content += f'<div class="model-group" data-model-group="{model_name}">'
            html_content += f'<h2>{model_name}</h2>'
            
            endpoint_list = []
            if isinstance(endpoints, list): endpoint_list = endpoints
            elif isinstance(endpoints, dict): endpoint_list = [endpoints]

            if not endpoint_list:
                html_content += "<p class='no-ids'>此模型下没有端点。</p>"
            else:
                for ep in endpoint_list:
                    if not isinstance(ep, dict): continue
                    session_id = ep.get('sessionId', ep.get('session_id', 'N/A'))
                    message_id = ep.get('messageId', ep.get('message_id', 'N/A'))
                    mode = ep.get('mode', 'N/A')
                    battle_target = ep.get('battle_target', '')
                    display_mode = f"{mode} (target: {battle_target})" if mode == 'battle' and battle_target else mode
                    html_content += f'''
                    <div class="endpoint-entry" id="entry-{session_id}">
                        <div class="endpoint-details">
                            <strong>Session ID:</strong> {session_id}<br>
                            <strong>Message ID:</strong> {message_id}<br>
                            <strong>Mode:</strong> {display_mode}
                        </div>
                        <button class="delete-btn" data-model="{model_name}" data-session="{session_id}">删除</button>
                    </div>
                    '''
            html_content += "</div>"

    html_content += """
        </div>
        <script>
            document.addEventListener('click', function(event) {
                if (event.target.classList.contains('delete-btn')) {
                    const button = event.target;
                    const modelName = button.dataset.model;
                    const sessionId = button.dataset.session;

                    if (confirm(`确定要删除模型 '${modelName}' 下的这个 Session ID 吗？\\n${sessionId}`)) {
                        fetch('/v1/delete-endpoint', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ modelName, sessionId })
                        })
                        .then(response => {
                            if (!response.ok) {
                                return response.json().then(err => { throw new Error(err.detail || '删除失败'); });
                            }
                            return response.json();
                        })
                        .then(data => {
                            const entryElement = document.getElementById(`entry-${sessionId}`);
                            if (entryElement) {
                                const parentGroup = entryElement.closest('.model-group');
                                // 移除ID条目
                                entryElement.style.transition = 'opacity 0.5s, transform 0.5s';
                                entryElement.style.opacity = '0';
                                entryElement.style.transform = 'translateX(-20px)';
                                setTimeout(() => {
                                    entryElement.remove();
                                    // 【【【 核心修复逻辑 】】】
                                    // 检查父分组是否还有其他ID条目
                                    if (parentGroup && !parentGroup.querySelector('.endpoint-entry')) {
                                        // 如果没有，也移除父分组
                                        parentGroup.style.transition = 'opacity 0.5s';
                                        parentGroup.style.opacity = '0';
                                        setTimeout(() => parentGroup.remove(), 500);
                                    }
                                }, 500);
                            }
                        })
                        .catch(error => { alert(`删除失败: ${error.message}`); });
                    }
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    api_port = int(os.environ.get("PORT", 7860))
    logger.info(f"🚀 LMArena Bridge API 服务器正在启动...")
    logger.info(f"   - 监听地址: http://0.0.0.0:{api_port}")
    uvicorn.run("api_server:app", host="0.0.0.0", port=api_port)
