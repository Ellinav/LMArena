import asyncio, json, logging, os, sys, re, threading, random, time
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import Optional, List

# --- 导入自定义模块 ---
from modules import image_generation
from modules import payload_converter

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
MAP_FILE_PATH = "/tmp/model_endpoint_map.json"

class EndpointUpdatePayload(BaseModel):
    model_name: str = Field(..., alias='modelName')
    session_id: str = Field(..., alias='sessionId')
    message_id: str = Field(..., alias='messageId')
    mode: str
    battle_target: Optional[str] = Field(None, alias='battleTarget')

def load_model_endpoint_map():
    global MODEL_ENDPOINT_MAP
    # 优先尝试从可写的 /tmp 目录加载上一次会话保存的最新状态
    try:
        with open(MAP_FILE_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content.strip():
                MODEL_ENDPOINT_MAP = {}
            else:
                MODEL_ENDPOINT_MAP = json.loads(content)
            logger.info(f"成功从临时文件 '{MAP_FILE_PATH}' 加载了 {len(MODEL_ENDPOINT_MAP)} 个端点映射。")
            return # 如果成功，直接返回
    except (FileNotFoundError, json.JSONDecodeError):
        # 如果在/tmp没找到文件，说明是冷启动，这是正常现象，继续往下走
        pass

    # 如果临时文件加载失败，则回退到从工作目录加载原始文件
    try:
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            content = f.read()
            if not content.strip():
                MODEL_ENDPOINT_MAP = {}
            else:
                MODEL_ENDPOINT_MAP = json.loads(content)
            logger.info(f"从原始文件 'model_endpoint_map.json' 加载了 {len(MODEL_ENDPOINT_MAP)} 个端点映射。")
    except (FileNotFoundError, json.JSONDecodeError):
        MODEL_ENDPOINT_MAP = {}

def save_model_endpoint_map():
    """将内存中的MODEL_ENDPOINT_MAP字典保存回json文件。"""
    try:
        # vvvvvv 修改这一行 vvvvvv
        with open(MAP_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(MODEL_ENDPOINT_MAP, f, indent=2, ensure_ascii=False)
        logger.info(f"✅ 成功将最新的ID地图保存到 {MAP_FILE_PATH}。") # (可选) 更新日志信息
    except Exception as e:
        logger.error(f"❌ 写入 {MAP_FILE_PATH} 文件时发生错误: {e}") # (可选) 更新日志信息
        
def load_config():
    global CONFIG
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
            json_content = re.sub(r'//.*|/\*[\s\S]*?\*/', '', content)
            CONFIG = json.loads(json_content)
    except (FileNotFoundError, json.JSONDecodeError):
        CONFIG = {}

def load_model_map():
    global MODEL_NAME_TO_ID_MAP
    try:
        with open('models.json', 'r', encoding='utf-8') as f:
            MODEL_NAME_TO_ID_MAP = json.load(f)
        logger.info(f"成功从 'models.json' 加载了 {len(MODEL_NAME_TO_ID_MAP)} 个模型。")
    except (FileNotFoundError, json.JSONDecodeError):
        MODEL_NAME_TO_ID_MAP = {}

def extract_models_from_html(html_content: str) -> Optional[List[dict]]:
    """
    智能解析HTML：遍历所有可能的Next.js数据块，直到找到包含 'initialModels' 的那一个。
    """
    # 使用 re.finditer 遍历所有匹配项，而不是只找第一个
    matches = re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html_content, re.DOTALL)
    
    for match in matches:
        payload = match.group(1)
        
        # 关键检查：如果这个数据块里不含 "initialModels"，就跳过，继续找下一个
        if 'initialModels' not in payload:
            continue

        # 找到了正确的数据块！现在对它进行解析
        try:
            cleaned_payload = payload.replace('\\"', '"').replace('\\\\', '\\')
            
            key = '"initialModels":['
            start_index = cleaned_payload.find(key)
            # 理论上不会失败，因为我们已经检查过 'initialModels' 的存在，但以防万一
            if start_index == -1: continue

            # 从数组开始的位置切片
            array_str = cleaned_payload[start_index + len(key) - 1:]
            
            # 通过匹配括号来精确地找到JSON数组的末尾
            open_brackets = 0
            for i, char in enumerate(array_str):
                if char == '[':
                    open_brackets += 1
                elif char == ']':
                    open_brackets -= 1
                
                # 当所有括号都闭合时，我们就找到了完整的数组
                if open_brackets == 0:
                    final_json_array_str = array_str[:i+1]
                    models = json.loads(final_json_array_str)
                    logger.info(f"✅ 成功从正确的HTML数据块中提取到 {len(models)} 个模型数据！")
                    return models # 成功！立即返回结果
            
            # 如果循环结束都没找到匹配的括号，说明这个数据块有问题
            logger.warning("找到了包含'initialModels'的数据块，但括号不匹配。将检查下一个。")
            continue

        except Exception as e:
            logger.warning(f"找到了一个候选数据块，但解析失败: {e}。将检查下一个。")
            continue
            
    # 如果遍历完所有匹配项都没有成功返回，说明真的找不到了
    logger.error("错误：遍历了所有数据块，但均未找到或未能成功解析 'initialModels'。")
    return None

async def compare_and_update_models(new_models_list: List[dict]):
    """
    (异步版) 将模型列表与内存中的版本进行比较，直接在内存中更新，
    并在更新后通过 WebSocket 主动推送给客户端。
    """
    global MODEL_NAME_TO_ID_MAP, browser_ws

    old_models = MODEL_NAME_TO_ID_MAP.copy()
    new_models_dict = {model['publicName']: model.get('id') for model in new_models_list if 'publicName' in model and 'id' in model}
    
    old_models_set = set(old_models.keys())
    new_models_set = set(new_models_dict.keys())
    added_models = new_models_set - old_models_set
    removed_models = old_models_set - new_models_set
    
    logger.info("---[ 模型列表更新检查 (内存模式) ]---")
    has_changes = False

    if added_models:
        has_changes = True
        logger.info("\n[+] 新增模型:")
        for name in sorted(list(added_models)):
            logger.info(f"  - {name} (ID: {new_models_dict.get(name)})")

    if removed_models:
        has_changes = True
        logger.info("\n[-] 已移除模型:")
        for name in sorted(list(removed_models)):
            logger.info(f"  - {name} (原ID: {old_models.get(name)})")

    logger.info("\n[*] 存量模型ID检查:")
    changed_id_models = 0
    for name in sorted(list(new_models_set.intersection(old_models_set))):
        new_id = new_models_dict.get(name)
        old_id = old_models.get(name)
        if new_id != old_id:
            has_changes = True
            changed_id_models += 1
            logger.info(f"  - ID 变更: '{name}' | 旧ID: {old_id} -> 新ID: {new_id}")
    
    if changed_id_models == 0: logger.info("  - 所有存量模型的ID均无变化。")
    
    if not has_changes:
        logger.info("\n[结论] 模型列表与内存版本一致，无需更新。")
        logger.info("---[ 检查完毕 ]---")
        return

    logger.info("\n[结论] 检测到模型列表变更，正在更新内存...")
    
    MODEL_NAME_TO_ID_MAP = new_models_dict
    logger.info(f"✅ 内存中的模型列表已成功更新，当前包含 {len(MODEL_NAME_TO_ID_MAP)} 个模型。")

    if browser_ws and browser_ws.client_state.name == 'CONNECTED':
        try:
            new_model_names = sorted(list(new_models_dict.keys()))
            message_to_send = {
                "command": "update_model_list",
                "data": new_model_names
            }
            await browser_ws.send_text(json.dumps(message_to_send))
            logger.info("✅ 已通过 WebSocket 主动向客户端推送更新后的模型列表。")
        except Exception as e:
            logger.warning(f"⚠️ 推送模型列表更新到客户端失败: {e}")
            
    logger.info("---[ 检查与更新完毕 ]---")

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
    payload_converter.initialize_converter(response_channels)
    load_config()
    load_model_map()
    load_model_endpoint_map()
    logger.info("服务器启动完成。等待油猴脚本连接...")
    asyncio.create_task(send_pings())
    last_activity_time = datetime.now()
    if CONFIG.get("enable_idle_restart", False):
        idle_monitor_thread = threading.Thread(target=idle_monitor, daemon=True)
        idle_monitor_thread.start()
    image_generation.initialize_image_module(logger, response_channels, CONFIG, MODEL_NAME_TO_ID_MAP, DEFAULT_MODEL_ID)
    yield
    logger.info("服务器正在关闭。")

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.post("/update_models")
async def update_models_endpoint(request: Request):
    html_content_bytes = await request.body()
    if not html_content_bytes:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No HTML content received."})
    
    logger.info("收到来自油猴脚本的模型列表更新通知...")
    new_models_list = extract_models_from_html(html_content_bytes.decode('utf-8'))
    
    if new_models_list:
        # 只在后端默默地比较和更新，不再需要推送
        # (compare_and_update_models 函数可以变回非 async 的普通 def 函数)
        await compare_and_update_models(new_models_list) 
        return JSONResponse(status_code=200, content={"status": "success", "message": "Server memory updated."})
    else:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Could not extract model data from HTML."})
        
@app.post("/v1/add-or-update-endpoint")
async def add_or_update_endpoint(payload: EndpointUpdatePayload):
    global MODEL_ENDPOINT_MAP
    new_entry = payload.dict(exclude_none=True, by_alias=True)
    model_name = new_entry.pop("modelName")

    # 如果模型是第一次出现，创建一个新列表
    if model_name not in MODEL_ENDPOINT_MAP:
        MODEL_ENDPOINT_MAP[model_name] = [new_entry]
        logger.info(f"成功为新模型 '{model_name}' 创建了新的端点映射列表。")
        save_model_endpoint_map()  # 保存更改
        return {"status": "success", "message": f"Endpoint for {model_name} created."}

    # 如果模型已存在且其值是列表
    if isinstance(MODEL_ENDPOINT_MAP.get(model_name), list):
        endpoints = MODEL_ENDPOINT_MAP[model_name]
        new_session_id = new_entry.get('sessionId')
        
        # 检查重复
        is_duplicate = any(ep.get('sessionId') == new_session_id for ep in endpoints)
        
        if not is_duplicate:
            endpoints.append(new_entry)
            logger.info(f"成功为模型 '{model_name}' 追加了一个新的端点映射。")
            save_model_endpoint_map()  # 保存更改
            return {"status": "success", "message": f"New endpoint for {model_name} appended."}
        else:
            logger.info(f"检测到重复的 Session ID，已为模型 '{model_name}' 忽略本次添加。")
            return {"status": "skipped", "message": "Duplicate endpoint ignored."}
            
    # 如果数据结构不正确，记录错误
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
        if not isinstance(new_map, dict): 
            raise HTTPException(status_code=400, detail="Request body must be a valid JSON object.")
        
        MODEL_ENDPOINT_MAP = new_map
        logger.info(f"✅ 成功从API导入了 {len(MODEL_ENDPOINT_MAP)} 个模型端点映射！")
        
        save_model_endpoint_map() # <-- 【【【核心修正】】】 在导入后立刻保存到临时文件
        
        return {"status": "success", "message": f"Map imported with {len(MODEL_ENDPOINT_MAP)} entries."}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON in request body.")
  
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

@app.get("/v1/models")
async def get_models():
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
                "created": int(time.time()), 
                "owned_by": "LMArenaBridge"
            }
            for model_name in MODEL_NAME_TO_ID_MAP.keys()
        ],
    }

