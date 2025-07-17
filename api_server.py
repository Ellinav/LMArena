import asyncio, json, logging, os, sys, subprocess, time, uuid, re, threading, random, mimetypes
from datetime import datetime
from contextlib import asynccontextmanager
import uvicorn
import requests
from packaging.version import parse as parse_version
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
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
WARNED_UNKNOWN_IDS = set()
main_event_loop = None
MODEL_NAME_TO_ID_MAP = {}
MODEL_ENDPOINT_MAP = {}
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

# --- 模型更新 ---
def extract_models_from_html(html_content):
    """
    从 HTML 内容中提取模型数据，采用更健壮的解析方法。
    """
    script_contents = re.findall(r'<script>(.*?)</script>', html_content, re.DOTALL)
    
    for script_content in script_contents:
        if 'self.__next_f.push' in script_content and 'initialState' in script_content and 'publicName' in script_content:
            match = re.search(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', script_content, re.DOTALL)
            if not match:
                continue
            
            full_payload = match.group(1)
            
            payload_string = full_payload.split('\\n')[0]
            
            json_start_index = payload_string.find(':')
            if json_start_index == -1:
                continue
            
            json_string_with_escapes = payload_string[json_start_index + 1:]
            json_string = json_string_with_escapes.replace('\\"', '"')
            
            try:
                data = json.loads(json_string)
                
                def find_initial_state(obj):
                    if isinstance(obj, dict):
                        for key, value in obj.items():
                            if key == 'initialState' and isinstance(value, list):
                                if value and isinstance(value[0], dict) and 'publicName' in value[0]:
                                    return value
                            result = find_initial_state(value)
                            if result is not None:
                                return result
                    elif isinstance(obj, list):
                        for item in obj:
                            result = find_initial_state(item)
                            if result is not None:
                                return result
                    return None

                models = find_initial_state(data)
                if models:
                    logger.info(f"成功从脚本块中提取到 {len(models)} 个模型。")
                    return models
            except json.JSONDecodeError as e:
                logger.error(f"解析提取的JSON字符串时出错: {e}")
                continue

    logger.error("错误：在HTML响应中找不到包含有效模型数据的脚本块。")
    return None

def compare_and_update_models(new_models_list, models_path):
    """
    比较新旧模型列表，打印差异，并用新列表更新本地 models.json 文件。
    """
    try:
        with open(models_path, 'r', encoding='utf-8') as f:
            old_models = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        old_models = {}

    new_models_dict = {model['publicName']: model for model in new_models_list if 'publicName' in model}
    old_models_set = set(old_models.keys())
    new_models_set = set(new_models_dict.keys())

    added_models = new_models_set - old_models_set
    removed_models = old_models_set - new_models_set
    
    logger.info("--- 模型列表更新检查 ---")
    has_changes = False

    if added_models:
        has_changes = True
        logger.info("\n[+] 新增模型:")
        for name in sorted(list(added_models)):
            model = new_models_dict[name]
            logger.info(f"  - 名称: {name}, ID: {model.get('id')}, 组织: {model.get('organization', 'N/A')}")

    if removed_models:
        has_changes = True
        logger.info("\n[-] 删除模型:")
        for name in sorted(list(removed_models)):
            logger.info(f"  - 名称: {name}, ID: {old_models.get(name)}")

    logger.info("\n[*] 共同模型检查:")
    changed_models = 0
    for name in sorted(list(new_models_set.intersection(old_models_set))):
        new_id = new_models_dict[name].get('id')
        old_id = old_models.get(name)
        if new_id != old_id:
            has_changes = True
            changed_models += 1
            logger.info(f"  - ID 变更: '{name}' 旧ID: {old_id} -> 新ID: {new_id}")
    
    if changed_models == 0:
        logger.info("  - 共同模型的ID无变化。")

    if not has_changes:
        logger.info("\n结论: 模型列表无任何变化，无需更新文件。")
        logger.info("--- 检查完毕 ---")
        return

    logger.info("\n结论: 检测到模型变更，正在更新 'models.json'...")
    updated_model_map = {model['publicName']: model.get('id') for model in new_models_list if 'publicName' in model and 'id' in model}
    try:
        with open(models_path, 'w', encoding='utf-8') as f:
            json.dump(updated_model_map, f, indent=4, ensure_ascii=False)
        logger.info(f"'{models_path}' 已成功更新，包含 {len(updated_model_map)} 个模型。")
        load_model_map()
    except IOError as e:
        logger.error(f"写入 '{models_path}' 文件时出错: {e}")
    
    logger.info("--- 检查与更新完毕 ---")

def restart_server():
    logger.warning("="*60)
    logger.warning("检测到服务器空闲超时，准备自动重启...")
    async def notify_browser_refresh():
        if browser_ws:
            try:
                await browser_ws.send_text(json.dumps({"command": "reconnect"}))
                logger.info("已向浏览器发送 'reconnect' 指令。")
            except Exception as e:
                logger.error(f"发送 'reconnect' 指令失败: {e}")
    if browser_ws and browser_ws.client_state.name == 'CONNECTED' and main_event_loop:
        asyncio.run_coroutine_threadsafe(notify_browser_refresh(), main_event_loop)
    time.sleep(3)
    logger.info("正在重启服务器...")
    os.execv(sys.executable, ['python'] + sys.argv)

def idle_monitor():
    global last_activity_time
    while last_activity_time is None: time.sleep(1)
    logger.info("空闲监控线程已启动。")
    while True:
        if CONFIG.get("enable_idle_restart", False):
            timeout = CONFIG.get("idle_restart_timeout_seconds", 300)
            if timeout == -1:
                time.sleep(10)
                continue
            if (datetime.now() - last_activity_time).total_seconds() > timeout:
                restart_server()
                break
        time.sleep(10)

async def send_pings():
    while True:
        await asyncio.sleep(30)
        if browser_ws:
            try:
                await browser_ws.send_text(json.dumps({"command": "ping"}))
                logger.debug("Ping sent.")
            except Exception:
                logger.debug("Ping发送失败，连接可能已关闭。")

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
    
    # 核心逻辑：在内存中更新ID池
    model_name = new_entry.pop("modelName")
    
    if model_name in MODEL_ENDPOINT_MAP and isinstance(MODEL_ENDPOINT_MAP[model_name], list):
        MODEL_ENDPOINT_MAP[model_name].append(new_entry)
        logger.info(f"成功为模型 '{model_name}' 追加了一个新的端点映射。")
    else:
        MODEL_ENDPOINT_MAP[model_name] = [new_entry]
        logger.info(f"成功为模型 '{model_name}' 创建了新的端点映射列表。")
    
    # 注意：写入文件的代码块已被移除，以避免权限错误
    
    return {"status": "success", "message": f"Endpoint for {model_name} updated."}
    
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
        return JSONResponse({"status": "success", "message": "Model comparison and update complete."})
    else:
        logger.error("未能从油猴脚本提供的 HTML 中提取模型数据。")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Could not extract model data from HTML."}
        )

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
                    logger.warning(f"⚠️ 收到未知或已关闭请求的响应: {request_id}。")
                    WARNED_UNKNOWN_IDS.add(request_id)
    except WebSocketDisconnect:
        logger.warning("❌ 油猴脚本客户端已断开连接。")
        if response_channels:
            logger.warning(f"WebSocket 连接断开！正在清理 {len(response_channels)} 个待处理的请求通道...")
    except Exception as e:
        logger.error(f"WebSocket 处理时发生未知错误: {e}", exc_info=True)
    finally:
        browser_ws = None
        for queue in response_channels.values():
            await queue.put({"error": "Browser disconnected during operation"})
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
    """根据模型名和Session ID删除一个指定的端点"""
    global MODEL_ENDPOINT_MAP
    model_name = payload.model_name
    session_id = payload.session_id

    if model_name in MODEL_ENDPOINT_MAP and isinstance(MODEL_ENDPOINT_MAP[model_name], list):
        endpoints = MODEL_ENDPOINT_MAP[model_name]
        # 找到并移除匹配的端点
        original_len = len(endpoints)
        MODEL_ENDPOINT_MAP[model_name] = [ep for ep in endpoints if ep.get('sessionId') != session_id]
        
        if len(MODEL_ENDPOINT_MAP[model_name]) < original_len:
            # 如果列表变空，则从字典中移除该模型
            if not MODEL_ENDPOINT_MAP[model_name]:
                del MODEL_ENDPOINT_MAP[model_name]
            logger.info(f"成功从模型 '{model_name}' 中删除了 Session ID 为 ...{session_id[-6:]} 的端点。")
            return {"status": "success", "message": "Endpoint deleted."}

    logger.warning(f"删除失败：未找到模型 '{model_name}' 或对应的 Session ID。")
    raise HTTPException(status_code=404, detail="Endpoint not found")


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
            body { font-family: sans-serif; background-color: #1a1a1a; color: #e0e0e0; margin: 0; padding: 2em; }
            h1, h2 { color: #4a90e2; border-bottom: 2px solid #333; padding-bottom: 10px; }
            .container { max-width: 1200px; margin: auto; }
            .model-group { background-color: #2a2b32; border: 1px solid #444; border-radius: 8px; margin-bottom: 2em; padding: 1.5em; }
            .endpoint-entry { background-color: #333; border: 1px solid #555; border-radius: 6px; padding: 1em; margin-top: 1em; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
            .endpoint-details { font-family: 'Courier New', Courier, monospace; font-size: 0.9em; word-break: break-all; }
            .delete-btn { background-color: #e2584a; color: white; border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer; font-weight: bold; }
            .delete-btn:hover { background-color: #c94f42; }
            .no-ids { font-style: italic; color: #888; }
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
            html_content += f'<div class="model-group"><h2>{model_name}</h2>'
            if not endpoints:
                html_content += "<p class='no-ids'>此模型下没有端点。</p>"
            else:
                for ep in endpoints:
                    session_id = ep.get('sessionId', 'N/A')
                    message_id = ep.get('messageId', 'N/A')
                    mode = ep.get('mode', 'N/A')
                    battle_target = ep.get('battle_target', '')
                    display_mode = f"{mode} ({battle_target})" if mode == 'battle' else mode

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
                            headers: {
                                'Content-Type': 'application/json',
                                // 浏览器会自动为同一站点的后续请求附加Basic Auth凭据
                            },
                            body: JSON.stringify({ modelName, sessionId })
                        })
                        .then(response => {
                            if (!response.ok) {
                                return response.json().then(err => { throw new Error(err.detail || '删除失败'); });
                            }
                            return response.json();
                        })
                        .then(data => {
                            console.log('删除成功:', data);
                            const entryElement = document.getElementById(`entry-${sessionId}`);
                            if (entryElement) {
                                entryElement.style.transition = 'opacity 0.5s';
                                entryElement.style.opacity = '0';
                                setTimeout(() => entryElement.remove(), 500);
                            }
                        })
                        .catch(error => {
                            console.error('删除时出错:', error);
                            alert(`删除失败: ${error.message}`);
                        });
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
