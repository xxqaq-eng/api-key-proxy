#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API Key 轮询代理 - 商汤日日新专用
解决单个 Key 的 429 频率限流和 5 小时配额限制问题
"""

import asyncio
import json
import os
import time
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

import yaml
import httpx
import re
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ===== 联网搜索功能 =====
# 搜索结果缓存
search_cache = {}
CACHE_EXPIRE_TIME = 3600  # 缓存过期时间（秒）

async def web_search(query: str, num_results: int = 5) -> str:
    """使用百度搜索获取网页信息，返回格式化的搜索结果"""
    try:
        # 清理查询内容：移除换行符、特殊字符，截断过长的查询
        clean_query = query.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
        clean_query = re.sub(r'\s+', ' ', clean_query).strip()
        
        # 优化搜索关键词：去掉常见的无关内容
        irrelevant_patterns = [
            r'^你好', r'^请问', r'^帮我', r'^我想知道', r'^我想问',
            r'^能不能', r'^可以吗', r'^吗$', r'^呢$', r'^啊$',
            r'谢谢', r'感谢', r'麻烦你', r'请你'
        ]
        for pattern in irrelevant_patterns:
            clean_query = re.sub(pattern, '', clean_query)
        clean_query = clean_query.strip()
        
        # 如果清理后关键词太短，就用原始查询
        if len(clean_query) < 2:
            clean_query = query.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').strip()
        
        # 只取前100个字符作为搜索关键词，避免搜索长篇上下文
        if len(clean_query) > 100:
            clean_query = clean_query[:100]
        
        # 检查缓存
        cache_key = clean_query.lower()
        if cache_key in search_cache:
            cached_data = search_cache[cache_key]
            if time.time() - cached_data['timestamp'] < CACHE_EXPIRE_TIME:
                logger.info(f"搜索命中缓存: {clean_query[:30]}...")
                return cached_data['results']
        
        # 对查询内容进行 URL 编码
        from urllib.parse import quote
        encoded_query = quote(clean_query, safe='')
        search_url = f"https://www.baidu.com/s?wd={encoded_query}&rn={num_results}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(search_url, headers=headers)
        
        if response.status_code != 200:
            logger.warning(f"搜索失败，状态码: {response.status_code}")
            return ""
        
        html_content = response.text
        
        # 解析搜索结果
        results = []
        
        # 百度搜索结果的格式：<h3 class="t"><a ...>标题</a></h3> 后面跟着摘要
        # 使用正则表达式提取标题和链接
        title_pattern = r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?</h3>'
        title_matches = re.findall(title_pattern, html_content, re.DOTALL)
        
        # 提取摘要
        abstract_pattern = r'<span class="content-right_8Zs40">(.*?)</span>'
        abstract_matches = re.findall(abstract_pattern, html_content, re.DOTALL)
        
        # 备用摘要提取模式
        if not abstract_matches:
            abstract_pattern2 = r'<div class="c-abstract[^"]*"[^>]*>(.*?)</div>'
            abstract_matches = re.findall(abstract_pattern2, html_content, re.DOTALL)
        
        seen_urls = set()
        for i, (url, title) in enumerate(title_matches[:num_results * 2]):  # 多取一些，去重后再截断
            # 清理标题中的 HTML 标签
            clean_title = re.sub(r'<[^>]+>', '', title).strip()
            # 清理摘要中的 HTML 标签
            abstract = ""
            if i < len(abstract_matches):
                abstract = re.sub(r'<[^>]+>', '', abstract_matches[i]).strip()
                # 截断过长的摘要
                if len(abstract) > 200:
                    abstract = abstract[:200] + "..."
            
            # 去重
            if clean_title and url not in seen_urls:
                seen_urls.add(url)
                results.append({
                    "title": clean_title,
                    "url": url,
                    "abstract": abstract
                })
            
            if len(results) >= num_results:
                break
        
        if not results:
            logger.warning("未解析到搜索结果")
            return ""
        
        # 格式化搜索结果 - 优化格式，让模型更容易理解
        formatted_results = "【联网搜索结果】\n\n"
        formatted_results += f"搜索关键词: {clean_query}\n\n"
        for i, result in enumerate(results, 1):
            formatted_results += f"【结果{i}】{result['title']}\n"
            if result['abstract']:
                formatted_results += f"摘要: {result['abstract']}\n"
            formatted_results += f"来源: {result['url']}\n\n"
        
        formatted_results += "【使用说明】\n"
        formatted_results += "1. 请优先根据以上搜索结果回答用户的问题\n"
        formatted_results += "2. 如果搜索结果中有相关信息，请引用并说明来源\n"
        formatted_results += "3. 如果搜索结果中没有相关信息，请根据你自己的知识回答\n"
        formatted_results += "4. 回答时请注明信息来源是联网搜索还是你的知识\n"
        
        # 存入缓存
        search_cache[cache_key] = {
            'results': formatted_results,
            'timestamp': time.time()
        }
        
        # 限制缓存大小，避免内存占用过大
        if len(search_cache) > 100:
            # 删除最旧的缓存
            oldest_key = min(search_cache.keys(), key=lambda k: search_cache[k]['timestamp'])
            del search_cache[oldest_key]
        
        logger.info(f"搜索完成，找到 {len(results)} 条结果")
        return formatted_results
        
    except Exception as e:
        logger.error(f"搜索异常: {e}")
        return ""

# ===== 配置加载 =====
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

config = load_config()

# ===== 日志配置 =====
logging.basicConfig(
    level=getattr(logging, config.get("log_level", "INFO")),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f"proxy_{datetime.now().strftime('%Y%m%d')}.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ===== 全局变量 =====
API_KEYS = config.get("api_keys", [])
TARGET_BASE_URL = config.get("target_base_url", "https://token.sensenova.cn/v1")
RATE_LIMIT_COOLDOWN = config.get("rate_limit_cooldown", 60)
QUOTA_LIMIT_COOLDOWN = config.get("quota_limit_cooldown", 18000)
RETRY_STATUS_CODES = config.get("retry_status_codes", [429, 500, 502, 503, 504])
MAX_RETRIES = config.get("max_retries", 3)
ACCESS_KEY = config.get("access_key", "")
PERSIST_STATS = config.get("persist_stats", True)
PERSIST_INTERVAL = config.get("persist_interval", 30)
ENABLE_WEB_SEARCH = config.get("enable_web_search", False)
WEB_SEARCH_RESULTS = config.get("web_search_results", 5)

# 临时存储 function calling 的结果（用于模拟联网搜索）
FUNCTION_CALLING_CACHE = {}

KEY_PRIORITIES = {}
KEY_REMARKS = {}

# 从配置文件加载备注
_remarks_config = config.get("key_remarks", {})
if _remarks_config:
    for _k, _v in _remarks_config.items():
        KEY_REMARKS[_k] = _v

# ===== API Key 管理器 =====
class KeyManager:
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.current_index = 0
        self.cool_until = {}  # key -> timestamp
        self.disabled = set()
        self.lock = asyncio.Lock()

    async def get_next_key(self) -> Optional[str]:
        async with self.lock:
            now = time.time()
            available = []
            for i, key in enumerate(self.keys):
                if key in self.disabled:
                    continue
                if key in self.cool_until and self.cool_until[key] > now:
                    continue
                available.append((i, key))

            if not available:
                return None

            # 按优先级排序，优先级高的优先
            available.sort(key=lambda x: KEY_PRIORITIES.get(x[1], 0), reverse=True)
            # 在相同优先级中轮询
            max_priority = available[0][1]
            same_priority = [(i, k) for i, k in available if KEY_PRIORITIES.get(k, 0) == KEY_PRIORITIES.get(max_priority, 0)]
            idx = self.current_index % len(same_priority)
            self.current_index += 1
            return same_priority[idx][1]

    async def set_cooling(self, key: str, cooldown_seconds: int):
        async with self.lock:
            self.cool_until[key] = time.time() + cooldown_seconds
            logger.info(f"Key 进入冷却 | {key[:8]}... | 冷却 {cooldown_seconds} 秒")

    async def unlock(self, key: str):
        async with self.lock:
            if key in self.cool_until:
                del self.cool_until[key]
            logger.info(f"Key 手动解锁 | {key[:8]}...")

    async def disable(self, key: str):
        async with self.lock:
            self.disabled.add(key)
            logger.info(f"Key 已禁用 | {key[:8]}...")

    async def enable(self, key: str):
        async with self.lock:
            self.disabled.discard(key)
            logger.info(f"Key 已启用 | {key[:8]}...")

    def get_status(self) -> List[Dict]:
        now = time.time()
        result = []
        for i, key in enumerate(self.keys):
            cool_remaining = 0
            available = True
            if key in self.disabled:
                available = False
            elif key in self.cool_until and self.cool_until[key] > now:
                available = False
                cool_remaining = self.cool_until[key] - now
            result.append({
                "index": i,
                "key": key,
                "available": available,
                "disabled": key in self.disabled,
                "cooldown_remaining": max(0, cool_remaining),
                "priority": KEY_PRIORITIES.get(key, 0),
                "remark": KEY_REMARKS.get(key, ""),
            })
        return result

    async def add_key(self, key: str, priority: int = 0, remark: str = ""):
        async with self.lock:
            if key not in self.keys:
                self.keys.append(key)
                KEY_PRIORITIES[key] = priority
                KEY_REMARKS[key] = remark
                logger.info(f"添加 Key | {key[:8]}... | 优先级 {priority} | 备注 {remark}")
                return True
            return False

    async def remove_key(self, index: int):
        async with self.lock:
            if 0 <= index < len(self.keys):
                key = self.keys.pop(index)
                self.cool_until.pop(key, None)
                self.disabled.discard(key)
                KEY_PRIORITIES.pop(key, None)
                KEY_REMARKS.pop(key, None)
                logger.info(f"删除 Key | {key[:8]}...")
                return True
            return False

    async def set_remark(self, key: str, remark: str):
        async with self.lock:
            KEY_REMARKS[key] = remark
            # 保存到配置文件
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                if "key_remarks" not in cfg or cfg["key_remarks"] is None:
                    cfg["key_remarks"] = {}
                cfg["key_remarks"][key] = remark
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            except Exception as e:
                logger.error(f"保存备注到配置文件失败: {e}")
            logger.info(f"修改 Key 备注 | {key[:8]}... | 新备注: {remark}")
            return True

    async def reorder(self, new_order: List[int]):
        async with self.lock:
            self.keys = [self.keys[i] for i in new_order if 0 <= i < len(self.keys)]

key_manager = KeyManager(API_KEYS)

# ===== 统计管理器 =====
class StatsManager:
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.stats = {}
        self.lock = asyncio.Lock()
        self.dirty = False
        self._load()

    def _new_stat(self) -> Dict:
        return {
            "total_requests": 0,
            "success": 0,
            "failed": 0,
            "rate_limited": 0,
            "quota_limited": 0,
            "server_error": 0,
            "total_tokens": 0,
            "total_time_ms": 0,
            "min_time_ms": 0,
            "max_time_ms": 0,
            "last_used": "",
            "model_usage": {},
        }

    def ensure_key(self, key: str):
        if key not in self.stats:
            self.stats[key] = self._new_stat()

    async def record_request(self, key: str, success: bool, status_code: int = 0,
                              tokens: int = 0, elapsed_ms: int = 0, limit_type: str = "",
                              model: str = ""):
        async with self.lock:
            self.ensure_key(key)
            s = self.stats[key]
            s["total_requests"] += 1
            s["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            s["total_time_ms"] += elapsed_ms
            if s["min_time_ms"] == 0 or elapsed_ms < s["min_time_ms"]:
                s["min_time_ms"] = elapsed_ms
            if elapsed_ms > s["max_time_ms"]:
                s["max_time_ms"] = elapsed_ms
            if success:
                s["success"] += 1
                s["total_tokens"] += tokens
                if model:
                    if model not in s["model_usage"]:
                        s["model_usage"][model] = {"requests": 0, "tokens": 0}
                    s["model_usage"][model]["requests"] += 1
                    s["model_usage"][model]["tokens"] += tokens
            else:
                s["failed"] += 1
                if status_code == 429:
                    if limit_type == "quota":
                        s["quota_limited"] += 1
                    else:
                        s["rate_limited"] += 1
                elif status_code >= 500:
                    s["server_error"] += 1
            self.dirty = True

    def get_stats(self) -> Dict:
        result = {"keys": [], "stats": {}}
        status_list = key_manager.get_status()
        for i, key in enumerate(self.keys):
            self.ensure_key(key)
            s = self.stats[key]
            avg_time = round(s["total_time_ms"] / s["total_requests"]) if s["total_requests"] > 0 else 0
            success_rate = round(s["success"] / s["total_requests"] * 100, 1) if s["total_requests"] > 0 else 0
            status = "available"
            cooldown_remaining = 0
            priority = KEY_PRIORITIES.get(key, 0)
            if i < len(status_list):
                st = status_list[i]
                if st.get("disabled"):
                    status = "disabled"
                elif not st.get("available"):
                    status = "cooling"
                cooldown_remaining = st.get("cooldown_remaining", 0)
                priority = st.get("priority", priority)
            result["keys"].append({
                "index": i,
                "key": key[:8] + "..." if len(key) > 8 else key,
                "key_full": key,
                "remark": KEY_REMARKS.get(key, ""),
                "priority": priority,
                "status": status,
                "cooldown_remaining": cooldown_remaining,
                "total_requests": s["total_requests"],
                "success": s["success"],
                "failed": s["failed"],
                "success_rate": success_rate,
                "rate_limited": s["rate_limited"],
                "quota_limited": s["quota_limited"],
                "server_error": s["server_error"],
                "total_tokens": s["total_tokens"],
                "avg_time_ms": avg_time,
                "min_time_ms": s["min_time_ms"],
                "max_time_ms": s["max_time_ms"],
                "last_used": s["last_used"],
                "model_usage": s.get("model_usage", {}),
            })
            result["stats"][key] = {
                "total_requests": s["total_requests"],
                "success": s["success"],
                "failed": s["failed"],
                "success_rate": success_rate,
                "rate_limited": s["rate_limited"],
                "quota_limited": s["quota_limited"],
                "server_error": s["server_error"],
                "total_tokens": s["total_tokens"],
                "avg_response_ms": avg_time,
                "min_time_ms": s["min_time_ms"],
                "max_time_ms": s["max_time_ms"],
                "last_used": s["last_used"],
                "model_usage": s.get("model_usage", {}),
            }
        return result

    def _load(self):
        path = os.path.join(DATA_DIR, "stats.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.stats = json.load(f)
                logger.info("已加载历史统计数据")
            except Exception as e:
                logger.error(f"加载统计数据失败: {e}")
                self.stats = {}

    async def save(self):
        if not PERSIST_STATS or not self.dirty:
            return
        async with self.lock:
            path = os.path.join(DATA_DIR, "stats.json")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.stats, f, ensure_ascii=False, indent=2)
                self.dirty = False
            except Exception as e:
                logger.error(f"保存统计数据失败: {e}")

stats_manager = StatsManager(API_KEYS)

# ===== 按天统计 =====
class DailyStats:
    def __init__(self):
        self.stats = {}
        self.lock = asyncio.Lock()
        self._load()

    async def record(self, success: bool, tokens: int = 0):
        async with self.lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if today not in self.stats:
                self.stats[today] = {"requests": 0, "success": 0, "failed": 0, "tokens": 0}
            self.stats[today]["requests"] += 1
            if success:
                self.stats[today]["success"] += 1
                self.stats[today]["tokens"] += tokens
            else:
                self.stats[today]["failed"] += 1

    def get_daily(self, days: int = 30) -> List[Dict]:
        result = []
        for i in range(days - 1, -1, -1):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            d = self.stats.get(date, {"requests": 0, "success": 0, "failed": 0, "tokens": 0})
            result.append({"date": date, **d})
        return result

    def _load(self):
        path = os.path.join(DATA_DIR, "daily_stats.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.stats = json.load(f)
            except Exception as e:
                logger.error(f"加载按天统计失败: {e}")

    async def save(self):
        async with self.lock:
            path = os.path.join(DATA_DIR, "daily_stats.json")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.stats, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"保存按天统计失败: {e}")

daily_stats = DailyStats()

# ===== 高级统计 =====
class AdvancedStats:
    def __init__(self):
        self.status_codes = {}
        self.response_time_buckets = {"<100ms": 0, "100-500ms": 0, "500-1000ms": 0, "1-3s": 0, ">3s": 0}
        self.rate_limit_events = []
        self.hourly_requests = {str(h): 0 for h in range(24)}
        self.model_usage = {}
        self.slow_requests = []
        self.lock = asyncio.Lock()

    async def record(self, status_code: int, elapsed_ms: int, model: str = "", is_rate_limit: bool = False):
        async with self.lock:
            self.status_codes[str(status_code)] = self.status_codes.get(str(status_code), 0) + 1
            if elapsed_ms < 100:
                self.response_time_buckets["<100ms"] += 1
            elif elapsed_ms < 500:
                self.response_time_buckets["100-500ms"] += 1
            elif elapsed_ms < 1000:
                self.response_time_buckets["500-1000ms"] += 1
            elif elapsed_ms < 3000:
                self.response_time_buckets["1-3s"] += 1
            else:
                self.response_time_buckets[">3s"] += 1
            hour = str(datetime.now().hour)
            self.hourly_requests[hour] = self.hourly_requests.get(hour, 0) + 1
            if model:
                self.model_usage[model] = self.model_usage.get(model, 0) + 1
            if elapsed_ms > 1000:
                self.slow_requests.append({"time": datetime.now().strftime("%H:%M:%S"), "elapsed_ms": elapsed_ms, "model": model})
                if len(self.slow_requests) > 50:
                    self.slow_requests = self.slow_requests[-50:]
            if is_rate_limit:
                self.rate_limit_events.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status_code": status_code})
                if len(self.rate_limit_events) > 100:
                    self.rate_limit_events = self.rate_limit_events[-100:]

    def get_all(self) -> Dict:
        return {
            "status_codes": self.status_codes,
            "response_time_buckets": self.response_time_buckets,
            "rate_limit_events": self.rate_limit_events[-20:],
            "hourly_requests": self.hourly_requests,
            "model_usage": self.model_usage,
            "slow_requests": self.slow_requests[-10:],
        }

advanced_stats = AdvancedStats()

# ===== 最近请求记录 =====
recent_requests = []
recent_requests_lock = asyncio.Lock()

async def add_recent_request(status_code: int, model: str, tokens: int, elapsed_ms: int, key: str, request_body: str = "", response_body: str = ""):
    async with recent_requests_lock:
        recent_requests.insert(0, {
            "id": len(recent_requests) + 1,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status_code": status_code,
            "model": model,
            "tokens": tokens,
            "elapsed_ms": elapsed_ms,
            "key": key[:8] + "..." if len(key) > 8 else key,
            "key_full": key,
            "remark": KEY_REMARKS.get(key, ""),
            "request_body": request_body[:500] if request_body else "",
            "response_body": response_body[:500] if response_body else "",
        })
        if len(recent_requests) > 100:
            recent_requests.pop()

# ===== 操作日志 =====
action_logs = []
action_logs_lock = asyncio.Lock()

async def add_action_log(action: str, detail: str):
    async with action_logs_lock:
        action_logs.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "detail": detail,
        })
        if len(action_logs) > 200:
            action_logs.pop()

# ===== FastAPI 应用 =====
app = FastAPI(title="API Key 轮询代理")

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 50)
    logger.info("启动 | API Key 轮询代理")
    logger.info(f"启动 | 目标地址: {TARGET_BASE_URL}")
    logger.info(f"启动 | 加载了 {len(API_KEYS)} 个 API Key")
    for i, key in enumerate(API_KEYS):
        logger.info(f"启动 |   Key{i+1}: {key[:8]}... (优先级: {KEY_PRIORITIES.get(key, 0)})")
    logger.info(f"启动 | 频率限流冷却: {RATE_LIMIT_COOLDOWN} 秒")
    logger.info(f"启动 | 配额限流冷却: {QUOTA_LIMIT_COOLDOWN} 秒 ({QUOTA_LIMIT_COOLDOWN/3600:.1f} 小时)")
    logger.info(f"启动 | 统计持久化: {'开启' if PERSIST_STATS else '关闭'}")
    logger.info(f"启动 | 访问密码: {'已设置' if ACCESS_KEY else '未设置（不验证）'}")
    logger.info("=" * 50)
    asyncio.create_task(persist_worker())

async def persist_worker():
    while True:
        await asyncio.sleep(PERSIST_INTERVAL)
        await stats_manager.save()
        await daily_stats.save()

# ===== 访问密码验证 =====
def verify_access(request: Request) -> bool:
    if not ACCESS_KEY:
        return True
    # 从 Authorization header (Bearer)、X-Access-Key header、query 参数中获取
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        if auth[7:] == ACCESS_KEY:
            return True
    access_key = request.headers.get("X-Access-Key", "")
    if access_key == ACCESS_KEY:
        return True
    query_key = request.query_params.get("access_key", "")
    if query_key == ACCESS_KEY:
        return True
    return False

# ===== 流式转发函数（包含重试逻辑）=====
async def stream_proxy_with_retry(path, request, body_bytes, model_name, is_stream, request_kwargs_base):
    last_error = None
    for attempt in range(MAX_RETRIES):
        key = await key_manager.get_next_key()
        if not key:
            logger.error("所有 Key 都在冷却中或已禁用")
            yield f'data: {{"error": "Service Unavailable", "message": "所有 Key 都在冷却中或已禁用"}}\n\n'.encode("utf-8")
            return
        
        start_time = time.time()
        try:
            # 构建带 Authorization 的请求头
            headers = dict(request_kwargs_base.get("headers", {}))
            headers["Authorization"] = f"Bearer {key}"
            request_kwargs = dict(request_kwargs_base)
            request_kwargs["headers"] = headers
            
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(**request_kwargs) as resp:
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    
                    # 429 限流
                    if resp.status_code == 429:
                        response_text = (await resp.aread()).decode("utf-8", errors="ignore")
                        # 保守策略：默认判定为频率限流（只冷却60秒）
                        # 只有在非常确定是配额耗尽的情况下才判定为配额限流（5小时）
                        limit_type = "rate"
                        response_lower = response_text.lower()
                        
                        # 配额耗尽的明确信号（需要同时满足多个条件，避免误判）
                        quota_signals = 0
                        if "quota" in response_lower:
                            quota_signals += 1
                        if "insufficient" in response_lower or "exceeded" in response_lower:
                            quota_signals += 1
                        if "5小时" in response_text or "5 hour" in response_lower or "300分钟" in response_text:
                            quota_signals += 2  # 明确提到5小时，权重更高
                        if "rate" in response_lower and "limit" in response_lower:
                            quota_signals -= 1  # 明确提到频率限制，降低配额判断
                        
                        # 只有配额信号 >= 2 才判定为配额限流，否则都按频率限流处理
                        if quota_signals >= 2:
                            limit_type = "quota"
                            await key_manager.set_cooling(key, QUOTA_LIMIT_COOLDOWN)
                            logger.warning(f"判定为配额限流 | Key: {key[:8]}... | 信号分: {quota_signals} | 响应: {response_text[:200]}")
                        else:
                            await key_manager.set_cooling(key, RATE_LIMIT_COOLDOWN)
                            logger.info(f"判定为频率限流 | Key: {key[:8]}... | 信号分: {quota_signals} | 响应: {response_text[:200]}")
                        
                        await stats_manager.record_request(key, success=False, status_code=429, elapsed_ms=elapsed_ms, limit_type=limit_type, model=model_name)
                        await daily_stats.record(success=False)
                        await add_recent_request(429, model_name, 0, elapsed_ms, key)
                        last_error = resp
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(1)
                            continue
                        else:
                            yield response_text.encode("utf-8")
                            return
                    
                    # 5xx 错误
                    if resp.status_code >= 500:
                        response_text = (await resp.aread()).decode("utf-8", errors="ignore")
                        await stats_manager.record_request(key, success=False, status_code=resp.status_code, elapsed_ms=elapsed_ms, model=model_name)
                        await daily_stats.record(success=False)
                        await add_recent_request(resp.status_code, model_name, 0, elapsed_ms, key)
                        last_error = resp
                        if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(0.5)
                            continue
                        else:
                            yield response_text.encode("utf-8")
                            return
                    
                    # 成功 - 流式转发
                    is_success = 200 <= resp.status_code < 300
                    total_tokens = 0
                    response_buffer = ""  # 收集完整响应数据用于解析 usage
                    chunk_count = 0
                    connection_broken = False
                    try:
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                chunk_count += 1
                                # 添加到缓冲区
                                try:
                                    chunk_str = chunk.decode("utf-8", errors="ignore")
                                    response_buffer += chunk_str
                                    
                                    # 尝试从缓冲区中解析 usage 信息（支持多种格式）
                                    if total_tokens == 0 and '"usage"' in response_buffer:
                                        import re
                                        # 格式1: "total_tokens": 123
                                        match = re.search(r'"total_tokens"\s*:\s*(\d+)', response_buffer)
                                        if match:
                                            total_tokens = int(match.group(1))
                                        # 格式2: usage 在 SSE data 中
                                        if total_tokens == 0:
                                            for line in response_buffer.split('\n'):
                                                if line.startswith('data:') and '"usage"' in line:
                                                    try:
                                                        json_str = line[5:].strip()
                                                        if json_str and json_str != '[DONE]':
                                                            usage_json = json.loads(json_str)
                                                            if 'usage' in usage_json and usage_json['usage']:
                                                                total_tokens = usage_json['usage'].get('total_tokens', 0)
                                                    except:
                                                        pass
                                except:
                                    pass
                                yield chunk
                    except Exception as e:
                        connection_broken = True
                        error_str = str(e)
                        logger.warning(f"流式连接断开 | 已接收 {chunk_count} 个数据块 | 错误: {error_str}")
                        
                        # 判断是否是连接断开类错误
                        is_connection_error = any(keyword in error_str.lower() for keyword in [
                            'connection', 'reset', 'broken', 'closed', 'timeout', 
                            'remotedisconnected', 'peer', 'aborted', 'eof'
                        ])
                        
                        if is_connection_error:
                            # 给客户端发送结束信号，避免显示"继续。"
                            logger.info("连接断开，发送结束信号给客户端")
                            yield b'data: [DONE]\n\n'
                        else:
                            # 其他错误，发送错误信息
                            error_msg = f'data: {{"error": "Stream error", "message": "{error_str}"}}\n\n'
                            yield error_msg.encode('utf-8')
                    finally:
                        # 如果还没找到 usage，从完整缓冲区再解析一次
                        if total_tokens == 0 and response_buffer:
                            try:
                                import re
                                match = re.search(r'"total_tokens"\s*:\s*(\d+)', response_buffer)
                                if match:
                                    total_tokens = int(match.group(1))
                            except:
                                pass
                        
                        # 如果还是0，尝试粗略估算（按字符数估算）
                        if total_tokens == 0 and response_buffer:
                            try:
                                # 提取 content 字段的内容来估算
                                content_match = re.findall(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', response_buffer)
                                total_content = ''.join(content_match)
                                if total_content:
                                    # 粗略估算：中文字符算1 token，英文单词算1 token
                                    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', total_content))
                                    english_words = len(re.findall(r'[a-zA-Z]+', total_content))
                                    estimated_tokens = chinese_chars + english_words + 100  # 加100估算输入token
                                    total_tokens = max(estimated_tokens, 1)
                                    logger.info(f"未找到 usage 信息，粗略估算 token: {total_tokens}")
                            except:
                                pass
                        
                        elapsed_final = int((time.time() - start_time) * 1000)
                        final_status = resp.status_code
                        if connection_broken and chunk_count > 0:
                            # 连接断开但已经接收了部分数据，算部分成功
                            final_status = 200
                            is_success = True
                            logger.info(f"流式请求部分完成 | 接收 {chunk_count} 个数据块 | 耗时 {elapsed_final}ms | Token {total_tokens}")
                        
                        await stats_manager.record_request(key, success=is_success, status_code=final_status, tokens=total_tokens, elapsed_ms=elapsed_final, model=model_name)
                        await daily_stats.record(success=is_success, tokens=total_tokens)
                        await advanced_stats.record(final_status, elapsed_final, model_name)
                        await add_recent_request(final_status, model_name, total_tokens, elapsed_final, key)
                    return
        except httpx.TimeoutException:
            elapsed_ms = int((time.time() - start_time) * 1000)
            await stats_manager.record_request(key, success=False, status_code=504, elapsed_ms=elapsed_ms, model=model_name)
            await daily_stats.record(success=False)
            await add_recent_request(504, model_name, 0, elapsed_ms, key)
            last_error = "timeout"
            if attempt < MAX_RETRIES - 1:
                continue
            else:
                yield f'data: {{"error": "Gateway Timeout"}}\n\n'.encode("utf-8")
                return
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.error(f"请求异常 | {key[:8]}... | {e}")
            await stats_manager.record_request(key, success=False, status_code=502, elapsed_ms=elapsed_ms, model=model_name)
            await daily_stats.record(success=False)
            await add_recent_request(502, model_name, 0, elapsed_ms, key)
            last_error = str(e)
            if attempt < MAX_RETRIES - 1:
                continue
            else:
                yield f'data: {{"error": "Bad Gateway", "message": "{str(e)}"}}\n\n'.encode("utf-8")
                return
    
    yield f'data: {{"error": "Bad Gateway", "message": "所有重试均失败"}}\n\n'.encode("utf-8")

# ===== OpenAI Responses 格式支持 =====
def convert_responses_to_completions(req_json: dict) -> dict:
    """将 OpenAI Responses 格式的请求转换成 Completions 格式"""
    completions = {}
    
    # 模型名称
    if "model" in req_json:
        completions["model"] = req_json["model"]
    
    # 输入转换
    input_data = req_json.get("input", "")
    messages = []
    
    if isinstance(input_data, str):
        # 字符串输入，直接作为用户消息
        messages.append({"role": "user", "content": input_data})
    elif isinstance(input_data, list):
        # 数组输入，转换消息格式
        for item in input_data:
            if isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content", "")
                
                # 角色转换：商汤 API 不支持 developer 角色，转换成 system
                if role == "developer":
                    role = "system"
                
                # 处理 content 可能是数组的情况
                if isinstance(content, list):
                    text_parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") in ["input_text", "output_text", "text"]:
                            text_parts.append(c.get("text", ""))
                    content = "".join(text_parts)
                
                messages.append({"role": role, "content": content})
    
    completions["messages"] = messages
    
    # 流式
    if "stream" in req_json:
        completions["stream"] = req_json["stream"]
    
    # max_output_tokens -> max_tokens
    if "max_output_tokens" in req_json:
        completions["max_tokens"] = req_json["max_output_tokens"]
    elif "max_tokens" in req_json:
        completions["max_tokens"] = req_json["max_tokens"]
    
    # 其他参数 - 只传递商汤 API 支持的参数
    for param in ["temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty", "stop"]:
        if param in req_json:
            completions[param] = req_json[param]
    
    # 注意：不传递 thinking、reasoning_effort、stream_options 等商汤 API 不支持的参数
    # 这些参数会导致商汤 API 返回 400 Bad Request
    
    # tools 转换 - 商汤 API 可能不支持 tools，暂时去掉
    # if "tools" in req_json:
    #     completions["tools"] = req_json["tools"]
    
    return completions


def convert_completions_to_responses(resp_json: dict, model_name: str = "") -> dict:
    """将 Completions 格式的非流式响应转换成 Responses 格式"""
    responses = {
        "id": "resp_" + resp_json.get("id", "unknown")[8:],
        "object": "response",
        "created_at": resp_json.get("created", int(time.time())),
        "model": model_name or resp_json.get("model", ""),
        "output": [],
        "usage": {}
    }
    
    # 转换 choices -> output
    choices = resp_json.get("choices", [])
    if choices:
        choice = choices[0]
        message = choice.get("message", {})
        content = message.get("content", "")
        
        output_item = {
            "type": "message",
            "role": message.get("role", "assistant"),
            "content": [
                {"type": "output_text", "text": content}
            ]
        }
        responses["output"].append(output_item)
    
    # 转换 usage
    usage = resp_json.get("usage", {})
    responses["usage"] = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0)
    }
    
    return responses


# ===== OpenAI Responses 代理端点 =====
@app.post("/v1/responses")
async def proxy_responses(request: Request):
    """代理 OpenAI Responses 格式的请求，自动转换成 Completions 格式"""
    # 访问密码验证
    if not verify_access(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized", "message": "访问密码错误"})
    
    # 读取请求体
    body_bytes = await request.body()
    try:
        req_json = json.loads(body_bytes)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": "Bad Request", "message": f"无效的请求体: {e}"})
    
    model_name = req_json.get("model", "")
    is_stream = req_json.get("stream", False)
    
    # 转换成 Completions 格式
    completions_req = convert_responses_to_completions(req_json)
    
    # 自动限制 max_tokens
    MAX_ALLOWED_TOKENS = 8192
    if "max_tokens" in completions_req and completions_req["max_tokens"] > MAX_ALLOWED_TOKENS:
        logger.info(f"[Responses] 自动限制 max_tokens: {completions_req['max_tokens']} -> {MAX_ALLOWED_TOKENS}")
        completions_req["max_tokens"] = MAX_ALLOWED_TOKENS
    
    completions_body = json.dumps(completions_req, ensure_ascii=False).encode("utf-8")
    
    # 联网搜索功能
    if ENABLE_WEB_SEARCH and "messages" in completions_req:
        # 提取最后一条用户消息
        user_query = ""
        for msg in reversed(completions_req["messages"]):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    user_query = content
                break
        
        # 只在用户问题比较短时才搜索（小于200字符），避免搜索长篇上下文
        if user_query and 2 < len(user_query) < 200:
            logger.info(f"[Responses] 联网搜索 | 查询: {user_query[:50]}...")
            search_results = await web_search(user_query, WEB_SEARCH_RESULTS)
            if search_results:
                # 把搜索结果作为系统提示添加到请求中
                has_system = False
                for msg in completions_req["messages"]:
                    if msg.get("role") == "system":
                        msg["content"] = msg.get("content", "") + "\n\n" + search_results
                        has_system = True
                        break
                
                if not has_system:
                    # 在消息列表开头添加系统提示
                    completions_req["messages"].insert(0, {
                        "role": "system",
                        "content": search_results
                    })
                
                completions_body = json.dumps(completions_req, ensure_ascii=False).encode("utf-8")
                logger.info("[Responses] 联网搜索 | 搜索结果已添加到请求中")
    
    logger.info(f"[Responses] 收到请求 | 模型: {model_name} | 流式: {is_stream}")
    
    if is_stream:
        # 流式请求 - 使用单独的异步生成器函数，避免流被提前关闭
        async def stream_responses_with_retry():
            for attempt in range(MAX_RETRIES):
                key = await key_manager.get_next_key()
                if not key:
                    logger.error("[Responses] 所有 Key 都在冷却中或已禁用")
                    yield f'data: {{"error": "Service Unavailable", "message": "所有 Key 都在冷却中或已禁用"}}\n\n'.encode("utf-8")
                    return
                
                start_time = time.time()
                try:
                    target_url = f"{TARGET_BASE_URL}/chat/completions"
                    
                    headers = {
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    }
                    
                    async with httpx.AsyncClient(timeout=300.0) as client:
                        async with client.stream("POST", target_url, headers=headers, content=completions_body) as resp:
                            elapsed_ms = int((time.time() - start_time) * 1000)
                            
                            if resp.status_code == 429:
                                response_text = (await resp.aread()).decode("utf-8", errors="ignore")
                                limit_type = "rate"
                                response_lower = response_text.lower()
                                quota_signals = 0
                                if "quota" in response_lower: quota_signals += 1
                                if "insufficient" in response_lower or "exceeded" in response_lower: quota_signals += 1
                                if "5小时" in response_text: quota_signals += 2
                                if "rate" in response_lower and "limit" in response_lower: quota_signals -= 1
                                
                                if quota_signals >= 2:
                                    limit_type = "quota"
                                    await key_manager.set_cooling(key, QUOTA_LIMIT_COOLDOWN)
                                else:
                                    await key_manager.set_cooling(key, RATE_LIMIT_COOLDOWN)
                                
                                await stats_manager.record_request(key, success=False, status_code=429, elapsed_ms=elapsed_ms, limit_type=limit_type, model=model_name)
                                await daily_stats.record(success=False)
                                await add_recent_request(429, model_name, 0, elapsed_ms, key)
                                
                                if attempt < MAX_RETRIES - 1:
                                    await asyncio.sleep(1)
                                    continue
                                else:
                                    yield f'data: {{"error": "rate_limit", "message": "{response_text}"}}\n\n'.encode("utf-8")
                                    return
                            
                            # 处理 400 错误 - 请求格式错误
                            if resp.status_code == 400:
                                response_text = (await resp.aread()).decode("utf-8", errors="ignore")
                                logger.error(f"[Responses] 商汤 API 返回 400 错误: {response_text[:200]}")
                                await stats_manager.record_request(key, success=False, status_code=400, elapsed_ms=elapsed_ms, model=model_name)
                                await daily_stats.record(success=False)
                                await add_recent_request(400, model_name, 0, elapsed_ms, key)
                                
                                # 400 错误不重试，直接返回错误
                                error_msg = response_text.replace('"', '\\"').replace('\n', ' ')
                                yield f'data: {{"error": "invalid_request", "message": "{error_msg}"}}\n\n'.encode("utf-8")
                                return
                            
                            if resp.status_code >= 500:
                                response_text = (await resp.aread()).decode("utf-8", errors="ignore")
                                await stats_manager.record_request(key, success=False, status_code=resp.status_code, elapsed_ms=elapsed_ms, model=model_name)
                                await daily_stats.record(success=False)
                                await add_recent_request(resp.status_code, model_name, 0, elapsed_ms, key)
                                
                                if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES - 1:
                                    await asyncio.sleep(0.5)
                                    continue
                                else:
                                    yield f'data: {{"error": "server_error", "message": "{response_text}"}}\n\n'.encode("utf-8")
                                    return
                            
                            # 成功 - 流式转换
                            is_success = 200 <= resp.status_code < 300
                            total_tokens = 0
                            response_buffer = ""
                            resp_id = f"resp_{int(time.time())}"
                            
                            # 发送 response.created 事件
                            seq_num = 0
                            created_response = {
                                "id": resp_id,
                                "object": "response",
                                "created_at": int(time.time()),
                                "model": model_name,
                                "status": "in_progress",
                                "output": []
                            }
                            created_event = {
                                "type": "response.created",
                                "sequence_number": seq_num,
                                "response": created_response
                            }
                            seq_num += 1
                            yield f"event: response.created\ndata: {json.dumps(created_event, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # 发送 output_item.added 事件
                            item_id = f"msg_{int(time.time())}"
                            output_item_event = {
                                "type": "response.output_item.added",
                                "sequence_number": seq_num,
                                "item": {
                                    "id": item_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "content": []
                                },
                                "output_index": 0
                            }
                            seq_num += 1
                            yield f"event: response.output_item.added\ndata: {json.dumps(output_item_event, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # 发送 content_part.added 事件
                            content_part_event = {
                                "type": "response.content_part.added",
                                "sequence_number": seq_num,
                                "item_id": item_id,
                                "output_index": 0,
                                "content_index": 0,
                                "part": {
                                    "type": "output_text",
                                    "text": ""
                                }
                            }
                            seq_num += 1
                            yield f"event: response.content_part.added\ndata: {json.dumps(content_part_event, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            full_text = ""
                            try:
                                async for chunk in resp.aiter_bytes():
                                    if chunk:
                                        chunk_str = chunk.decode("utf-8", errors="ignore")
                                        response_buffer += chunk_str
                                        
                                        # 解析 SSE 格式的 chunk
                                        for line in chunk_str.split("\n"):
                                            line = line.strip()
                                            if line.startswith("data:"):
                                                data_str = line[5:].strip()
                                                if data_str == "[DONE]":
                                                    continue
                                                try:
                                                    chunk_json = json.loads(data_str)
                                                    choices = chunk_json.get("choices", [])
                                                    if choices:
                                                        delta = choices[0].get("delta", {})
                                                        content = delta.get("content", "")
                                                        if content:
                                                            full_text += content
                                                            # 转换成 Responses 格式的 delta 事件
                                                            delta_event = {
                                                                "type": "response.output_text.delta",
                                                                "sequence_number": seq_num,
                                                                "delta": content,
                                                                "item_id": item_id,
                                                                "output_index": 0,
                                                                "content_index": 0
                                                            }
                                                            seq_num += 1
                                                            yield f"event: response.output_text.delta\ndata: {json.dumps(delta_event, ensure_ascii=False)}\n\n".encode("utf-8")
                                                    
                                                    # 解析 usage
                                                    if "usage" in chunk_json and chunk_json["usage"]:
                                                        total_tokens = chunk_json["usage"].get("total_tokens", 0)
                                                except:
                                                    pass
                            except Exception as e:
                                logger.warning(f"[Responses] 流式连接断开: {e}")
                            
                            # 发送 content_part.done 事件
                            content_part_done_event = {
                                "type": "response.content_part.done",
                                "sequence_number": seq_num,
                                "item_id": item_id,
                                "output_index": 0,
                                "content_index": 0,
                                "part": {
                                    "type": "output_text",
                                    "text": full_text
                                }
                            }
                            seq_num += 1
                            yield f"event: response.content_part.done\ndata: {json.dumps(content_part_done_event, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # 发送 output_item.done 事件
                            output_item_done_event = {
                                "type": "response.output_item.done",
                                "sequence_number": seq_num,
                                "item": {
                                    "id": item_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": full_text
                                        }
                                    ]
                                },
                                "output_index": 0
                            }
                            seq_num += 1
                            yield f"event: response.output_item.done\ndata: {json.dumps(output_item_done_event, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # 发送 response.completed 事件
                            completed_response = {
                                "id": resp_id,
                                "object": "response",
                                "created_at": int(time.time()),
                                "model": model_name,
                                "status": "completed",
                                "output": [
                                    {
                                        "id": item_id,
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": full_text
                                            }
                                        ]
                                    }
                                ],
                                "usage": {
                                    "input_tokens": 0,
                                    "output_tokens": 0,
                                    "total_tokens": total_tokens
                                }
                            }
                            completed_event = {
                                "type": "response.completed",
                                "sequence_number": seq_num,
                                "response": completed_response
                            }
                            seq_num += 1
                            yield f"event: response.completed\ndata: {json.dumps(completed_event, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # 发送结束信号
                            yield b'data: [DONE]\n\n'
                            
                            # 记录统计
                            elapsed_final = int((time.time() - start_time) * 1000)
                            await stats_manager.record_request(key, success=is_success, status_code=resp.status_code, tokens=total_tokens, elapsed_ms=elapsed_final, model=model_name)
                            await daily_stats.record(success=is_success, tokens=total_tokens)
                            await advanced_stats.record(resp.status_code, elapsed_final, model_name)
                            await add_recent_request(resp.status_code, model_name, total_tokens, elapsed_final, key)
                            return
                
                except httpx.TimeoutException:
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    await stats_manager.record_request(key, success=False, status_code=504, elapsed_ms=elapsed_ms, model=model_name)
                    await daily_stats.record(success=False)
                    await add_recent_request(504, model_name, 0, elapsed_ms, key)
                    
                    if attempt < MAX_RETRIES - 1:
                        continue
                    else:
                        yield f'data: {{"error": "Gateway Timeout", "message": "请求超时"}}\n\n'.encode("utf-8")
                        return
                
                except Exception as e:
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    logger.error(f"[Responses] 请求异常 | {key[:8]}... | {e}")
                    await stats_manager.record_request(key, success=False, status_code=502, elapsed_ms=elapsed_ms, model=model_name)
                    await daily_stats.record(success=False)
                    await add_recent_request(502, model_name, 0, elapsed_ms, key)
                    
                    if attempt < MAX_RETRIES - 1:
                        continue
                    else:
                        yield f'data: {{"error": "Bad Gateway", "message": "{str(e)}"}}\n\n'.encode("utf-8")
                        return
            
            yield f'data: {{"error": "Bad Gateway", "message": "所有重试均失败"}}\n\n'.encode("utf-8")
        
        return StreamingResponse(
            stream_responses_with_retry(),
            status_code=200,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    
    # 非流式请求
    # 尝试多个 Key
    for attempt in range(MAX_RETRIES):
        key = await key_manager.get_next_key()
        if not key:
            logger.error("[Responses] 所有 Key 都在冷却中或已禁用")
            return JSONResponse(status_code=503, content={"error": "Service Unavailable", "message": "所有 Key 都在冷却中或已禁用"})
        
        start_time = time.time()
        try:
            target_url = f"{TARGET_BASE_URL}/chat/completions"
            
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(target_url, headers=headers, content=completions_body)
            
            elapsed_ms = int((time.time() - start_time) * 1000)
            response_text = resp.text
            
            if resp.status_code == 429:
                limit_type = "rate"
                response_lower = response_text.lower()
                quota_signals = 0
                if "quota" in response_lower: quota_signals += 1
                if "insufficient" in response_lower or "exceeded" in response_lower: quota_signals += 1
                if "5小时" in response_text: quota_signals += 2
                if "rate" in response_lower and "limit" in response_lower: quota_signals -= 1
                
                if quota_signals >= 2:
                    limit_type = "quota"
                    await key_manager.set_cooling(key, QUOTA_LIMIT_COOLDOWN)
                else:
                    await key_manager.set_cooling(key, RATE_LIMIT_COOLDOWN)
                
                await stats_manager.record_request(key, success=False, status_code=429, elapsed_ms=elapsed_ms, limit_type=limit_type, model=model_name)
                await daily_stats.record(success=False)
                await add_recent_request(429, model_name, 0, elapsed_ms, key)
                
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                    continue
                else:
                    return JSONResponse(status_code=429, content={"error": "rate_limit", "message": response_text})
            
            if resp.status_code >= 500:
                await stats_manager.record_request(key, success=False, status_code=resp.status_code, elapsed_ms=elapsed_ms, model=model_name)
                await daily_stats.record(success=False)
                await add_recent_request(resp.status_code, model_name, 0, elapsed_ms, key)
                
                if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(0.5)
                    continue
                else:
                    return JSONResponse(status_code=resp.status_code, content={"error": "server_error", "message": response_text})
            
            # 成功 - 转换成 Responses 格式
            try:
                resp_json = resp.json()
                responses_json = convert_completions_to_responses(resp_json, model_name)
                tokens = responses_json.get("usage", {}).get("total_tokens", 0)
                
                is_success = 200 <= resp.status_code < 300
                await stats_manager.record_request(key, success=is_success, status_code=resp.status_code, tokens=tokens, elapsed_ms=elapsed_ms, model=model_name)
                await daily_stats.record(success=is_success, tokens=tokens)
                await advanced_stats.record(resp.status_code, elapsed_ms, model_name)
                await add_recent_request(resp.status_code, model_name, tokens, elapsed_ms, key)
                
                return JSONResponse(status_code=resp.status_code, content=responses_json)
            except Exception as e:
                logger.error(f"[Responses] 响应转换失败: {e}")
                return JSONResponse(status_code=500, content={"error": "Internal Error", "message": f"响应转换失败: {e}"})
        
        except httpx.TimeoutException:
            elapsed_ms = int((time.time() - start_time) * 1000)
            await stats_manager.record_request(key, success=False, status_code=504, elapsed_ms=elapsed_ms, model=model_name)
            await daily_stats.record(success=False)
            await add_recent_request(504, model_name, 0, elapsed_ms, key)
            
            if attempt < MAX_RETRIES - 1:
                continue
            else:
                return JSONResponse(status_code=504, content={"error": "Gateway Timeout", "message": "请求超时"})
        
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[Responses] 请求异常 | {key[:8]}... | {e}")
            await stats_manager.record_request(key, success=False, status_code=502, elapsed_ms=elapsed_ms, model=model_name)
            await daily_stats.record(success=False)
            await add_recent_request(502, model_name, 0, elapsed_ms, key)
            
            if attempt < MAX_RETRIES - 1:
                continue
            else:
                return JSONResponse(status_code=502, content={"error": "Bad Gateway", "message": str(e)})
    
    return JSONResponse(status_code=502, content={"error": "Bad Gateway", "message": "所有重试均失败"})

# ===== Anthropic Messages 格式支持 =====
def convert_anthropic_to_completions(req_json: dict) -> dict:
    """将 Anthropic Messages 格式的请求转换成 Completions 格式"""
    completions = {}
    
    # 模型名称
    if "model" in req_json:
        completions["model"] = req_json["model"]
    
    # 系统提示
    messages = []
    if "system" in req_json and req_json["system"]:
        system_content = req_json["system"]
        if isinstance(system_content, list):
            # 处理 system 是数组的情况
            text_parts = []
            for item in system_content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            system_content = "".join(text_parts)
        messages.append({"role": "system", "content": system_content})
    
    # 消息转换
    if "messages" in req_json:
        for msg in req_json["messages"]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            # 处理 content 是数组的情况
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_result":
                            # 工具调用结果
                            tool_content = item.get("content", "")
                            if isinstance(tool_content, list):
                                for tc in tool_content:
                                    if isinstance(tc, dict) and tc.get("type") == "text":
                                        text_parts.append(tc.get("text", ""))
                            else:
                                text_parts.append(str(tool_content))
                content = "".join(text_parts)
            
            # Anthropic 的 assistant 消息可能包含 tool_use
            if role == "assistant" and isinstance(msg.get("content"), list):
                tool_calls = []
                text_parts = []
                for item in msg["content"]:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_use":
                            tool_calls.append({
                                "id": item.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": item.get("name", ""),
                                    "arguments": json.dumps(item.get("input", {}), ensure_ascii=False)
                                }
                            })
                content = "".join(text_parts)
                new_msg = {"role": "assistant", "content": content}
                if tool_calls:
                    new_msg["tool_calls"] = tool_calls
                messages.append(new_msg)
            else:
                messages.append({"role": role, "content": content})
    
    completions["messages"] = messages
    
    # 流式
    if "stream" in req_json:
        completions["stream"] = req_json["stream"]
    
    # max_tokens
    if "max_tokens" in req_json:
        completions["max_tokens"] = req_json["max_tokens"]
    
    # 其他参数
    for param in ["temperature", "top_p", "top_k", "stop"]:
        if param in req_json:
            completions[param] = req_json[param]
    
    # tools 转换
    if "tools" in req_json and req_json["tools"]:
        openai_tools = []
        for tool in req_json["tools"]:
            if isinstance(tool, dict) and tool.get("name"):
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {})
                    }
                })
        if openai_tools:
            completions["tools"] = openai_tools
    
    return completions


def convert_completions_to_anthropic(resp_json: dict, model_name: str = "") -> dict:
    """将 Completions 格式的非流式响应转换成 Anthropic Messages 格式"""
    choices = resp_json.get("choices", [])
    content = []
    stop_reason = "end_turn"
    
    if choices:
        choice = choices[0]
        message = choice.get("message", {})
        text_content = message.get("content", "")
        
        if text_content:
            content.append({"type": "text", "text": text_content})
        
        # 处理 tool_calls
        tool_calls = message.get("tool_calls", [])
        for tc in tool_calls:
            func = tc.get("function", {})
            try:
                tool_input = json.loads(func.get("arguments", "{}"))
            except:
                tool_input = {}
            content.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "input": tool_input
            })
            stop_reason = "tool_use"
        
        if choice.get("finish_reason") == "stop":
            stop_reason = "end_turn"
    
    usage = resp_json.get("usage", {})
    anthropic_response = {
        "id": f"msg_{int(time.time())}_{uuid.uuid4().hex[:8]}",
        "type": "message",
        "role": "assistant",
        "model": model_name or resp_json.get("model", ""),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0)
        }
    }
    
    return anthropic_response


# ===== Anthropic Messages 代理端点 =====
@app.post("/v1/messages")
async def proxy_anthropic_messages(request: Request):
    """代理 Anthropic Messages 格式的请求，自动转换成 Completions 格式"""
    # 访问密码验证
    if not verify_access(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized", "message": "访问密码错误"})
    
    # 读取请求体
    body_bytes = await request.body()
    try:
        req_json = json.loads(body_bytes)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": "Bad Request", "message": f"无效的请求体: {e}"})
    
    model_name = req_json.get("model", "")
    is_stream = req_json.get("stream", False)
    
    # 转换成 Completions 格式
    completions_req = convert_anthropic_to_completions(req_json)
    
    # 自动限制 max_tokens
    MAX_ALLOWED_TOKENS = 8192
    if "max_tokens" in completions_req and completions_req["max_tokens"] > MAX_ALLOWED_TOKENS:
        completions_req["max_tokens"] = MAX_ALLOWED_TOKENS
    
    completions_body = json.dumps(completions_req, ensure_ascii=False).encode("utf-8")
    
    logger.info(f"[Anthropic] 收到请求 | 模型: {model_name} | 流式: {is_stream}")
    
    # 尝试多个 Key
    for attempt in range(MAX_RETRIES):
        key = await key_manager.get_next_key()
        if not key:
            return JSONResponse(status_code=503, content={"error": "Service Unavailable", "message": "所有 Key 都在冷却中或已禁用"})
        
        start_time = time.time()
        try:
            target_url = f"{TARGET_BASE_URL}/chat/completions"
            
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if is_stream else "application/json",
            }
            
            if is_stream:
                # 流式请求
                async def stream_anthropic():
                    async with httpx.AsyncClient(timeout=300.0) as client:
                        async with client.stream("POST", target_url, headers=headers, content=completions_body) as resp:
                            if resp.status_code != 200:
                                error_text = (await resp.aread()).decode("utf-8", errors="ignore")
                                yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': error_text}}, ensure_ascii=False)}\n\n".encode("utf-8")
                                return
                            
                            msg_id = f"msg_{int(time.time())}_{uuid.uuid4().hex[:8]}"
                            full_text = ""
                            total_tokens = 0
                            
                            # message_start
                            start_event = {
                                "type": "message_start",
                                "message": {
                                    "id": msg_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "model": model_name,
                                    "content": [],
                                    "stop_reason": None,
                                    "stop_sequence": None,
                                    "usage": {"input_tokens": 0, "output_tokens": 0}
                                }
                            }
                            yield f"event: message_start\ndata: {json.dumps(start_event, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # content_block_start
                            block_start = {
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""}
                            }
                            yield f"event: content_block_start\ndata: {json.dumps(block_start, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            try:
                                async for chunk in resp.aiter_bytes():
                                    if chunk:
                                        chunk_str = chunk.decode("utf-8", errors="ignore")
                                        for line in chunk_str.split("\n"):
                                            line = line.strip()
                                            if line.startswith("data:"):
                                                data_str = line[5:].strip()
                                                if data_str == "[DONE]":
                                                    continue
                                                try:
                                                    chunk_json = json.loads(data_str)
                                                    choices = chunk_json.get("choices", [])
                                                    if choices:
                                                        delta = choices[0].get("delta", {})
                                                        content = delta.get("content", "")
                                                        if content:
                                                            full_text += content
                                                            delta_event = {
                                                                "type": "content_block_delta",
                                                                "index": 0,
                                                                "delta": {"type": "text_delta", "text": content}
                                                            }
                                                            yield f"event: content_block_delta\ndata: {json.dumps(delta_event, ensure_ascii=False)}\n\n".encode("utf-8")
                                                    
                                                    # 解析 usage
                                                    if "usage" in chunk_json and chunk_json["usage"]:
                                                        total_tokens = chunk_json["usage"].get("total_tokens", 0)
                                                except:
                                                    pass
                            except Exception as e:
                                logger.warning(f"[Anthropic] 流式连接断开: {e}")
                            
                            # content_block_stop
                            block_stop = {"type": "content_block_stop", "index": 0}
                            yield f"event: content_block_stop\ndata: {json.dumps(block_stop, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # message_delta
                            msg_delta = {
                                "type": "message_delta",
                                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": total_tokens}
                            }
                            yield f"event: message_delta\ndata: {json.dumps(msg_delta, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # message_stop
                            msg_stop = {"type": "message_stop"}
                            yield f"event: message_stop\ndata: {json.dumps(msg_stop, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # 记录统计
                            elapsed_final = int((time.time() - start_time) * 1000)
                            await stats_manager.record_request(key, success=True, status_code=200, tokens=total_tokens, elapsed_ms=elapsed_final, model=model_name)
                            await daily_stats.record(success=True, tokens=total_tokens)
                            await advanced_stats.record(200, elapsed_final, model_name)
                            await add_recent_request(200, model_name, total_tokens, elapsed_final, key)
                
                return StreamingResponse(
                    stream_anthropic(),
                    status_code=200,
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    }
                )
            else:
                # 非流式请求
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(target_url, headers=headers, content=completions_body)
                
                elapsed_ms = int((time.time() - start_time) * 1000)
                response_text = resp.text
                
                if resp.status_code == 429:
                    limit_type = "rate"
                    response_lower = response_text.lower()
                    quota_signals = 0
                    if "quota" in response_lower: quota_signals += 1
                    if "insufficient" in response_lower or "exceeded" in response_lower: quota_signals += 1
                    if "5小时" in response_text: quota_signals += 2
                    if "rate" in response_lower and "limit" in response_lower: quota_signals -= 1
                    
                    if quota_signals >= 2:
                        limit_type = "quota"
                        await key_manager.set_cooling(key, QUOTA_LIMIT_COOLDOWN)
                    else:
                        await key_manager.set_cooling(key, RATE_LIMIT_COOLDOWN)
                    
                    await stats_manager.record_request(key, success=False, status_code=429, elapsed_ms=elapsed_ms, limit_type=limit_type, model=model_name)
                    await daily_stats.record(success=False)
                    await add_recent_request(429, model_name, 0, elapsed_ms, key)
                    
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(1)
                        continue
                    else:
                        return JSONResponse(status_code=429, content={"error": "rate_limit", "message": response_text})
                
                if resp.status_code >= 500:
                    await stats_manager.record_request(key, success=False, status_code=resp.status_code, elapsed_ms=elapsed_ms, model=model_name)
                    await daily_stats.record(success=False)
                    await add_recent_request(resp.status_code, model_name, 0, elapsed_ms, key)
                    
                    if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        return JSONResponse(status_code=resp.status_code, content={"error": "server_error", "message": response_text})
                
                # 成功 - 转换成 Anthropic 格式
                try:
                    resp_json = resp.json()
                    anthropic_json = convert_completions_to_anthropic(resp_json, model_name)
                    tokens = anthropic_json.get("usage", {}).get("output_tokens", 0)
                    
                    await stats_manager.record_request(key, success=True, status_code=resp.status_code, tokens=tokens, elapsed_ms=elapsed_ms, model=model_name)
                    await daily_stats.record(success=True, tokens=tokens)
                    await advanced_stats.record(resp.status_code, elapsed_ms, model_name)
                    await add_recent_request(resp.status_code, model_name, tokens, elapsed_ms, key)
                    
                    return JSONResponse(status_code=resp.status_code, content=anthropic_json)
                except Exception as e:
                    logger.error(f"[Anthropic] 响应转换失败: {e}")
                    return JSONResponse(status_code=500, content={"error": "Internal Error", "message": f"响应转换失败: {e}"})
        
        except httpx.TimeoutException:
            elapsed_ms = int((time.time() - start_time) * 1000)
            await stats_manager.record_request(key, success=False, status_code=504, elapsed_ms=elapsed_ms, model=model_name)
            await daily_stats.record(success=False)
            await add_recent_request(504, model_name, 0, elapsed_ms, key)
            
            if attempt < MAX_RETRIES - 1:
                continue
            else:
                return JSONResponse(status_code=504, content={"error": "Gateway Timeout", "message": "请求超时"})
        
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[Anthropic] 请求异常 | {key[:8]}... | {e}")
            await stats_manager.record_request(key, success=False, status_code=502, elapsed_ms=elapsed_ms, model=model_name)
            await daily_stats.record(success=False)
            await add_recent_request(502, model_name, 0, elapsed_ms, key)
            
            if attempt < MAX_RETRIES - 1:
                continue
            else:
                return JSONResponse(status_code=502, content={"error": "Bad Gateway", "message": str(e)})
    
    return JSONResponse(status_code=502, content={"error": "Bad Gateway", "message": "所有重试均失败"})

# ===== 代理转发 =====
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def forward_request(path: str, request: Request) -> Response:
    # 访问密码验证
    if not verify_access(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized", "message": "访问密码错误"})

    # 读取请求体
    body_bytes = await request.body()
    model_name = ""
    is_stream = False
    try:
        if body_bytes:
            req_json = json.loads(body_bytes)
            model_name = req_json.get("model", "")
            is_stream = req_json.get("stream", False)
            # 自动限制 max_tokens，避免过大导致响应时间过长
            MAX_ALLOWED_TOKENS = 8192
            if "max_tokens" in req_json and req_json["max_tokens"] > MAX_ALLOWED_TOKENS:
                logger.info(f"自动限制 max_tokens: {req_json['max_tokens']} -> {MAX_ALLOWED_TOKENS}")
                req_json["max_tokens"] = MAX_ALLOWED_TOKENS
                body_bytes = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
            # 记录请求参数
            logger.info(f"收到请求 | 模型: {model_name} | 流式: {is_stream} | 参数: {json.dumps({k: v for k, v in req_json.items() if k != 'messages'}, ensure_ascii=False)[:300]}")
            
            # Function Calling 模拟（用于 DeepWrite 联网搜索）
            # 只在请求中明确包含 web_search 工具时才触发
            has_web_search_tool = False
            search_tool_name = "web_search"
            if ENABLE_WEB_SEARCH and "tools" in req_json and req_json["tools"]:
                for tool in req_json["tools"]:
                    if isinstance(tool, dict) and tool.get("type") == "function":
                        func = tool.get("function", {})
                        func_name = func.get("name", "").lower()
                        if "search" in func_name or "web" in func_name or "browser" in func_name:
                            has_web_search_tool = True
                            search_tool_name = func.get("name", "web_search")
                            break
            
            if has_web_search_tool:
                has_tool_message = any(msg.get("role") == "tool" for msg in req_json.get("messages", []))
                
                if not has_tool_message:
                    # 第一次请求：包含 web_search 工具，没有 tool message
                    # 直接返回伪造的 tool_calls，让 DeepWrite 执行搜索
                    user_query = ""
                    for msg in reversed(req_json.get("messages", [])):
                        if msg.get("role") == "user":
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                user_query = content
                            elif isinstance(content, list):
                                text_parts = []
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        text_parts.append(c.get("text", ""))
                                user_query = "".join(text_parts)
                            break
                    
                    if user_query and len(user_query) > 1:
                        # 返回伪造的 tool_calls
                        tool_call_id = f"call_{int(time.time())}_{uuid.uuid4().hex[:8]}"
                        
                        if is_stream:
                            # 流式响应格式
                            async def fake_tool_calls_stream():
                                # 发送一个包含 tool_calls 的 chunk
                                chunk_data = {
                                    "id": f"chatcmpl_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": model_name,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "role": "assistant",
                                                "content": None,
                                                "tool_calls": [
                                                    {
                                                        "index": 0,
                                                        "id": tool_call_id,
                                                        "type": "function",
                                                        "function": {
                                                            "name": search_tool_name,
                                                            "arguments": json.dumps({"query": user_query}, ensure_ascii=False)
                                                        }
                                                    }
                                                ]
                                            },
                                            "finish_reason": "tool_calls"
                                        }
                                    ]
                                }
                                yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n".encode("utf-8")
                                yield b'data: [DONE]\n\n'
                            
                            logger.info(f"Function Calling 模拟 | 返回伪造的 tool_calls（流式）| 工具: {search_tool_name} | 查询: {user_query[:50]}")
                            return StreamingResponse(
                                fake_tool_calls_stream(),
                                status_code=200,
                                media_type="text/event-stream",
                                headers={
                                    "Cache-Control": "no-cache",
                                    "Connection": "keep-alive",
                                    "X-Accel-Buffering": "no",
                                }
                            )
                        else:
                            # 非流式响应格式
                            fake_response = {
                                "id": f"chatcmpl_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                                "object": "chat.completion",
                                "created": int(time.time()),
                                "model": model_name,
                                "choices": [
                                    {
                                        "index": 0,
                                        "message": {
                                            "role": "assistant",
                                            "content": None,
                                            "tool_calls": [
                                                {
                                                    "id": tool_call_id,
                                                    "type": "function",
                                                    "function": {
                                                        "name": search_tool_name,
                                                        "arguments": json.dumps({"query": user_query}, ensure_ascii=False)
                                                    }
                                                }
                                            ]
                                        },
                                        "finish_reason": "tool_calls"
                                    }
                                ],
                                "usage": {
                                    "prompt_tokens": 100,
                                    "completion_tokens": 20,
                                    "total_tokens": 120
                                }
                            }
                            
                            logger.info(f"Function Calling 模拟 | 返回伪造的 tool_calls（非流式）| 工具: {search_tool_name} | 查询: {user_query[:50]}")
                            return JSONResponse(status_code=200, content=fake_response)
                
                else:
                    # 第二次请求：包含 tools，有 tool message
                    # 把搜索结果提取出来，作为系统提示添加到请求中
                    search_results = []
                    for msg in req_json.get("messages", []):
                        if msg.get("role") == "tool":
                            content = msg.get("content", "")
                            if content:
                                search_results.append(content)
                    
                    if search_results:
                        search_text = "【联网搜索结果】\n\n" + "\n\n".join(search_results)
                        
                        # 把搜索结果作为系统提示添加到请求中
                        has_system = False
                        for msg in req_json["messages"]:
                            if msg.get("role") == "system":
                                msg["content"] = msg.get("content", "") + "\n\n" + search_text
                                has_system = True
                                break
                        
                        if not has_system:
                            req_json["messages"].insert(0, {
                                "role": "system",
                                "content": search_text
                            })
                        
                        logger.info(f"Function Calling 模拟 | 搜索结果已添加到请求中 | 共 {len(search_results)} 条搜索结果")
                    
                    # 去掉 tools，正常发送给商汤 API
                    req_json.pop("tools", None)
                    req_json.pop("tool_choice", None)
                    body_bytes = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
                    logger.info("Function Calling 模拟 | 已移除 tools，继续正常请求")
            
            # 联网搜索功能（简单版，用于不支持 function calling 的客户端）
            if ENABLE_WEB_SEARCH and "messages" in req_json and "tools" not in req_json:
                # 提取最后一条用户消息
                user_query = ""
                for msg in reversed(req_json["messages"]):
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            user_query = content
                        elif isinstance(content, list):
                            # 处理 content 是数组的情况
                            text_parts = []
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text_parts.append(c.get("text", ""))
                            user_query = "".join(text_parts)
                        break
                
                # 只在用户问题比较短时才搜索（小于200字符），避免搜索长篇上下文
                if user_query and 2 < len(user_query) < 200:
                    logger.info(f"联网搜索 | 查询: {user_query[:50]}...")
                    search_results = await web_search(user_query, WEB_SEARCH_RESULTS)
                    if search_results:
                        # 把搜索结果作为系统提示添加到请求中
                        has_system = False
                        for msg in req_json["messages"]:
                            if msg.get("role") == "system":
                                msg["content"] = msg.get("content", "") + "\n\n" + search_results
                                has_system = True
                                break
                        
                        if not has_system:
                            # 在消息列表开头添加系统提示
                            req_json["messages"].insert(0, {
                                "role": "system",
                                "content": search_results
                            })
                        
                        body_bytes = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
                        logger.info("联网搜索 | 搜索结果已添加到请求中")
    except Exception as e:
        logger.warning(f"解析请求体失败: {e}")

    # 尝试多个 Key
    last_error = None
    for attempt in range(MAX_RETRIES):
        key = await key_manager.get_next_key()
        if not key:
            logger.error("所有 Key 都在冷却中或已禁用")
            return JSONResponse(status_code=503, content={"error": "Service Unavailable", "message": "所有 Key 都在冷却中或已禁用"})

        start_time = time.time()
        try:
            # 构建目标 URL
            target_url = f"{TARGET_BASE_URL}/{path}"
            if request.query_params:
                target_url += "?" + str(request.query_params)

            # 构建请求头
            headers = {
                "Authorization": f"Bearer {key}",
                "Accept": request.headers.get("accept", "application/json"),
                "User-Agent": request.headers.get("user-agent", "api-key-proxy/1.0"),
            }
            if request.method in ["POST", "PUT", "PATCH"]:
                content_type = request.headers.get("content-type", "application/json")
                headers["Content-Type"] = content_type

            # 发送请求
            request_kwargs = {
                "method": request.method,
                "url": target_url,
                "headers": headers,
            }
            if request.method != "GET" and body_bytes:
                request_kwargs["content"] = body_bytes

            # ===== 流式请求处理 =====
            if is_stream and request.method == "POST":
                # 构建不包含 Authorization 的请求参数基础
                request_kwargs_base = {
                    "method": request.method,
                    "url": target_url,
                    "headers": {
                        "Accept": request.headers.get("accept", "application/json"),
                        "User-Agent": request.headers.get("user-agent", "api-key-proxy/1.0"),
                        "Content-Type": request.headers.get("content-type", "application/json"),
                    },
                }
                if request.method != "GET" and body_bytes:
                    request_kwargs_base["content"] = body_bytes
                
                # 直接返回流式响应，重试逻辑在 stream_proxy_with_retry 中处理
                return StreamingResponse(
                    stream_proxy_with_retry(path, request, body_bytes, model_name, is_stream, request_kwargs_base),
                    status_code=200,
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    }
                )

            # ===== 非流式请求处理 =====
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.request(**request_kwargs)

            elapsed_ms = int((time.time() - start_time) * 1000)
            tokens = 0
            response_text = resp.text

            # 解析 Token 使用量
            try:
                resp_json = resp.json()
                if "usage" in resp_json:
                    tokens = resp_json["usage"].get("total_tokens", 0)
            except:
                pass

            # 判断是否限流
            if resp.status_code == 429:
                # 保守策略：默认判定为频率限流（只冷却60秒）
                # 只有在非常确定是配额耗尽的情况下才判定为配额限流（5小时）
                limit_type = "rate"
                response_lower = response_text.lower()
                
                # 配额耗尽的明确信号（需要同时满足多个条件，避免误判）
                quota_signals = 0
                if "quota" in response_lower:
                    quota_signals += 1
                if "insufficient" in response_lower or "exceeded" in response_lower:
                    quota_signals += 1
                if "5小时" in response_text or "5 hour" in response_lower or "300分钟" in response_text:
                    quota_signals += 2  # 明确提到5小时，权重更高
                if "rate" in response_lower and "limit" in response_lower:
                    quota_signals -= 1  # 明确提到频率限制，降低配额判断
                
                # 只有配额信号 >= 2 才判定为配额限流，否则都按频率限流处理
                if quota_signals >= 2:
                    limit_type = "quota"
                    await key_manager.set_cooling(key, QUOTA_LIMIT_COOLDOWN)
                    logger.warning(f"判定为配额限流 | Key: {key[:8]}... | 信号分: {quota_signals} | 响应: {response_text[:200]}")
                else:
                    await key_manager.set_cooling(key, RATE_LIMIT_COOLDOWN)
                    logger.info(f"判定为频率限流 | Key: {key[:8]}... | 信号分: {quota_signals} | 响应: {response_text[:200]}")

                await stats_manager.record_request(key, success=False, status_code=429, elapsed_ms=elapsed_ms, limit_type=limit_type, model=model_name)
                await daily_stats.record(success=False)
                await advanced_stats.record(429, elapsed_ms, model_name, is_rate_limit=True)
                await add_recent_request(429, model_name, 0, elapsed_ms, key, body_bytes.decode("utf-8", errors="ignore")[:500], response_text[:500])
                await add_action_log("限流", f"Key {key[:8]}... 遇到429（{limit_type}限流），冷却{QUOTA_LIMIT_COOLDOWN if limit_type=='quota' else RATE_LIMIT_COOLDOWN}秒")

                last_error = resp
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                    continue
                else:
                    return Response(content=response_text, status_code=resp.status_code, headers=dict(resp.headers), media_type=resp.headers.get("content-type"))

            # 其他错误状态码
            if resp.status_code >= 500:
                await stats_manager.record_request(key, success=False, status_code=resp.status_code, elapsed_ms=elapsed_ms, model=model_name)
                await daily_stats.record(success=False)
                await advanced_stats.record(resp.status_code, elapsed_ms, model_name)
                await add_recent_request(resp.status_code, model_name, 0, elapsed_ms, key, body_bytes.decode("utf-8", errors="ignore")[:500], response_text[:500])
                last_error = resp
                if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(0.5)
                    continue
                else:
                    return Response(content=response_text, status_code=resp.status_code, headers=dict(resp.headers), media_type=resp.headers.get("content-type"))

            # 成功
            is_success = 200 <= resp.status_code < 300
            await stats_manager.record_request(key, success=is_success, status_code=resp.status_code, tokens=tokens, elapsed_ms=elapsed_ms, model=model_name)
            await daily_stats.record(success=is_success, tokens=tokens)
            await advanced_stats.record(resp.status_code, elapsed_ms, model_name)
            await add_recent_request(resp.status_code, model_name, tokens, elapsed_ms, key, body_bytes.decode("utf-8", errors="ignore")[:500], response_text[:500])

            # 移除可能的 hop-by-hop 头
            response_headers = dict(resp.headers)
            for h in ["content-encoding", "transfer-encoding", "connection"]:
                response_headers.pop(h, None)

            # 对 /v1/models 请求的特殊处理：转换成 DeepSeek/OpenAI 格式，添加 web_search 支持
            if path == "models":
                try:
                    models_json = json.loads(response_text)
                    if "data" in models_json and isinstance(models_json["data"], list):
                        # 转换成简单的 OpenAI/DeepSeek 格式
                        simple_models = []
                        for model_info in models_json["data"]:
                            model_id = model_info.get("id", "unknown")
                            simple_model = {
                                "id": model_id,
                                "object": "model",
                                "created": model_info.get("created", int(time.time())),
                                "owned_by": "deepseek",
                                "permission": [
                                    {
                                        "id": f"perm_{model_id}",
                                        "object": "model_permission",
                                        "created": model_info.get("created", int(time.time())),
                                        "allow_create_engine": False,
                                        "allow_sampling": True,
                                        "allow_logprobs": False,
                                        "allow_search_indices": False,
                                        "allow_view": True,
                                        "allow_fine_tuning": False,
                                        "organization": "*",
                                        "group": None,
                                        "is_blocking": False
                                    }
                                ],
                                "root": model_id,
                                "parent": None
                            }
                            # 添加支持的功能标识
                            if ENABLE_WEB_SEARCH:
                                simple_model["supported_features"] = ["tools", "web_search", "json_mode", "reasoning"]
                            simple_models.append(simple_model)
                        
                        models_json["data"] = simple_models
                        response_text = json.dumps(models_json, ensure_ascii=False)
                        
                        # 添加 DeepSeek 标识的响应头
                        response_headers["Server"] = "deepseek"
                        response_headers["X-Provider"] = "deepseek"
                        
                        logger.info("已将 models 响应转换为 DeepSeek/OpenAI 格式，并添加 web_search 支持")
                except Exception as e:
                    logger.warning(f"修改 models 响应失败: {e}")

            return Response(content=response_text, status_code=resp.status_code, headers=response_headers, media_type=resp.headers.get("content-type"))

        except httpx.TimeoutException:
            elapsed_ms = int((time.time() - start_time) * 1000)
            await stats_manager.record_request(key, success=False, status_code=504, elapsed_ms=elapsed_ms, model=model_name)
            await daily_stats.record(success=False)
            await advanced_stats.record(504, elapsed_ms, model_name)
            await add_recent_request(504, model_name, 0, elapsed_ms, key)
            last_error = "timeout"
            if attempt < MAX_RETRIES - 1:
                continue
            return JSONResponse(status_code=504, content={"error": "Gateway Timeout", "message": "请求超时"})

        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.error(f"请求异常 | {key[:8]}... | {e}")
            await stats_manager.record_request(key, success=False, status_code=502, elapsed_ms=elapsed_ms, model=model_name)
            await daily_stats.record(success=False)
            await advanced_stats.record(502, elapsed_ms, model_name)
            await add_recent_request(502, model_name, 0, elapsed_ms, key)
            last_error = str(e)
            if attempt < MAX_RETRIES - 1:
                continue
            return JSONResponse(status_code=502, content={"error": "Bad Gateway", "message": str(e)})

    return JSONResponse(status_code=502, content={"error": "Bad Gateway", "message": "所有重试均失败"})

# ===== 兼容没有 /v1 前缀的请求（自动重定向） =====
@app.api_route("/models", methods=["GET", "POST"])
async def redirect_models(request: Request):
    """自动重定向 /models 到 /v1/models"""
    target_url = "/v1/models"
    if request.query_params:
        target_url += "?" + str(request.query_params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=target_url, status_code=307)

@app.api_route("/chat/completions", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def redirect_chat_completions(request: Request):
    """自动重定向 /chat/completions 到 /v1/chat/completions"""
    body = await request.body()
    # 直接转发到 /v1/chat/completions
    from fastapi.responses import Response as FastAPIResponse
    # 构建新的请求
    headers = dict(request.headers)
    # 移除 host 头，避免冲突
    headers.pop("host", None)
    
    # 尝试多个 Key
    for attempt in range(MAX_RETRIES):
        key = await key_manager.get_next_key()
        if not key:
            return JSONResponse(status_code=503, content={"error": "Service Unavailable", "message": "所有 Key 都在冷却中或已禁用"})
        
        try:
            target_url = f"{TARGET_BASE_URL}/chat/completions"
            req_headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(target_url, headers=req_headers, content=body)
            
            response_headers = dict(resp.headers)
            for h in ["content-encoding", "transfer-encoding", "connection"]:
                response_headers.pop(h, None)
            
            return FastAPIResponse(content=resp.content, status_code=resp.status_code, headers=response_headers, media_type=resp.headers.get("content-type"))
        
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                continue
            return JSONResponse(status_code=502, content={"error": "Bad Gateway", "message": str(e)})
    
    return JSONResponse(status_code=502, content={"error": "Bad Gateway", "message": "所有重试均失败"})

# ===== 仪表盘页面 =====
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML

# ===== 统计接口 =====
@app.get("/stats")
async def get_stats():
    return stats_manager.get_stats()

@app.get("/api/total-stats")
async def get_total_stats():
    all_daily = daily_stats.stats
    total_requests = sum(d.get("requests", 0) for d in all_daily.values())
    total_success = sum(d.get("success", 0) for d in all_daily.values())
    total_failed = sum(d.get("failed", 0) for d in all_daily.values())
    total_tokens = sum(d.get("tokens", 0) for d in all_daily.values())

    key_usage = []
    total_model_usage = {}
    for key in API_KEYS:
        stat = stats_manager.stats.get(key, {})
        model_usage = stat.get("model_usage", {})
        for model, usage in model_usage.items():
            if model not in total_model_usage:
                total_model_usage[model] = {"requests": 0, "tokens": 0}
            total_model_usage[model]["requests"] += usage.get("requests", 0)
            total_model_usage[model]["tokens"] += usage.get("tokens", 0)
        key_usage.append({
            "key": key[:8] + "..." if len(key) > 8 else key,
            "key_full": key,
            "remark": KEY_REMARKS.get(key, ""),
            "requests": stat.get("total_requests", 0),
            "success": stat.get("success", 0),
            "failed": stat.get("failed", 0),
            "tokens": stat.get("total_tokens", 0),
            "rate_limited": stat.get("rate_limited", 0),
            "quota_limited": stat.get("quota_limited", 0),
            "model_usage": model_usage,
        })

    return {
        "total": {
            "requests": total_requests,
            "success": total_success,
            "failed": total_failed,
            "tokens": total_tokens,
            "success_rate": round(total_success / total_requests * 100, 1) if total_requests > 0 else 0,
        },
        "key_usage": key_usage,
        "total_model_usage": total_model_usage,
        "days_recorded": len(all_daily),
    }

@app.get("/api/daily-stats")
async def get_daily_stats(days: int = 30):
    return {"daily": daily_stats.get_daily(days)}

@app.get("/api/advanced-stats")
async def get_advanced_stats():
    return advanced_stats.get_all()

@app.get("/api/recent-requests")
async def get_recent_requests(limit: int = 50):
    return {"requests": recent_requests[:limit]}

@app.get("/api/request-details/{req_id}")
async def get_request_detail(req_id: int):
    for req in recent_requests:
        if req["id"] == req_id:
            return {"status": "ok", "detail": req}
    return {"status": "error", "message": "未找到"}

@app.get("/api/actions")
async def get_actions():
    return {"actions": action_logs[:100]}

# ===== Key 管理接口 =====
@app.get("/api/keys")
async def get_keys():
    return {"keys": [{"index": i, "key": k, "priority": KEY_PRIORITIES.get(k, 0)} for i, k in enumerate(API_KEYS)]}

@app.post("/api/keys")
async def add_key(request: Request):
    data = await request.json()
    key = data.get("key", "").strip()
    priority = data.get("priority", 0)
    remark = data.get("remark", "").strip()
    if not key:
        return {"status": "error", "message": "Key 不能为空"}
    success = await key_manager.add_key(key, priority, remark)
    if success:
        stats_manager.ensure_key(key)
        await add_action_log("添加Key", f"添加了 Key {key[:8]}... 备注: {remark}")
        return {"status": "ok", "total_keys": len(API_KEYS)}
    return {"status": "error", "message": "Key 已存在"}

@app.post("/api/keys/{index}/remark")
async def set_key_remark(index: int, request: Request):
    data = await request.json()
    remark = data.get("remark", "").strip()
    if 0 <= index < len(API_KEYS):
        key = API_KEYS[index]
        await key_manager.set_remark(key, remark)
        await add_action_log("修改备注", f"Key {key[:8]}... 的备注修改为: {remark}")
        return {"status": "ok"}
    return {"status": "error", "message": "索引无效"}

@app.delete("/api/keys/{index}")
async def delete_key(index: int):
    success = await key_manager.remove_key(index)
    if success:
        await add_action_log("删除Key", f"删除了第 {index+1} 个 Key")
        return {"status": "ok", "total_keys": len(API_KEYS)}
    return {"status": "error", "message": "索引无效"}

@app.post("/api/keys/{index}/unlock")
async def unlock_key(index: int):
    if 0 <= index < len(API_KEYS):
        await key_manager.unlock(API_KEYS[index])
        await add_action_log("解锁Key", f"手动解锁了第 {index+1} 个 Key")
        return {"status": "ok"}
    return {"status": "error", "message": "索引无效"}

@app.post("/api/keys/{index}/disable")
async def disable_key(index: int):
    if 0 <= index < len(API_KEYS):
        await key_manager.disable(API_KEYS[index])
        await add_action_log("禁用Key", f"禁用了第 {index+1} 个 Key")
        return {"status": "ok"}
    return {"status": "error", "message": "索引无效"}

@app.post("/api/keys/{index}/enable")
async def enable_key(index: int):
    if 0 <= index < len(API_KEYS):
        await key_manager.enable(API_KEYS[index])
        await add_action_log("启用Key", f"启用了第 {index+1} 个 Key")
        return {"status": "ok"}
    return {"status": "error", "message": "索引无效"}

@app.post("/api/keys/reorder")
async def reorder_keys(request: Request):
    data = await request.json()
    new_order = data.get("order", [])
    await key_manager.reorder(new_order)
    await add_action_log("排序", "重新排序了 Key 列表")
    return {"status": "ok"}

@app.post("/api/keys/unlock-all")
async def unlock_all():
    for key in API_KEYS:
        await key_manager.unlock(key)
    await add_action_log("全部解锁", "手动解锁了所有 Key")
    return {"status": "ok"}

# ===== 测试接口 =====
@app.post("/test")
async def test_key(request: Request):
    data = await request.json()
    index = data.get("index", -1)
    model = data.get("model", "glm-5.2")
    prompt = data.get("prompt", "你好，请用一句话介绍你自己。")

    if index >= 0 and index < len(API_KEYS):
        key = API_KEYS[index]
    else:
        key = await key_manager.get_next_key()
        if not key:
            return {"status": "error", "message": "没有可用的 Key"}

    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{TARGET_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 100},
            )
        elapsed_ms = int((time.time() - start_time) * 1000)
        tokens = 0
        response_text = ""
        try:
            resp_json = resp.json()
            if "usage" in resp_json:
                tokens = resp_json["usage"].get("total_tokens", 0)
            if "choices" in resp_json and len(resp_json["choices"]) > 0:
                response_text = resp_json["choices"][0].get("message", {}).get("content", "")
        except:
            response_text = resp.text[:200]

        if resp.status_code == 200:
            await stats_manager.record_request(key, success=True, status_code=200, tokens=tokens, elapsed_ms=elapsed_ms, model=model)
            await daily_stats.record(success=True, tokens=tokens)
            return {"status": "ok", "model": model, "elapsed_ms": elapsed_ms, "tokens": tokens, "response": response_text}
        else:
            await stats_manager.record_request(key, success=False, status_code=resp.status_code, elapsed_ms=elapsed_ms, model=model)
            return {"status": "error", "message": f"状态码 {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        await stats_manager.record_request(key, success=False, status_code=500, elapsed_ms=elapsed_ms, model=model)
        return {"status": "error", "message": str(e)}

@app.post("/api/reload-config")
async def reload_config():
    global config, API_KEYS, TARGET_BASE_URL, RATE_LIMIT_COOLDOWN, QUOTA_LIMIT_COOLDOWN, ACCESS_KEY
    config = load_config()
    API_KEYS = config.get("api_keys", [])
    TARGET_BASE_URL = config.get("target_base_url", "https://token.sensenova.cn/v1")
    RATE_LIMIT_COOLDOWN = config.get("rate_limit_cooldown", 60)
    QUOTA_LIMIT_COOLDOWN = config.get("quota_limit_cooldown", 18000)
    ACCESS_KEY = config.get("access_key", "")
    # 重新加载备注
    _remarks_config = config.get("key_remarks", {})
    KEY_REMARKS.clear()
    if _remarks_config:
        for _k, _v in _remarks_config.items():
            KEY_REMARKS[_k] = _v
    key_manager.keys = API_KEYS
    stats_manager.keys = API_KEYS
    for key in API_KEYS:
        stats_manager.ensure_key(key)
    await add_action_log("重载配置", "重新加载了配置文件")
    return {"status": "ok", "message": "配置已重载", "key_count": len(API_KEYS)}

# ===== 仪表盘 HTML =====
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>API Key 轮询代理</title>
<style>
:root {
  --bg: #f8fafc;
  --bg2: #f1f5f9;
  --card: #ffffff;
  --text: #1e293b;
  --text2: #64748b;
  --text3: #94a3b8;
  --border: #e2e8f0;
  --primary: #6366f1;
  --success: #10b981;
  --danger: #ef4444;
  --warning: #f59e0b;
  --info: #3b82f6;
}
[data-theme="dark"] {
  --bg: #0f172a;
  --bg2: #1e293b;
  --card: #1e293b;
  --text: #f1f5f9;
  --text2: #94a3b8;
  --text3: #64748b;
  --border: #334155;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); padding-top: 70px; }
.navbar { position: fixed; top: 0; left: 0; right: 0; height: 60px; background: var(--card); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 24px; z-index: 100; }
.navbar-title { font-size: 18px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
.navbar-status { font-size: 13px; color: var(--text2); }
.navbar-actions { display: flex; gap: 8px; }
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }
.overview-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
.overview-card { background: var(--card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); }
.overview-card .label { font-size: 13px; color: var(--text2); margin-bottom: 8px; }
.overview-card .value { font-size: 28px; font-weight: 700; }
.overview-card .sub { font-size: 12px; color: var(--text3); margin-top: 4px; }
.collapsible { background: var(--card); border-radius: 12px; border: 1px solid var(--border); margin-bottom: 16px; overflow: hidden; }
.collapsible-header { padding: 16px 20px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; font-weight: 600; }
.collapsible-header:hover { background: var(--bg2); }
.collapsible-body { padding: 0 20px 20px; }
.collapsible.collapsed .collapsible-body { display: none; }
.btn { padding: 8px 16px; border: none; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 500; transition: all 0.2s; }
.btn:hover { opacity: 0.9; transform: translateY(-1px); }
.btn-primary { background: var(--primary); color: white; }
.btn-success { background: var(--success); color: white; }
.btn-danger { background: var(--danger); color: white; }
.btn-warning { background: var(--warning); color: white; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.key-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 12px; overflow: hidden; transition: all 0.3s; }
.key-card.cooling { border-left: 4px solid var(--warning); }
.key-card.disabled { border-left: 4px solid var(--text3); opacity: 0.7; }
.key-card.highlight { border-color: var(--primary); box-shadow: 0 0 20px rgba(99, 102, 241, 0.3); }
.key-card-header { padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; }
.key-card-left { display: flex; align-items: center; gap: 10px; }
.key-badge { font-size: 12px; padding: 2px 8px; border-radius: 4px; font-weight: 500; }
.key-badge.active { background: rgba(16, 185, 129, 0.1); color: var(--success); }
.key-badge.cooling { background: rgba(245, 158, 11, 0.1); color: var(--warning); }
.key-badge.disabled { background: rgba(148, 163, 184, 0.1); color: var(--text3); }
.key-value { font-family: monospace; font-size: 13px; }
.key-remark { font-size: 12px; color: var(--primary); background: rgba(99, 102, 241, 0.1); padding: 2px 8px; border-radius: 4px; cursor: pointer; }
.key-remark:hover { background: rgba(99, 102, 241, 0.2); }
.key-card-stats { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; padding: 0 16px 12px; }
.key-stat { text-align: center; }
.key-stat-value { font-size: 18px; font-weight: 600; }
.key-stat-label { font-size: 11px; color: var(--text3); }
.key-card-actions { display: flex; gap: 6px; }
.drag-handle { cursor: grab; color: var(--text3); user-select: none; }
.empty { text-align: center; color: var(--text3); padding: 20px; font-size: 13px; }
.mono { font-family: monospace; }
.toast { position: fixed; top: 80px; right: 20px; padding: 12px 20px; border-radius: 8px; color: white; font-size: 14px; z-index: 1000; animation: slideIn 0.3s; }
@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
.toast.success { background: var(--success); }
.toast.error { background: var(--danger); }
.toast.info { background: var(--info); }
.modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { background: var(--card); border-radius: 12px; padding: 24px; max-width: 600px; width: 90%; max-height: 80vh; overflow-y: auto; }
.modal-title { font-size: 18px; font-weight: 700; margin-bottom: 16px; }
.modal-section { margin-bottom: 16px; }
.modal-label { font-size: 12px; color: var(--text2); margin-bottom: 4px; font-weight: 600; }
.modal-content { font-size: 13px; background: var(--bg2); padding: 10px; border-radius: 6px; word-break: break-all; max-height: 200px; overflow-y: auto; }
.log-terminal { background: #0f172a; color: #10b981; font-family: monospace; font-size: 12px; padding: 12px; border-radius: 8px; height: 300px; overflow-y: auto; }
.log-line { margin-bottom: 2px; }
.log-info { color: #60a5fa; }
.log-warning { color: #fbbf24; }
.log-error { color: #f87171; }
.usage-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }
.usage-item { text-align: center; padding: 12px; background: var(--bg2); border-radius: 8px; }
.usage-value { font-size: 22px; font-weight: 700; }
.usage-label { font-size: 11px; color: var(--text3); margin-top: 4px; }
.key-usage-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.key-usage-name { width: 100px; font-size: 12px; }
.key-usage-track { flex: 1; height: 20px; background: var(--bg2); border-radius: 4px; overflow: hidden; }
.key-usage-fill { height: 100%; background: var(--primary); border-radius: 4px; transition: width 0.3s; }
.key-usage-val { width: 120px; font-size: 12px; text-align: right; color: var(--text2); }
input, select { padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; background: var(--card); color: var(--text); }
input:focus, select:focus { outline: none; border-color: var(--primary); }
</style>
</head>
<body>

<div class="navbar">
  <div class="navbar-title">🔑 API Key 轮询代理</div>
  <div class="navbar-status" id="navStatus">运行中</div>
  <div class="navbar-actions">
    <button class="btn btn-primary btn-sm" onclick="testAllKeys()">全部测试</button>
    <button class="btn btn-success btn-sm" onclick="unlockAll()">全部解锁</button>
    <button class="btn btn-sm" onclick="reloadConfig()">重载配置</button>
    <button class="btn btn-sm" onclick="toggleTheme()">🌙 暗黑</button>
  </div>
</div>

<div class="container">
  <!-- 概览卡片 -->
  <div class="overview-grid">
    <div class="overview-card">
      <div class="label">总请求数</div>
      <div class="value" id="ovTotal">-</div>
      <div class="sub">累计转发请求</div>
    </div>
    <div class="overview-card">
      <div class="label">成功 / 成功率</div>
      <div class="value" id="ovSuccess" style="color:var(--success);">-</div>
      <div class="sub" id="ovRate">-</div>
    </div>
    <div class="overview-card">
      <div class="label">失败</div>
      <div class="value" id="ovFailed" style="color:var(--danger);">-</div>
      <div class="sub">失败请求数</div>
    </div>
    <div class="overview-card">
      <div class="label">消耗 Token</div>
      <div class="value" id="ovTokens" style="color:var(--warning);">-</div>
      <div class="sub" id="ovAvgTime">平均响应 -</div>
    </div>
  </div>

  <!-- 用量统计 -->
  <div class="collapsible" id="sectionUsage">
    <div class="collapsible-header" onclick="toggleSection('sectionUsage')">
      <div>📊 用量统计</div>
      <span id="usageDaysInfo" style="font-size:12px;color:var(--text3);">加载中...</span>
    </div>
    <div class="collapsible-body">
      <div class="usage-grid" id="totalSummary">
        <div class="usage-item"><div class="usage-value">-</div><div class="usage-label">总请求</div></div>
        <div class="usage-item"><div class="usage-value">-</div><div class="usage-label">总成功</div></div>
        <div class="usage-item"><div class="usage-value">-</div><div class="usage-label">总失败</div></div>
        <div class="usage-item"><div class="usage-value">-</div><div class="usage-label">总Token</div></div>
        <div class="usage-item"><div class="usage-value">-</div><div class="usage-label">总成功率</div></div>
      </div>
      <div style="margin-bottom:20px;">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--text2);">📈 近30天请求趋势</div>
        <svg id="lineChart" width="100%" height="200"></svg>
      </div>
      <div style="margin-bottom:20px;">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--text2);">🔑 每个 Key 用量对比</div>
        <div id="keyUsageBars"><div class="empty">加载中...</div></div>
      </div>
      <div>
        <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--text2);">📊 总的按模型统计（所有Key合计）</div>
        <div id="totalModelUsage"><div class="empty">加载中...</div></div>
      </div>
    </div>
  </div>

  <!-- Key 管理 -->
  <div class="collapsible" id="sectionKeys">
    <div class="collapsible-header" onclick="toggleSection('sectionKeys')">
      <div>⚙️ Key 管理 <span style="font-size:12px;color:var(--text3);font-weight:normal;">（拖拽排序，点击Key显示/隐藏）</span></div>
      <span class="collapsible-icon">▼</span>
    </div>
    <div class="collapsible-body">
      <div style="display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;">
        <input type="text" id="newKeyInput" placeholder="输入新的 API Key" style="flex:1;min-width:200px;">
        <input type="text" id="newKeyRemark" placeholder="备注（可选）" style="width:150px;">
        <input type="number" id="newKeyPriority" placeholder="优先级" value="0" style="width:80px;">
        <button class="btn btn-primary" onclick="addKey()">➕ 添加 Key</button>
      </div>
      <div id="keyList"><div class="empty">加载中...</div></div>
    </div>
  </div>

  <!-- 高级分析 -->
  <div class="collapsible collapsed" id="sectionAdvanced">
    <div class="collapsible-header" onclick="toggleSection('sectionAdvanced')">
      <div>🔬 高级分析</div>
      <span class="collapsible-icon">▼</span>
    </div>
    <div class="collapsible-body">
      <div id="advancedContent"><div class="empty">加载中...</div></div>
    </div>
  </div>

  <!-- 最近请求 -->
  <div class="collapsible collapsed" id="sectionRequests">
    <div class="collapsible-header" onclick="toggleSection('sectionRequests')">
      <div>📋 最近请求记录</div>
      <span class="collapsible-icon">▼</span>
    </div>
    <div class="collapsible-body">
      <div id="requestsList"><div class="empty">加载中...</div></div>
    </div>
  </div>

  <!-- 操作日志 -->
  <div class="collapsible collapsed" id="sectionActions">
    <div class="collapsible-header" onclick="toggleSection('sectionActions')">
      <div>📝 操作日志</div>
      <span class="collapsible-icon">▼</span>
    </div>
    <div class="collapsible-body">
      <div id="actionsList"><div class="empty">加载中...</div></div>
    </div>
  </div>

  <!-- 实时日志终端 -->
  <div class="collapsible collapsed" id="sectionLogs">
    <div class="collapsible-header" onclick="toggleSection('sectionLogs')">
      <div>💻 实时日志终端</div>
      <span class="collapsible-icon">▼</span>
    </div>
    <div class="collapsible-body">
      <div class="log-terminal" id="logTerminal"></div>
    </div>
  </div>
</div>

<div id="modalContainer"></div>
<div id="toastContainer"></div>

<script>
// ===== 全局状态 =====
let visibleKeys = {};
let modelUsageVisible = {};
let dragSrcIndex = null;
const startTime = Date.now();

// ===== 主题切换 =====
function toggleTheme() {
  const html = document.documentElement;
  const isDark = html.getAttribute('data-theme') === 'dark';
  html.setAttribute('data-theme', isDark ? 'light' : 'dark');
  localStorage.setItem('theme', isDark ? 'light' : 'dark');
}
if (localStorage.getItem('theme') === 'dark') {
  document.documentElement.setAttribute('data-theme', 'dark');
}

// ===== 可折叠面板 =====
function toggleSection(id) {
  document.getElementById(id).classList.toggle('collapsed');
}

// ===== Toast 提示 =====
function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}

// ===== 模态框 =====
function showModal(title, content) {
  const container = document.getElementById('modalContainer');
  container.innerHTML = '<div class="modal-overlay" onclick="closeModal()">' +
    '<div class="modal" onclick="event.stopPropagation()">' +
    '<div class="modal-title">' + title + '</div>' + content +
    '<div style="text-align:right;margin-top:16px;"><button class="btn" onclick="closeModal()">关闭</button></div>' +
    '</div></div>';
}
function closeModal() { document.getElementById('modalContainer').innerHTML = ''; }

// ===== 运行时长 =====
function formatDuration(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h > 0 ? h + 'h ' : '') + (m > 0 ? m + 'm ' : '') + sec + 's';
}

// ===== 主数据刷新 =====
async function fetchData() {
  try {
    const res = await fetch('/stats');
    const data = await res.json();
    const s = data.stats || {};
    const total = Object.values(s).reduce((a, k) => a + (k.total_requests || 0), 0);
    const success = Object.values(s).reduce((a, k) => a + (k.success || 0), 0);
    const failed = Object.values(s).reduce((a, k) => a + (k.failed || 0), 0);
    const tokens = Object.values(s).reduce((a, k) => a + (k.total_tokens || 0), 0);
    const times = Object.values(s).map(k => k.avg_response_ms || 0).filter(t => t > 0);
    const avgTime = times.length ? Math.round(times.reduce((a, b) => a + b, 0) / times.length) : 0;

    document.getElementById('ovTotal').textContent = total;
    document.getElementById('ovSuccess').textContent = success;
    document.getElementById('ovRate').textContent = total > 0 ? '成功率 ' + (success / total * 100).toFixed(1) + '%' : '-';
    document.getElementById('ovFailed').textContent = failed;
    document.getElementById('ovTokens').textContent = tokens;
    document.getElementById('ovAvgTime').textContent = '平均响应 ' + avgTime + 'ms';

    renderKeyList(data);
    if (data.keys) highlightKeySwitch(data.keys);

    const allCooling = data.keys && data.keys.every(k => k.status !== 'available');
    if (allCooling && data.keys.length > 0) {
      document.getElementById('navStatus').textContent = '全部冷却中';
      document.getElementById('navStatus').style.color = 'var(--warning)';
    } else {
      document.getElementById('navStatus').textContent = '运行中 · ' + formatDuration(Date.now() - startTime);
      document.getElementById('navStatus').style.color = '';
    }
  } catch (e) { console.error(e); }
}

// ===== 渲染 Key 列表 =====
function renderKeyList(data) {
  const keys = data.keys || [];
  const stats = data.stats || {};
  if (!keys.length) { document.getElementById('keyList').innerHTML = '<div class="empty">暂无 Key，请在上方添加</div>'; return; }
  let html = '';
  keys.forEach((k, i) => {
    const st = stats[k.key_full] || stats[k.key] || {};
    const isVisible = visibleKeys[i];
    const displayKey = isVisible ? k.key : (k.key.length > 12 ? k.key.slice(0, 6) + '...' + k.key.slice(-4) : k.key);
    const statusClass = k.status === 'available' ? 'active' : (k.status === 'disabled' ? 'disabled' : 'cooling');
    const statusText = k.status === 'available' ? '● 可用' : (k.status === 'disabled' ? '○ 已禁用' : '⏳ 冷却中');
    const cardClass = k.status === 'cooling' ? 'key-card cooling' : (k.status === 'disabled' ? 'key-card disabled' : 'key-card');
    const cooldownText = k.cooldown_remaining > 0 ? ' · 剩余' + Math.ceil(k.cooldown_remaining) + 's' : '';

    html += '<div class="' + cardClass + '" data-index="' + i + '">' +
      '<div class="key-card-header">' +
        '<div class="key-card-left" style="flex-wrap:wrap;gap:6px;">' +
          '<span class="drag-handle" title="拖拽排序">⋮⋮</span>' +
          '<span class="key-badge ' + statusClass + '">' + statusText + cooldownText + '</span>' +
          '<span class="key-value mono" onclick="toggleKeyVisibility(' + i + ')" title="点击显示/隐藏完整Key">' + displayKey + '</span>' +
          '<span style="font-size:11px;color:var(--text3);">优先级:' + (k.priority || 0) + '</span>' +
          '<span class="key-remark" onclick="editKeyRemark(' + i + ')" title="点击编辑备注">' + (k.remark ? '📝 ' + k.remark : '➕ 添加备注') + '</span>' +
        '</div>' +
        '<div class="key-card-actions">' +
          '<button class="btn btn-sm btn-primary" onclick="testKey(' + i + ')">测试</button>' +
          (k.status === 'cooling' ? '<button class="btn btn-sm btn-success" onclick="unlockKey(' + i + ')">解锁</button>' : '') +
          (k.status === 'disabled' ? '<button class="btn btn-sm btn-warning" onclick="enableKey(' + i + ')">启用</button>' : '<button class="btn btn-sm" onclick="disableKey(' + i + ')">禁用</button>') +
          '<button class="btn btn-sm btn-danger" onclick="deleteKey(' + i + ')">删除</button>' +
        '</div>' +
      '</div>' +
      '<div class="key-card-stats">' +
        '<div class="key-stat"><div class="key-stat-value">' + (st.total_requests || 0) + '</div><div class="key-stat-label">请求</div></div>' +
        '<div class="key-stat"><div class="key-stat-value" style="color:var(--success);">' + (st.success || 0) + '</div><div class="key-stat-label">成功</div></div>' +
        '<div class="key-stat"><div class="key-stat-value" style="color:var(--danger);">' + (st.failed || 0) + '</div><div class="key-stat-label">失败</div></div>' +
        '<div class="key-stat"><div class="key-stat-value" style="color:var(--warning);">' + (st.rate_limited || 0) + '</div><div class="key-stat-label">频率限流</div></div>' +
        '<div class="key-stat"><div class="key-stat-value" style="color:var(--warning);">' + (st.quota_limited || 0) + '</div><div class="key-stat-label">配额限流</div></div>' +
        '<div class="key-stat"><div class="key-stat-value" style="color:var(--primary);">' + (st.total_tokens || 0) + '</div><div class="key-stat-label">Token</div></div>' +
      '</div>' +
      '<div style="padding:0 15px 12px;border-top:1px solid var(--border);">' +
        '<button class="btn btn-sm" style="margin-top:8px;width:100%;" onclick="toggleModelUsage(' + i + ')">' + (modelUsageVisible[i] ? '▼ 隐藏按模型统计' : '▶ 查看按模型统计') + '</button>' +
        (modelUsageVisible[i] ? renderKeyModelUsage(st.model_usage || {}) : '') +
      '</div>' +
    '</div>';
  });
  document.getElementById('keyList').innerHTML = html;
  setupDragAndDrop();
}

// ===== Key 显示/隐藏 =====
function toggleKeyVisibility(i) { visibleKeys[i] = !visibleKeys[i]; fetchData(); }

// ===== 按模型统计 =====
function toggleModelUsage(i) { modelUsageVisible[i] = !modelUsageVisible[i]; fetchData(); }

function renderKeyModelUsage(modelUsage) {
  const models = Object.keys(modelUsage);
  if (models.length === 0) {
    return '<div style="margin-top:8px;padding:8px;background:var(--bg2);border-radius:6px;font-size:12px;color:var(--text3);">暂无按模型统计数据（发起请求后自动记录）</div>';
  }
  let html = '<div style="margin-top:8px;background:var(--bg2);border-radius:6px;padding:10px;">';
  html += '<div style="font-size:12px;font-weight:600;margin-bottom:6px;color:var(--text2);">按模型统计</div>';
  models.forEach(model => {
    const usage = modelUsage[model];
    html += '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:12px;">' +
      '<span class="mono" style="color:var(--text2);">' + model + '</span>' +
      '<span style="color:var(--text3);">' + usage.requests + '次 / ' + usage.tokens + ' Token</span>' +
      '</div>';
  });
  html += '</div>';
  return html;
}

// ===== Key 管理操作 =====
async function addKey() {
  const key = document.getElementById('newKeyInput').value.trim();
  const priority = parseInt(document.getElementById('newKeyPriority').value) || 0;
  const remark = document.getElementById('newKeyRemark') ? document.getElementById('newKeyRemark').value.trim() : '';
  if (!key) { showToast('请输入 API Key', 'error'); return; }
  try {
    const res = await fetch('/api/keys', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, priority, remark }) });
    const data = await res.json();
    if (data.status === 'ok') {
      document.getElementById('newKeyInput').value = '';
      if (document.getElementById('newKeyRemark')) document.getElementById('newKeyRemark').value = '';
      showToast('Key 添加成功！共 ' + data.total_keys + ' 个', 'success');
      fetchData(); loadUsageStats(); loadActions();
    } else { showToast('添加失败: ' + data.message, 'error'); }
  } catch (e) { showToast('请求失败: ' + e.message, 'error'); }
}

async function editKeyRemark(index) {
  try {
    const res = await fetch('/stats');
    const data = await res.json();
    const key = data.keys[index];
    const oldRemark = key.remark || '';
    const newRemark = prompt('请输入这个 Key 的备注（方便你区分）：', oldRemark);
    if (newRemark === null) return;
    const updateRes = await fetch('/api/keys/' + index + '/remark', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ remark: newRemark }) });
    const updateData = await updateRes.json();
    if (updateData.status === 'ok') {
      showToast('备注修改成功', 'success');
      fetchData(); loadActions();
    } else { showToast('修改失败', 'error'); }
  } catch (e) { showToast('请求失败: ' + e.message, 'error'); }
}

async function deleteKey(index) {
  if (!confirm('确定要删除这个 Key 吗？')) return;
  try {
    const res = await fetch('/api/keys/' + index, { method: 'DELETE' });
    const data = await res.json();
    if (data.status === 'ok') {
      showToast('Key 已删除', 'success');
      fetchData(); loadUsageStats(); loadActions();
    } else { showToast('删除失败', 'error'); }
  } catch (e) { showToast('请求失败: ' + e.message, 'error'); }
}

async function unlockKey(index) {
  try {
    await fetch('/api/keys/' + index + '/unlock', { method: 'POST' });
    showToast('Key 已解锁', 'success');
    fetchData(); loadActions();
  } catch (e) { showToast('请求失败: ' + e.message, 'error'); }
}

async function disableKey(index) {
  try {
    await fetch('/api/keys/' + index + '/disable', { method: 'POST' });
    showToast('Key 已禁用', 'success');
    fetchData(); loadActions();
  } catch (e) { showToast('请求失败: ' + e.message, 'error'); }
}

async function enableKey(index) {
  try {
    await fetch('/api/keys/' + index + '/enable', { method: 'POST' });
    showToast('Key 已启用', 'success');
    fetchData(); loadActions();
  } catch (e) { showToast('请求失败: ' + e.message, 'error'); }
}

async function unlockAll() {
  try {
    await fetch('/api/keys/unlock-all', { method: 'POST' });
    showToast('全部 Key 已解锁', 'success');
    fetchData(); loadActions();
  } catch (e) { showToast('请求失败: ' + e.message, 'error'); }
}

async function reloadConfig() {
  try {
    const res = await fetch('/api/reload-config', { method: 'POST' });
    const data = await res.json();
    if (data.status === 'ok') {
      showToast('配置已重载，共 ' + data.key_count + ' 个 Key', 'success');
      fetchData(); loadUsageStats(); loadActions();
    } else { showToast('重载失败', 'error'); }
  } catch (e) { showToast('请求失败: ' + e.message, 'error'); }
}

// ===== 测试功能 =====
async function testKey(index) {
  const model = prompt('请输入模型名称（默认 glm-5.2）:', 'glm-5.2') || 'glm-5.2';
  const prompt = '你好，请用一句话介绍你自己。';
  showToast('测试中...', 'info');
  try {
    const res = await fetch('/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ index, model, prompt }) });
    const data = await res.json();
    if (data.status === 'ok') {
      showToast('测试成功！耗时 ' + data.elapsed_ms + 'ms，Token ' + data.tokens, 'success');
      showModal('测试结果',
        '<div class="modal-section"><div class="modal-label">模型</div><div class="modal-content">' + data.model + '</div></div>' +
        '<div class="modal-section"><div class="modal-label">耗时</div><div class="modal-content">' + data.elapsed_ms + ' ms</div></div>' +
        '<div class="modal-section"><div class="modal-label">Token</div><div class="modal-content">' + data.tokens + '</div></div>' +
        '<div class="modal-section"><div class="modal-label">回复</div><div class="modal-content">' + (data.response || '(空)') + '</div></div>');
      fetchData(); loadUsageStats();
    } else {
      showToast('测试失败: ' + data.message, 'error');
    }
  } catch (e) { showToast('请求失败: ' + e.message, 'error'); }
}

async function testAllKeys() {
  showToast('正在测试所有 Key...', 'info');
  const keys = document.querySelectorAll('#keyList .key-card');
  let success = 0, failed = 0;
  for (let i = 0; i < keys.length; i++) {
    try {
      const res = await fetch('/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ index: i, model: 'glm-5.2', prompt: 'hi' }) });
      const data = await res.json();
      if (data.status === 'ok') success++; else failed++;
    } catch (e) { failed++; }
  }
  showToast('测试完成：成功 ' + success + ' 个，失败 ' + failed + ' 个', success > 0 ? 'success' : 'error');
  fetchData(); loadUsageStats();
}

// ===== 用量统计 =====
async function loadUsageStats() {
  try {
    const totalRes = await fetch('/api/total-stats');
    const totalData = await totalRes.json();
    renderTotalSummary(totalData);
    renderKeyUsageBars(totalData.key_usage);
    renderTotalModelUsage(totalData.total_model_usage);
    document.getElementById('usageDaysInfo').textContent = '已记录 ' + totalData.days_recorded + ' 天 · 累计 ' + totalData.total.requests + ' 次请求';

    const dailyRes = await fetch('/api/daily-stats?days=30');
    const dailyData = await dailyRes.json();
    renderLineChart(dailyData.daily);
  } catch (e) { console.error(e); }
}

function renderTotalSummary(data) {
  const t = data.total;
  document.getElementById('totalSummary').innerHTML =
    '<div class="usage-item"><div class="usage-value">' + t.requests + '</div><div class="usage-label">总请求</div></div>' +
    '<div class="usage-item"><div class="usage-value" style="color:var(--success);">' + t.success + '</div><div class="usage-label">总成功</div></div>' +
    '<div class="usage-item"><div class="usage-value" style="color:var(--danger);">' + t.failed + '</div><div class="usage-label">总失败</div></div>' +
    '<div class="usage-item"><div class="usage-value" style="color:var(--primary);">' + t.tokens + '</div><div class="usage-label">总Token</div></div>' +
    '<div class="usage-item"><div class="usage-value" style="color:var(--warning);">' + t.success_rate + '%</div><div class="usage-label">总成功率</div></div>';
}

function renderKeyUsageBars(keyUsage) {
  if (!keyUsage || !keyUsage.length) { document.getElementById('keyUsageBars').innerHTML = '<div class="empty">暂无数据</div>'; return; }
  const max = Math.max(...keyUsage.map(k => k.requests), 1);
  let html = '';
  keyUsage.forEach(k => {
    const w = Math.max(2, (k.requests / max) * 100);
    const displayName = k.remark ? k.remark + ' (' + k.key + ')' : k.key;
    html += '<div class="key-usage-bar" title="' + (k.remark ? '备注: ' + k.remark + ' | ' : '') + '请求:' + k.requests + ' 成功:' + k.success + ' 失败:' + k.failed + ' Token:' + k.tokens + ' 限流:' + (k.rate_limited + k.quota_limited) + '">' +
      '<div class="key-usage-name' + (k.remark ? '' : ' mono') + '">' + displayName + '</div>' +
      '<div class="key-usage-track"><div class="key-usage-fill" style="width:' + w + '%;"></div></div>' +
      '<div class="key-usage-val">' + k.requests + '次 / ' + k.tokens + 'Token</div></div>';
  });
  document.getElementById('keyUsageBars').innerHTML = html;
}

function renderTotalModelUsage(totalModelUsage) {
  const models = Object.keys(totalModelUsage || {});
  const container = document.getElementById('totalModelUsage');
  if (!container) return;
  if (models.length === 0) {
    container.innerHTML = '<div class="empty">暂无按模型统计数据（发起请求后自动记录）</div>';
    return;
  }
  models.sort((a, b) => (totalModelUsage[b].tokens || 0) - (totalModelUsage[a].tokens || 0));
  const maxTokens = Math.max(...models.map(m => totalModelUsage[m].tokens || 0), 1);
  let html = '';
  models.forEach(model => {
    const usage = totalModelUsage[model];
    const w = Math.max(2, (usage.tokens / maxTokens) * 100);
    html += '<div style="margin-bottom:8px;">' +
      '<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px;">' +
      '<span class="mono" style="color:var(--text2);">' + model + '</span>' +
      '<span style="color:var(--text3);">' + usage.requests + '次 / ' + usage.tokens + ' Token</span>' +
      '</div>' +
      '<div style="height:8px;background:var(--bg2);border-radius:4px;overflow:hidden;">' +
      '<div style="height:100%;width:' + w + '%;background:linear-gradient(90deg,var(--primary),#8b5cf6);border-radius:4px;"></div>' +
      '</div></div>';
  });
  container.innerHTML = html;
}

function renderLineChart(daily) {
  const svg = document.getElementById('lineChart');
  if (!daily || !daily.length) { svg.innerHTML = '<text x="400" y="100" text-anchor="middle" fill="var(--text3)" font-size="14">暂无数据</text>'; return; }
  const W = 800, H = 200, P = { top: 20, right: 20, bottom: 30, left: 50 };
  const cw = W - P.left - P.right, ch = H - P.top - P.bottom;
  const maxReq = Math.max(...daily.map(d => d.requests), 1);
  let points = daily.map((d, i) => {
    const x = P.left + (i / (daily.length - 1)) * cw;
    const y = P.top + ch - (d.requests / maxReq) * ch;
    return { x, y, ...d };
  });
  let pathD = 'M ' + points.map(p => p.x + ',' + p.y).join(' L ');
  let areaD = pathD + ' L ' + points[points.length-1].x + ',' + (P.top + ch) + ' L ' + points[0].x + ',' + (P.top + ch) + ' Z';
  let html = '<defs><linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#6366f1" stop-opacity="0.3"/><stop offset="100%" stop-color="#6366f1" stop-opacity="0"/></linearGradient></defs>';
  html += '<path d="' + areaD + '" fill="url(#areaGrad)"/>';
  html += '<path d="' + pathD + '" fill="none" stroke="#6366f1" stroke-width="2"/>';
  points.forEach((p, i) => {
    // 给所有数据点都画圆点，添加 tooltip
    const isLabelPoint = (i % 5 === 0 || i === points.length - 1);
    html += '<circle cx="' + p.x + '" cy="' + p.y + '" r="' + (isLabelPoint ? 4 : 3) + '" fill="#6366f1" style="cursor:pointer;">';
    html += '<title>' + p.date + ' | 请求:' + p.requests + ' | 成功:' + p.success + ' | 失败:' + p.failed + ' | Token:' + p.tokens + '</title>';
    html += '</circle>';
    if (isLabelPoint) {
      html += '<text x="' + p.x + '" y="' + (H - 10) + '" text-anchor="middle" fill="var(--text3)" font-size="10">' + p.date.slice(5) + '</text>';
    }
  });
  for (let i = 0; i <= 4; i++) {
    const y = P.top + (i / 4) * ch;
    const val = Math.round(maxReq * (1 - i / 4));
    html += '<line x1="' + P.left + '" y1="' + y + '" x2="' + (W - P.right) + '" y2="' + y + '" stroke="var(--border)" stroke-dasharray="2,2"/>';
    html += '<text x="' + (P.left - 5) + '" y="' + (y + 4) + '" text-anchor="end" fill="var(--text3)" font-size="10">' + val + '</text>';
  }
  svg.innerHTML = html;
}

// ===== 高级分析 =====
async function loadAdvancedStats() {
  try {
    const res = await fetch('/api/advanced-stats');
    const data = await res.json();
    let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">';
    html += '<div><div style="font-size:13px;font-weight:600;margin-bottom:8px;">状态码分布</div>';
    const sc = data.status_codes || {};
    const scMax = Math.max(...Object.values(sc), 1);
    Object.entries(sc).forEach(([code, count]) => {
      const color = code.startsWith('2') ? 'var(--success)' : code.startsWith('4') ? 'var(--warning)' : 'var(--danger)';
      html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;"><span style="width:50px;font-size:12px;">' + code + '</span><div style="flex:1;height:16px;background:var(--bg2);border-radius:3px;"><div style="height:100%;width:' + (count/scMax*100) + '%;background:' + color + ';border-radius:3px;"></div></div><span style="width:40px;font-size:12px;text-align:right;">' + count + '</span></div>';
    });
    html += '</div>';
    html += '<div><div style="font-size:13px;font-weight:600;margin-bottom:8px;">响应时间分布</div>';
    const rt = data.response_time_buckets || {};
    const rtMax = Math.max(...Object.values(rt), 1);
    Object.entries(rt).forEach(([bucket, count]) => {
      html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;"><span style="width:80px;font-size:12px;">' + bucket + '</span><div style="flex:1;height:16px;background:var(--bg2);border-radius:3px;"><div style="height:100%;width:' + (count/rtMax*100) + '%;background:var(--info);border-radius:3px;"></div></div><span style="width:40px;font-size:12px;text-align:right;">' + count + '</span></div>';
    });
    html += '</div>';
    html += '<div><div style="font-size:13px;font-weight:600;margin-bottom:8px;">24小时请求分布</div>';
    const hr = data.hourly_requests || {};
    const hrMax = Math.max(...Object.values(hr), 1);
    html += '<div style="display:flex;align-items:flex-end;height:100px;gap:2px;">';
    for (let h = 0; h < 24; h++) {
      const count = hr[String(h)] || 0;
      const hgt = count / hrMax * 100;
      html += '<div style="flex:1;height:' + hgt + '%;background:var(--primary);border-radius:2px 2px 0 0;" title="' + h + '时: ' + count + '次"></div>';
    }
    html += '</div></div>';
    html += '<div><div style="font-size:13px;font-weight:600;margin-bottom:8px;">模型使用分布</div>';
    const mu = data.model_usage || {};
    const muMax = Math.max(...Object.values(mu), 1);
    Object.entries(mu).sort((a,b) => b[1]-a[1]).forEach(([model, count]) => {
      html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;"><span class="mono" style="width:150px;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + model + '</span><div style="flex:1;height:16px;background:var(--bg2);border-radius:3px;"><div style="height:100%;width:' + (count/muMax*100) + '%;background:var(--warning);border-radius:3px;"></div></div><span style="width:40px;font-size:12px;text-align:right;">' + count + '</span></div>';
    });
    html += '</div>';
    html += '</div>';
    if (data.rate_limit_events && data.rate_limit_events.length > 0) {
      html += '<div style="margin-top:20px;"><div style="font-size:13px;font-weight:600;margin-bottom:8px;">最近限流事件</div>';
      data.rate_limit_events.slice(-10).reverse().forEach(e => {
        html += '<div style="font-size:12px;padding:4px 0;border-bottom:1px solid var(--border);"><span style="color:var(--text3);">' + e.time + '</span> <span style="color:var(--warning);">状态码 ' + e.status_code + '</span></div>';
      });
      html += '</div>';
    }
    document.getElementById('advancedContent').innerHTML = html;
  } catch (e) { console.error(e); }
}

// ===== 最近请求 =====
async function loadRequests() {
  try {
    const res = await fetch('/api/recent-requests?limit=50');
    const data = await res.json();
    const requests = data.requests || [];
    if (!requests.length) { document.getElementById('requestsList').innerHTML = '<div class="empty">暂无请求记录</div>'; return; }
    let html = '<table style="width:100%;font-size:12px;border-collapse:collapse;">';
    html += '<tr style="border-bottom:2px solid var(--border);"><th style="text-align:left;padding:8px;">时间</th><th style="text-align:left;padding:8px;">状态码</th><th style="text-align:left;padding:8px;">模型</th><th style="text-align:left;padding:8px;">Token</th><th style="text-align:left;padding:8px;">耗时</th><th style="text-align:left;padding:8px;">Key</th><th style="text-align:left;padding:8px;">操作</th></tr>';
    requests.forEach(r => {
      const statusColor = r.status_code >= 200 && r.status_code < 300 ? 'var(--success)' : r.status_code === 429 ? 'var(--warning)' : 'var(--danger)';
      const keyDisplay = r.remark ? r.remark + ' (' + r.key + ')' : r.key;
      html += '<tr style="border-bottom:1px solid var(--border);">' +
        '<td style="padding:8px;">' + r.time + '</td>' +
        '<td style="padding:8px;color:' + statusColor + ';font-weight:600;">' + r.status_code + '</td>' +
        '<td style="padding:8px;" class="mono">' + (r.model || '-') + '</td>' +
        '<td style="padding:8px;">' + r.tokens + '</td>' +
        '<td style="padding:8px;">' + r.elapsed_ms + 'ms</td>' +
        '<td style="padding:8px;" title="' + (r.remark ? '备注: ' + r.remark : '') + '">' + keyDisplay + '</td>' +
        '<td style="padding:8px;"><button class="btn btn-sm" onclick="showRequestDetail(' + r.id + ')">详情</button></td>' +
        '</tr>';
    });
    html += '</table>';
    document.getElementById('requestsList').innerHTML = html;
  } catch (e) { console.error(e); }
}

async function showRequestDetail(id) {
  try {
    const res = await fetch('/api/request-details/' + id);
    const data = await res.json();
    if (data.status !== 'ok') { showToast('未找到详情', 'error'); return; }
    const d = data.detail;
    const keyDetail = d.remark ? d.remark + ' (' + d.key + ')' : d.key;
    showModal('请求详情 #' + id,
      '<div class="modal-section"><div class="modal-label">请求信息</div><div class="modal-content">时间: ' + d.time + ' | 方法: POST | 状态码: ' + d.status_code + ' | 耗时: ' + d.elapsed_ms + 'ms | Key: ' + keyDetail + '</div></div>' +
      '<div class="modal-section"><div class="modal-label">请求内容</div><div class="modal-content">' + (d.request_body || '(空)') + '</div></div>' +
      '<div class="modal-section"><div class="modal-label">响应内容</div><div class="modal-content">' + (d.response_body || '(空)') + '</div></div>');
  } catch (e) { showToast('加载详情失败', 'error'); }
}

// ===== 操作日志 =====
async function loadActions() {
  try {
    const res = await fetch('/api/actions');
    const data = await res.json();
    const actions = data.actions || [];
    if (!actions.length) { document.getElementById('actionsList').innerHTML = '<div class="empty">暂无操作记录</div>'; return; }
    let html = '';
    actions.forEach(a => {
      html += '<div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:12px;">' +
        '<span style="color:var(--text3);">' + a.time + '</span> ' +
        '<span style="color:var(--primary);font-weight:600;">[' + a.action + ']</span> ' +
        '<span style="color:var(--text2);">' + a.detail + '</span></div>';
    });
    document.getElementById('actionsList').innerHTML = html;
  } catch (e) { console.error(e); }
}

// ===== 实时日志终端 =====
function addLog(type, message) {
  const terminal = document.getElementById('logTerminal');
  if (!terminal) return;
  const time = new Date().toLocaleTimeString();
  const line = document.createElement('div');
  line.className = 'log-line log-' + type;
  line.textContent = '[' + time + '] ' + message;
  terminal.appendChild(line);
  terminal.scrollTop = terminal.scrollHeight;
  while (terminal.children.length > 200) terminal.removeChild(terminal.firstChild);
}

// ===== Key 轮换高亮 =====
let lastActiveKey = -1;
function highlightKeySwitch(keys) {
  const activeIdx = keys.findIndex(k => k.status === 'available');
  if (activeIdx !== -1 && activeIdx !== lastActiveKey && lastActiveKey !== -1) {
    const cards = document.querySelectorAll('#keyList .key-card');
    if (cards[activeIdx]) {
      cards[activeIdx].classList.add('highlight');
      setTimeout(() => cards[activeIdx].classList.remove('highlight'), 1000);
    }
    addLog('info', 'Key 切换 | 从 Key' + (lastActiveKey + 1) + ' 切换到 Key' + (activeIdx + 1));
  }
  if (activeIdx !== -1) lastActiveKey = activeIdx;
}

// ===== 拖拽排序 =====
function setupDragAndDrop() {
  const cards = document.querySelectorAll('#keyList .key-card');
  cards.forEach(card => {
    card.draggable = true;
    card.addEventListener('dragstart', (e) => {
      dragSrcIndex = parseInt(card.dataset.index);
      e.dataTransfer.effectAllowed = 'move';
    });
    card.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; });
    card.addEventListener('drop', async (e) => {
      e.preventDefault();
      const targetIndex = parseInt(card.dataset.index);
      if (dragSrcIndex !== null && dragSrcIndex !== targetIndex) {
        const keys = document.querySelectorAll('#keyList .key-card');
        const order = Array.from(keys).map(k => parseInt(k.dataset.index));
        const [moved] = order.splice(dragSrcIndex, 1);
        order.splice(targetIndex, 0, moved);
        try {
          await fetch('/api/keys/reorder', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order }) });
          showToast('排序已更新', 'success');
          fetchData(); loadActions();
        } catch (err) { showToast('排序失败', 'error'); }
      }
      dragSrcIndex = null;
    });
  });
}

// ===== 初始化 =====
fetchData();
loadUsageStats();
loadRequests();
loadActions();
loadAdvancedStats();
setInterval(fetchData, 1000);
setInterval(loadRequests, 5000);
setInterval(loadUsageStats, 30000);
setInterval(loadActions, 10000);
setInterval(loadAdvancedStats, 5000);
addLog('info', '仪表盘已启动，开始监控...');
</script>
</body>
</html>
"""

# ===== 启动 =====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.get("host", "0.0.0.0"), port=config.get("port", 8000))