@app.get("/v1/get-model-list")
async def get_model_list():
    if not MODEL_NAME_TO_ID_MAP:
        raise HTTPException(
            status_code=404,
            detail="服务器端模型列表为空。请确保 'models.json' 文件存在且内容正确。"
        )
    model_names = sorted(list(MODEL_NAME_TO_ID_MAP.keys()))
    return JSONResponse(content=model_names)

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    global last_activity_time
    last_activity_time = datetime.now()

    load_config()
    api_key = os.environ.get("API_KEY") or CONFIG.get("api_key")
    if api_key:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise HTTPException(status_code=401, detail="未提供 API Key。")
        if auth_header.split(' ')[1] != api_key:
            raise HTTPException(status_code=401, detail="提供的 API Key 不正确。")

    if not browser_ws:
        raise HTTPException(status_code=503, detail="油猴脚本客户端未连接。")

    try:
        openai_req = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

    model_name = openai_req.get("model")
    session_id, message_id, mode_override, battle_target_override = None, None, None, None

    if model_name and model_name in MODEL_ENDPOINT_MAP:
        mapping_entry = MODEL_ENDPOINT_MAP[model_name]
        selected_mapping = random.choice(mapping_entry) if isinstance(mapping_entry, list) and mapping_entry else mapping_entry if isinstance(mapping_entry, dict) else None
        
        if selected_mapping:
            session_id = selected_mapping.get("session_id") or selected_mapping.get("sessionId")
            message_id = selected_mapping.get("message_id") or selected_mapping.get("messageId")
            mode_override = selected_mapping.get("mode")
            battle_target_override = selected_mapping.get("battle_target")

    if not session_id and CONFIG.get("use_default_ids_if_mapping_not_found", True):
        session_id = CONFIG.get("session_id")
        message_id = CONFIG.get("message_id")
        mode_override, battle_target_override = None, None

    if not session_id or not message_id or "YOUR_" in session_id or "YOUR_" in message_id:
        raise HTTPException(status_code=400, detail="会话ID或消息ID无效。")

    if not model_name or model_name not in MODEL_NAME_TO_ID_MAP:
        logger.warning(f"请求的模型 '{model_name}' 不在 models.json 中，将使用默认模型ID。")

    request_id = str(uuid.uuid4())
    response_channels[request_id] = asyncio.Queue()

    try:
        from modules.payload_converter import convert_openai_to_lmarena_payload, stream_generator, non_stream_response
        lmarena_payload = convert_openai_to_lmarena_payload(
            openai_req, session_id, message_id, MODEL_NAME_TO_ID_MAP, DEFAULT_MODEL_ID,
            mode_override=mode_override, battle_target_override=battle_target_override
        )
        
        message_to_browser = {"request_id": request_id, "payload": lmarena_payload}
        await browser_ws.send_text(json.dumps(message_to_browser))

        is_stream = openai_req.get("stream", True)
        if is_stream:
            return StreamingResponse(stream_generator(request_id, model_name or "default_model"), media_type="text/event-stream")
        else:
            return await non_stream_response(request_id, model_name or "default_model")
    except Exception as e:
        if request_id in response_channels: del response_channels[request_id]
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/images/generations")
async def images_generations(request: Request):
    global last_activity_time
    last_activity_time = datetime.now()
    response_data, status_code = await image_generation.handle_image_generation_request(request, browser_ws)
    return JSONResponse(content=response_data, status_code=status_code)

