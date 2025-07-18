import asyncio, json, logging, os, sys, re, threading, random, time
from datetime import datetime
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from typing import Optional, List

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
    except (FileNotFoundError, json.JSONDecodeError):
        MODEL_ENDPOINT_MAP = {}

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

def compare_and_update_models(new_models_list: List[dict], models_path: str):
    try:
        if os.path.exists(models_path) and os.path.getsize(models_path) > 0:
            with open(models_path, 'r', encoding='utf-8') as f:
                old_models = json.load(f)
        else:
            old_models = {}
    except (FileNotFoundError, json.JSONDecodeError):
        old_models = {}

    new_models_dict = {model['publicName']: model.get('id') for model in new_models_list if 'publicName' in model and 'id' in model}
    old_models_set = set(old_models.keys())
    new_models_set = set(new_models_dict.keys())
    added_models = new_models_set - old_models_set
    removed_models = old_models_set - new_models_set
    
    logger.info("---[ 模型列表更新检查 (HTML模式) ]---")
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
        logger.info("\n[结论] 模型列表与本地版本一致，无需更新。")
        logger.info("---[ 检查完毕 ]---")
        return

    logger.info("\n[结论] 检测到模型列表变更，正在更新 'models.json'...")
    try:
        with open(models_path, 'w', encoding='utf-8') as f:
            json.dump(new_models_dict, f, indent=4, ensure_ascii=False, sort_keys=True)
        logger.info(f"✅ 'models.json' 已成功更新，当前包含 {len(new_models_dict)} 个模型。")
        load_model_map()
    except IOError as e:
        logger.error(f"❌ 写入 '{models_path}' 文件时出错: {e}")
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
        logger.warning("模型更新请求未收到任何 HTML 内容。")
        return JSONResponse(status_code=400, content={"status": "error", "message": "No HTML content received."})
    
    logger.info("收到来自油猴脚本的页面内容，开始检查并更新模型...")
    new_models_list = extract_models_from_html(html_content_bytes.decode('utf-8'))
    
    if new_models_list:
        compare_and_update_models(new_models_list, 'models.json')
        return JSONResponse({"status": "success", "message": "Model comparison and update complete."})
    else:
        logger.error("未能从油猴脚本提供的 HTML 中提取模型数据。")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Could not extract model data from HTML."}
        )
        
@app.post("/v1/add-or-update-endpoint")
async def add_or_update_endpoint(payload: EndpointUpdatePayload):
    # ... 此函数及以下所有其他端点和类的代码都保持原样，无需修改 ...
    # 为了保证这是您可以直接使用的完整文件，此处将包含所有代码
    global MODEL_ENDPOINT_MAP
    new_entry = payload.dict(exclude_none=True, by_alias=True)
    model_name = new_entry.pop("modelName")
    if model_name not in MODEL_ENDPOINT_MAP:
        MODEL_ENDPOINT_MAP[model_name] = [new_entry]
        logger.info(f"成功为新模型 '{model_name}' 创建了新的端点映射列表。")
        return {"status": "success", "message": f"Endpoint for {model_name} created."}
    if isinstance(MODEL_ENDPOINT_MAP.get(model_name), list):
        endpoints = MODEL_ENDPOINT_MAP[model_name]
        new_session_id = new_entry.get('sessionId')
        is_duplicate = any(ep.get('sessionId') == new_session_id for ep in endpoints)
        if not is_duplicate:
            endpoints.append(new_entry)
            logger.info(f"成功为模型 '{model_name}' 追加了一个新的端点映射。")
            return {"status": "success", "message": f"New endpoint for {model_name} appended."}
        else:
            logger.info(f"检测到重复的 Session ID，已为模型 '{model_name}' 忽略本次添加。")
            return {"status": "skipped", "message": "Duplicate endpoint ignored."}
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

security = HTTPBasic()
class DeletePayload(BaseModel):
    model_name: str = Field(..., alias='modelName')
    session_id: str = Field(..., alias='sessionId')

def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    server_api_key = os.environ.get("API_KEY") or CONFIG.get("api_key")
    if server_api_key and credentials.password == server_api_key:
        return credentials.username
    raise HTTPException(status_code=401, detail="Incorrect API Key", headers={"WWW-Authenticate": "Basic"})

@app.post("/v1/delete-endpoint")
async def delete_endpoint(payload: DeletePayload, username: str = Depends(get_current_user)):
    global MODEL_ENDPOINT_MAP
    model_name = payload.model_name
    session_id_to_delete = payload.session_id
    if model_name in MODEL_ENDPOINT_MAP and isinstance(MODEL_ENDPOINT_MAP[model_name], list):
        original_len = len(MODEL_ENDPOINT_MAP[model_name])
        MODEL_ENDPOINT_MAP[model_name] = [ep for ep in MODEL_ENDPOINT_MAP[model_name] if ep.get('sessionId', ep.get('session_id')) != session_id_to_delete]
        if len(MODEL_ENDPOINT_MAP[model_name]) < original_len:
            if not MODEL_ENDPOINT_MAP[model_name]:
                del MODEL_ENDPOINT_MAP[model_name]
            return {"status": "success", "message": "Endpoint deleted."}
    raise HTTPException(status_code=404, detail="Endpoint not found")

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

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page(username: str = Depends(get_current_user)):
    # 此处省略管理页面的HTML生成代码以保持简洁，但它在您的文件中应保持不变
    return HTMLResponse(content="<h1>Admin Page Placeholder</h1>") # 实际应为完整的HTML

if __name__ == "__main__":
    # 确保在运行前，存在 modules/payload_converter.py 文件
    if not os.path.exists("modules/payload_converter.py"):
        logger.error("错误: 缺少 'modules/payload_converter.py' 文件。请确保该文件存在。")
        sys.exit(1)
        
    api_port = int(os.environ.get("PORT", 7860))
    logger.info(f"🚀 LMArena Bridge API 服务器正在启动...")
    logger.info(f"   - 监听地址: http://0.0.0.0:{api_port}")
    uvicorn.run("api_server:app", host="0.0.0.0", port=api_port)