@app.post("/internal/start_id_capture")
async def start_id_capture():
    if not browser_ws:
        raise HTTPException(status_code=503, detail="Browser client not connected.")
    try:
        await browser_ws.send_text(json.dumps({"command": "activate_id_capture"}))
        return JSONResponse({"status": "success", "message": "Activation command sent."})
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to send command via WebSocket.")

security = HTTPBearer()
class DeletePayload(BaseModel):
    model_name: str = Field(..., alias='modelName')
    session_id: str = Field(..., alias='sessionId')

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    server_api_key = os.environ.get("API_KEY") or CONFIG.get("api_key")
    # 访问 .credentials 属性是正确的，因为 security 是 HTTPBearer()
    if server_api_key and credentials.credentials == server_api_key:
        return "admin"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect API Key",
        headers={"WWW-authenticate": "Bearer"},
    )

@app.post("/v1/delete-endpoint")
async def delete_endpoint(payload: DeletePayload, current_user: str = Depends(get_current_user)):
    global MODEL_ENDPOINT_MAP
    
    model_name_from_client = payload.model_name.strip()
    session_id_to_delete = payload.session_id.strip()

    logger.info(f"收到删除请求: 模型='{model_name_from_client}', SessionID='{session_id_to_delete}'")

    found_model_key = None
    for key in MODEL_ENDPOINT_MAP.keys():
        if key.strip() == model_name_from_client:
            found_model_key = key
            break
            
    if not found_model_key:
        logger.error(f"删除失败：无法在 MODEL_ENDPOINT_MAP 中找到匹配的模型 '{model_name_from_client}'。")
        raise HTTPException(status_code=404, detail="Endpoint not found: Model name does not match.")

    # --- 【【【核心修复逻辑】】】 ---
    entry = MODEL_ENDPOINT_MAP[found_model_key]
    
    # 情况一：值是单个字典
    if isinstance(entry, dict):
        # 检查这个字典的 session_id 是否匹配
        current_session_id = entry.get('sessionId', entry.get('session_id', '')).strip()
        if current_session_id == session_id_to_delete:
            # 匹配成功，直接删除整个模型条目
            del MODEL_ENDPOINT_MAP[found_model_key]
            logger.info(f"成功删除模型 '{found_model_key}' 的单个字典条目 (SessionID: {session_id_to_delete})。")
            save_model_endpoint_map()
            return {"status": "success", "message": "Endpoint (single entry) deleted."}
        else:
            # 不匹配
            logger.warning(f"在模型 '{found_model_key}' 的单个条目中未找到匹配的 SessionID: '{session_id_to_delete}'。")

    # 情况二：值是一个列表
    elif isinstance(entry, list):
        original_len = len(entry)
        new_endpoints = [
            ep for ep in entry 
            if ep.get('sessionId', ep.get('session_id', '')).strip() != session_id_to_delete
        ]
        
        if len(new_endpoints) < original_len:
            logger.info(f"成功在模型 '{found_model_key}' 的列表中移除了 SessionID: {session_id_to_delete}")
            
            if not new_endpoints:
                del MODEL_ENDPOINT_MAP[found_model_key]
                logger.info(f"模型 '{found_model_key}' 的端点列表已空，已将其从映射中移除。")
            else:
                MODEL_ENDPOINT_MAP[found_model_key] = new_endpoints
            save_model_endpoint_map()    
            return {"status": "success", "message": "Endpoint (from list) deleted."}
        else:
            logger.warning(f"在模型 '{found_model_key}' 的列表中未找到要删除的 SessionID: '{session_id_to_delete}'。")

    # 如果代码执行到这里，说明找到了模型，但 SessionID 不匹配
    raise HTTPException(status_code=404, detail="Endpoint not found: Session ID does not match.")

@app.get("/", response_class=HTMLResponse)
async def root():
    ws_status = "✅ 已连接" if browser_ws and browser_ws.client_state.name == 'CONNECTED' else "❌ 未连接"
    mapped_models_count = len(MODEL_ENDPOINT_MAP)
    total_ids_count = sum(len(v) if isinstance(v, list) else 1 for v in MODEL_ENDPOINT_MAP.values())
    return HTMLResponse(content=f"""
    <!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><title>LMArena Bridge Status</title>
    <style>body{{display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background-color:#121212;color:#e0e0e0;font-family:sans-serif;}}.status-box{{background-color:#1e1e1e;border:1px solid #383838;border-radius:10px;padding:2em 3em;text-align:center;box-shadow:0 4px 15px rgba(0,0,0,0.2);}}h1{{color:#76a9fa;margin-bottom:1.5em;}}p{{font-size:1.2em;line-height:1.8;}}</style>
    </head><body><div class="status-box"><h1>LMArena Bridge Status</h1><p><strong>油猴脚本连接状态:</strong> {ws_status}</p><p><strong>已映射模型种类数:</strong> {mapped_models_count}</p><p><strong>已捕获ID总数:</strong> {total_ids_count}</p></div></body></html>
    """)

@app.get("/v1/get-endpoint-map")
async def get_endpoint_map_data(current_user: str = Depends(get_current_user)):
    # 这个接口受保护，必须提供正确的 Bearer Token
    return JSONResponse(content=MODEL_ENDPOINT_MAP)

@app.get("/admin/login", response_class=HTMLResponse)
async def get_admin_login_page():
    # 这个端点只返回一个简单的登录页面，不需要任何认证
    return HTMLResponse(content="""
    <!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
    <title>Admin Login</title>
    <style>
        body { display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background-color: #121212; color: #e0e0e0; font-family: sans-serif; }
        .auth-box { background: #1e1e1e; padding: 2em 3em; border-radius: 8px; box-shadow: 0 5px 20px rgba(0,0,0,0.5); text-align: center; }
        h2 { color: #76a9fa; }
        input { padding: 10px; margin: 15px 0; width: 280px; background: #333; border: 1px solid #555; border-radius: 4px; color: #fff; }
        button { width: 100%; padding: 10px 20px; background: #76a9fa; color: #121212; border: none; border-radius: 4px; font-weight: bold; cursor: pointer; }
    </style>
    </head><body>
    <div class="auth-box">
        <h2>Admin后台认证</h2>
        <p>请输入您的 API Key。</p>
        <input type="password" id="api-key-input" placeholder="API Key">
        <button onclick="login()">进入</button>
    </div>
    <script>
        function login() {
            const apiKey = document.getElementById('api-key-input').value;
            if (apiKey) {
                // 将 API Key 存到 localStorage，然后跳转到真正的 admin 页面
                localStorage.setItem('adminApiKey', apiKey);
                window.location.href = '/admin';
            } else {
                alert('请输入 API Key！');
            }
        }
        document.getElementById('api-key-input').addEventListener('keyup', (e) => {
            if (e.key === 'Enter') login();
        });
    </script>
    </body></html>
    """)

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page():
    # 最终版：基于能正常工作的极简版，安全地添加了导入、导出和删除功能
    html_content = """
    <!DOCTYPE html>
    <html lang="zh">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>LMArena Bridge - ID 管理后台</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 2em; }
            .container { max-width: 1200px; margin: auto; }
            .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1em; flex-wrap: wrap; gap: 1em;}
            h1 { color: #76a9fa; margin: 0;}
            .button-group { display: flex; gap: 10px; }
            .admin-btn { border: none; padding: 10px 15px; border-radius: 6px; cursor: pointer; font-weight: bold; transition: background-color 0.2s; color: white; }
            #export-btn { background-color: #388e3c; }
            #export-btn:hover { background-color: #2e7d32; }
            #import-btn { background-color: #1976d2; }
            #import-btn:hover { background-color: #115293; }
            .admin-btn:disabled { background-color: #555; cursor: not-allowed; opacity: 0.7; }
            .model-group { background-color: #1e1e1e; border: 1px solid #383838; border-radius: 8px; margin-bottom: 2em; padding: 1.5em; overflow: hidden; }
            h2 { border-bottom: 1px solid #333; padding-bottom: 10px; margin-top:0; }
            .endpoint-entry { background-color: #2a2b32; border-left: 4px solid #4a90e2; padding: 1em; margin-top: 1em; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1em; }
            .endpoint-details { font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; font-size: 0.9em; word-break: break-all; line-height: 1.6; }
            .delete-btn { background-color: #da3633; color: white; border: none; padding: 8px 12px; border-radius: 6px; cursor: pointer; font-weight: bold; }
            .delete-btn:hover { background-color: #b92521; }
            #loading-state, #empty-state, #error-state { text-align: center; margin-top: 3em; color: #888; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>LMArena Bridge - ID 管理后台</h1>
                <div class="button-group">
                    <button id="import-btn" class="admin-btn">导入JSON</button>
                    <button id="export-btn" class="admin-btn" disabled>导出JSON</button>
                    <input type="file" id="import-file-input" accept=".json" style="display: none;">
                </div>
            </div>
            <div id="data-container">
                <div id="loading-state"><h2>🔄 正在加载数据...</h2></div>
            </div>
        </div>

        <script>
            document.addEventListener('DOMContentLoaded', function() {
                
                let modelEndpointMapData = null;
                const exportButton = document.getElementById('export-btn');
                const importButton = document.getElementById('import-btn');
                const importFileInput = document.getElementById('import-file-input');
                const dataContainer = document.getElementById('data-container');
                const apiKey = localStorage.getItem('adminApiKey');

                // --- 导出功能 ---
                exportButton.addEventListener('click', function() {
                    if (!modelEndpointMapData || Object.keys(modelEndpointMapData).length === 0) {
                        alert('没有数据可导出！'); return;
                    }
                    const dataStr = JSON.stringify(modelEndpointMapData, null, 2);
                    const dataBlob = new Blob([dataStr], { type: 'application/json;charset=utf-8' });
                    const url = URL.createObjectURL(dataBlob);
                    const a = document.createElement('a');
                    const date = new Date().toISOString().slice(0, 10);
                    a.href = url;
                    a.download = `model_endpoint_map_${date}.json`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                });

                // --- 导入功能 ---
                importButton.addEventListener('click', () => importFileInput.click());
                importFileInput.addEventListener('change', (event) => {
                    const file = event.target.files[0];
                    if (!file) return;
                    if (!apiKey) { alert('认证信息丢失，请重新登录。'); window.location.href = '/admin/login'; return; }
                    if (!confirm(`确定要用文件 '${file.name}' 的内容覆盖服务器上所有的ID吗？此操作不可逆！`)) {
                        importFileInput.value = ''; return;
                    }
                    const reader = new FileReader();
                    reader.onload = async (e) => {
                        try {
                            const content = e.target.result;
                            JSON.parse(content);
                            const response = await fetch('/v1/import-map', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${apiKey}` },
                                body: content
                            });
                            if (!response.ok) {
                                if (response.status === 401) { alert('认证失败，请重新登录。'); window.location.href = '/admin/login'; return; }
                                const err = await response.json();
                                throw new Error(err.detail || '服务器返回未知错误。');
                            }
                            alert('✅ 导入成功！页面将刷新以显示最新数据。');
                            location.reload();
                        } catch (error) {
                            alert(`❌ 导入失败: ${error.message}`);
                        } finally {
                            importFileInput.value = '';
                        }
                    };
                    reader.readAsText(file);
                });

                // --- 删除功能 (事件委托) ---
                dataContainer.addEventListener('click', async function(event) {
                    if (event.target.classList.contains('delete-btn')) {
                        if (!apiKey) { alert('认证信息丢失，请重新登录。'); window.location.href = '/admin/login'; return; }
                        const button = event.target;
                        const modelName = button.dataset.model;
                        const sessionId = button.dataset.session;
                        if (confirm(`确定要删除模型 '${modelName}' 下的这个 Session ID 吗？\\n${sessionId}`)) {
                            try {
                                const response = await fetch('/v1/delete-endpoint', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${apiKey}` },
                                    body: JSON.stringify({ modelName, sessionId })
                                });
                                if (!response.ok) {
                                    if (response.status === 401) { alert('认证失败，请重新登录。'); window.location.href = '/admin/login'; return; }
                                    const err = await response.json();
                                    throw new Error(err.detail || '服务器返回未知错误。');
                                }
                                const entryElement = document.getElementById(`entry-${sessionId}`);
                                if (entryElement) {
                                    const modelGroup = entryElement.closest('.model-group');
                                    entryElement.remove();
                                    if (modelGroup && !modelGroup.querySelector('.endpoint-entry')) {
                                        modelGroup.remove();
                                    }
                                    if (document.querySelectorAll('.model-group').length === 0) {
                                        renderData({});
                                    }
                                }
                            } catch (error) {
                                alert(`删除失败: ${error.message}`);
                            }
                        }
                    }
                });

                // --- 渲染函数 ---
                function renderData(data) {
                    modelEndpointMapData = data;
                    if (Object.keys(data).length === 0) {
                        dataContainer.innerHTML = '<div id="empty-state"><h2>当前没有已捕获的ID。</h2></div>';
                        exportButton.disabled = true;
                        return;
                    }
                    exportButton.disabled = false;
                    let html = '';
                    const sortedModelNames = Object.keys(data).sort();
                    for (const modelName of sortedModelNames) {
                        html += '<div class="model-group" id="group-for-' + modelName.replace(/[^a-zA-Z0-9]/g, '-') + '"><h2>' + modelName + '</h2>';
                        const endpoints = Array.isArray(data[modelName]) ? data[modelName] : [data[modelName]];
                        for (const ep of endpoints) {
                            const sessionId = ep.sessionId || ep.session_id || 'N/A';
                            const messageId = ep.messageId || ep.message_id || 'N/A';
                            const mode = ep.mode || 'N/A';
                            const battleTarget = ep.battle_target;
                            const displayMode = mode === 'battle' && battleTarget ? `battle (target: ${battleTarget})` : mode;
                            html += '<div class="endpoint-entry" id="entry-' + sessionId + '">' +
                                        '<div class="endpoint-details">' +
                                            '<strong>Session ID:</strong> ' + sessionId + '<br>' +
                                            '<strong>Message ID:</strong> ' + messageId + '<br>' +
                                            '<strong>Mode:</strong> ' + displayMode +
                                        '</div>' +
                                        '<button class="delete-btn" data-model="' + modelName + '" data-session="' + sessionId + '">删除</button>' +
                                    '</div>';
                        }
                        html += '</div>';
                    }
                    dataContainer.innerHTML = html;
                }
                
                // --- 启动函数：页面加载时获取初始数据 ---
                async function initialLoad() {
                    if (!apiKey) {
                        window.location.href = '/admin/login';
                        return;
                    }
                    try {
                        const response = await fetch('/v1/get-endpoint-map', { headers: { 'Authorization': `Bearer ${apiKey}` } });
                        if (!response.ok) {
                             if (response.status === 401) { alert('认证失败，请重新登录。'); localStorage.removeItem('adminApiKey'); window.location.href = '/admin/login'; return; }
                            throw new Error('获取数据失败，服务器状态: ' + response.status);
                        }
                        const data = await response.json();
                        renderData(data);
                    } catch (error) {
                        dataContainer.innerHTML = `<div id="error-state"><h2>❌ 加载数据失败</h2><p>${error.toString()}</p></div>`;
                        exportButton.disabled = true;
                    }
                }

                initialLoad(); // 执行！
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    # 确保在运行前，存在 modules/payload_converter.py 文件
    if not os.path.exists("modules/payload_converter.py"):
        logger.error("错误: 缺少 'modules/payload_converter.py' 文件。请确保该文件存在。")
        sys.exit(1)
        
    api_port = int(os.environ.get("PORT", 7860))
    logger.info(f"🚀 LMArena Bridge API 服务器正在启动...")
    logger.info(f"   - 监听地址: http://0.0.0.0:{api_port}")
    uvicorn.run("api_server:app", host="0.0.0.0", port=api_port)
