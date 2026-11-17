# -*- coding: utf-8 -*-
"""
server.py —— 学术工作台本地后端
功能：
  * 提供前端页面（web/ 目录）
  * API：概览统计、目录树、资讯缓存、待办、研究日志、打开文件夹
  * 资讯缓存管理：1 小时 TTL（见下方 CACHE_TTL），手动刷新立即拉取；断网时返回旧缓存
  * 依赖：仅 Python 标准库
运行：python3 server.py  →  浏览器打开 http://127.0.0.1:8765
"""

import os
import sys
import socket
import re
import json
import time
import shutil
import subprocess
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import fetchers

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(BASE_DIR)          # 学术工作台根目录
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")
TODOS_FILE = os.path.join(DATA_DIR, "todos.json")
GRAD_TRACK_FILE = os.path.join(DATA_DIR, "graduation_track.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "journal.json")
LITERATURE_DIR = os.path.join(DATA_DIR, "literature")
FRONTIER_DIR = os.path.join(DATA_DIR, "frontier")
HOTSPOT_DIR = os.path.join(DATA_DIR, "hotspots")
PUBLICATIONS_FILE = os.path.join(DATA_DIR, "publications.json")
ABLESCI_QUEUE_FILE = os.path.join(DATA_DIR, "ablesci_queue.json")
GROUPS_FILE = os.path.join(DATA_DIR, "library_groups.json")
ENTRY_NAMES_FILE = os.path.join(DATA_DIR, "entry_names.json")

PORT = 8765
WORKER_BASE = "http://127.0.0.1:8766"   # PDF 转写 worker（pdf_worker/worker.py）
WORKER_PY = os.path.join(BASE_DIR, "pdf_worker", ".venv",
                          "Scripts" if os.name == "nt" else "bin",
                          "python.exe" if os.name == "nt" else "python")
WORKER_SCRIPT = os.path.join(BASE_DIR, "pdf_worker", "worker.py")
CACHE_TTL = 1 * 3600          # 资讯缓存有效期：1 小时
CACHE_MAX_AGE = 7 * 24 * 3600  # 缓存最长保留：7 天（之后即使断网也不展示）


# ---------------------------------------------------------------------------
# 个人设定（可选）：复制 data/settings.example.json 为 data/settings.json 即可覆盖
#   field_name           侧栏与浏览器标题上的研究领域名
#   degree_level         学段：本科 / 硕士 / 博士（决定进度卡标题与年级名）
#   program_years        学制年数（本科一般 4、硕士 3、博士 4）
#   phd_start            入学日期 YYYY-MM-DD
#   phd_end              预计毕业日期 YYYY-MM-DD
#   c_journal_required   毕业要求论文数
#   c_journal_label      毕业要求名称（如「C 刊论文」「SCI 一区」）
#   workspace_dir        「文件夹」面板扫描的根目录（默认 = 工作台的上一级目录）
#   auto_archive         每日自动 git 存档开关（默认关闭；开启后每天自动提交一次）
#
# 学制四项（degree_level / program_years / phd_start / phd_end）刻意**不设默认值**：
# 没配置时界面显示「待设置」并给出提示，而不是拿内置日期算出一个不属于你的进度。
# ---------------------------------------------------------------------------
def _load_settings():
    """读取 data/settings.json；不存在或损坏时返回空 dict，全部走内置默认值。"""
    try:
        with open(os.path.join(DATA_DIR, "settings.json"), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


SETTINGS = _load_settings()

# 研究领域名：显示在侧栏品牌下方（改 data/settings.json 的 field_name 即可）
FIELD_NAME = SETTINGS.get("field_name") or "你的研究领域"

# 学段与学制（可在 data/settings.json 覆盖）。刻意不设默认日期，见上方注释。
DEGREE_LEVEL = (SETTINGS.get("degree_level") or "").strip()
try:
    PROGRAM_YEARS = int(SETTINGS.get("program_years") or 0)
except (TypeError, ValueError):
    PROGRAM_YEARS = 0
PHD_START = (SETTINGS.get("phd_start") or "").strip()
PHD_END = (SETTINGS.get("phd_end") or "").strip()

try:
    _C_JOURNAL_N = int(SETTINGS.get("c_journal_required") or 2)
except (TypeError, ValueError):
    _C_JOURNAL_N = 2

# 毕业条件
GRADUATION_REQUIREMENTS = {
    "c_journal_required": _C_JOURNAL_N,   # 需发表论文数
    "c_journal_label": SETTINGS.get("c_journal_label") or "C 刊论文",
}

# 工作台根目录（「文件夹」面板扫描起点，可在 settings.json 用 workspace_dir 覆盖）
WORKSPACE = SETTINGS.get("workspace_dir") or WORKSPACE

# 板块目录（与文件夹体系一一对应）
SECTIONS = [
    ("01_文献库", "文献库"),
    ("02_研究笔记", "研究笔记"),
    ("03_论文写作", "论文写作"),
    ("04_数据分析", "数据分析"),
    ("05_学业事务", "学业事务"),
    ("06_项目归档", "项目归档"),
    ("07_个人管理", "个人管理"),
    ("08_临时中转", "临时中转"),
    ("09_毕业设计", "毕业设计"),
]

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 数据读写工具
# ---------------------------------------------------------------------------
def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _read_json(path, default):
    _ensure_data_dir()
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    _ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 文件系统服务
# ---------------------------------------------------------------------------
def scan_tree(root, max_depth=3, max_items=80):
    """扫描工作台目录树，返回 {name, path, type, children, count}"""
    root = os.path.abspath(root)

    def walk(dirpath, depth):
        node = {
            "name": os.path.basename(dirpath) or dirpath,
            "path": dirpath,
            "type": "dir",
            "count": 0,
            "children": [],
        }
        try:
            entries = sorted(os.listdir(dirpath), key=lambda x: (not os.path.isdir(os.path.join(dirpath, x)), x.lower()))
        except OSError:
            return node
        for name in entries:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            if os.path.isdir(full):
                if depth < max_depth:
                    child = walk(full, depth + 1)
                else:
                    child = {"name": name, "path": full, "type": "dir", "count": 0, "children": []}
                node["children"].append(child)
                node["count"] += child["count"]
            else:
                node["children"].append({"name": name, "path": full, "type": "file", "size": os.path.getsize(full) if os.path.exists(full) else 0})
                node["count"] += 1
        node["children"] = node["children"][:max_items]
        return node

    return walk(root, 0)


def section_stats():
    """统计各板块文件数 + 最近修改的 2 个文件。"""
    stats = []
    for folder, label in SECTIONS:
        path = os.path.join(WORKSPACE, folder)
        count = 0
        recent = []
        if os.path.isdir(path):
            all_files = []
            for dirpath, _, filenames in os.walk(path):
                for fn in filenames:
                    if fn.startswith(".") or fn.startswith("~$"):
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        all_files.append((os.path.getmtime(fp), fp))
                    except OSError:
                        pass
            count = len(all_files)
            recent = [fp for _, fp in sorted(all_files, reverse=True)[:2]]
        stats.append({"folder": folder, "label": label, "count": count, "recent": recent})
    return stats


# 学段 → 年级单字（大一 / 研二 / 博三）
_DEGREE_STAGE_CHAR = {"本科": "大", "硕士": "研", "博士": "博"}
_CN_DIGITS = "〇一二三四五六七八九十"


def degree_label():
    """进度卡标题：设了学段就是「博士进度 / 硕士进度 / 本科进度」，否则「学业进度」。"""
    return (DEGREE_LEVEL + "进度") if DEGREE_LEVEL else "学业进度"


def _stage_name(year_no):
    """第 N 学年 → 年级名（大一 / 研二 / 博三）。没设学段时返回空串。"""
    ch = _DEGREE_STAGE_CHAR.get(DEGREE_LEVEL)
    if not ch or year_no < 1:
        return ""
    num = _CN_DIGITS[year_no] if year_no <= 10 else str(year_no)
    return ch + num


def phd_progress():
    """学业进度：入学至今的天数与百分比。

    未在 data/settings.json 里配置学制时返回 configured=False，由前端提示「待设置」——
    绝不拿内置日期假装算出一个不属于用户的进度（这正是旧版「博二显示成博一」的原因）。
    """
    from datetime import datetime

    today = datetime.now()
    base = {
        "configured": False,
        "label": degree_label(),
        "degree_level": DEGREE_LEVEL,
        "total_years": PROGRAM_YEARS,
        "stage": "",
        "year_no": 0,
        "start": PHD_START,
        "end": PHD_END,
        "elapsed_days": 0,
        "total_days": 0,
        "remain_days": 0,
        "percent": 0.0,
        "today": today.strftime("%Y-%m-%d"),
    }
    try:
        start = datetime.strptime(PHD_START, "%Y-%m-%d")
        end = datetime.strptime(PHD_END, "%Y-%m-%d")
    except (TypeError, ValueError):
        return base
    if end <= start:
        return base

    total = (end - start).days
    elapsed = max(0, (today - start).days)
    pct = min(100.0, elapsed / total * 100)
    remain = (end - today).days

    # 当前是第几学年：按入学月日逐年滚动，跨过入学日才算升一级
    years_passed = today.year - start.year - (
        1 if (today.month, today.day) < (start.month, start.day) else 0
    )
    year_no = max(1, years_passed + 1)
    if PROGRAM_YEARS:
        year_no = min(year_no, PROGRAM_YEARS)

    base.update({
        "configured": True,
        "stage": _stage_name(year_no),
        "year_no": year_no,
        "elapsed_days": elapsed,
        "total_days": total,
        "remain_days": max(0, remain),
        "percent": round(pct, 1),
    })
    return base


def get_publications():
    """读取已发表论文列表。"""
    if not os.path.exists(PUBLICATIONS_FILE):
        return []
    try:
        with open(PUBLICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_publications(pubs):
    """保存论文列表。"""
    with _lock:
        with open(PUBLICATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(pubs, f, ensure_ascii=False, indent=2)


def grad_track():
    """毕业要求中「论文之外的硬性环节」清单，读 data/graduation_track.json。"""
    data = _read_json(GRAD_TRACK_FILE, {"items": []}) or {}
    return {
        "basis": data.get("basis", ""),
        "updated": data.get("updated", ""),
        "items": data.get("items", []) or [],
    }


def graduation_progress():
    """毕业条件进度统计。"""
    pubs = get_publications()
    c_pubs = [p for p in pubs if p.get("type") == "c_journal"]
    required = GRADUATION_REQUIREMENTS["c_journal_required"]
    track = grad_track()
    return {
        "c_journal": {
            "required": required,
            "label": GRADUATION_REQUIREMENTS["c_journal_label"],
            "achieved": len(c_pubs),
            "remaining": max(0, required - len(c_pubs)),
            "complete": len(c_pubs) >= required,
            "items": c_pubs,
        },
        "milestones": track["items"],
        "milestone_basis": track["basis"],
    }


def open_in_finder(path):
    """用系统默认方式打开路径：macOS → Finder；Windows → 资源管理器/默认程序；Linux → 文件管理器。"""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if os.name == "nt":
            os.startfile(path)  # 目录 → 资源管理器；文件 → 关联的默认程序
            return True
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 资讯缓存
# ---------------------------------------------------------------------------
def load_cache():
    return _read_json(CACHE_FILE, {"news": {}, "weather": None, "fetched_at": None})


def save_cache(data):
    _write_json(CACHE_FILE, data)


def get_news(force=False):
    """返回资讯数据。force=True 强制刷新；否则命中 TTL 用缓存。"""
    cache = load_cache()
    now = time.time()

    if not force:
        fetched_at = cache.get("fetched_at")
        if fetched_at:
            try:
                age = now - time.mktime(time.strptime(fetched_at, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                age = CACHE_TTL + 1
            # 缓存新鲜：直接用
            if age < CACHE_TTL:
                return {"ok": True, "from_cache": True, "data": cache, "age_seconds": int(age)}
            # 缓存过期但还在最长保留期内：返回旧数据并异步刷新
            if age < CACHE_MAX_AGE:
                threading.Thread(target=_background_refresh, daemon=True).start()
                return {"ok": True, "from_cache": True, "stale": True, "data": cache, "age_seconds": int(age)}

    # 无缓存 / 已过期超期 / 强制刷新：同步抓取
    try:
        data = fetchers.fetch_all_with_weather()
        cache = data
        save_cache(cache)
        return {"ok": True, "from_cache": False, "data": cache}
    except Exception as e:
        if cache.get("news"):
            return {"ok": True, "from_cache": True, "stale": True, "data": cache, "refresh_error": str(e)[:120]}
        return {"ok": False, "error": str(e)[:120], "data": {"news": {}, "weather": None}}


def _background_refresh():
    """后台线程刷新缓存（失败静默，不打断用户）。"""
    with _lock:
        cache = load_cache()
        try:
            data = fetchers.fetch_all_with_weather()
            save_cache(data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 天气：独立轻接口。资讯整包（/api/news）冷启动要抓十几个源、可能几十秒，
# 顶栏天气以前得跟着它一起等，所以单独开一条毫秒级通道。
# ---------------------------------------------------------------------------
WEATHER_REFRESH_AFTER = 30 * 60   # 缓存超过 30 分钟就后台悄悄换一次


def get_weather():
    cache = load_cache()
    w = cache.get("weather")
    if w:
        fetched_at = cache.get("fetched_at")
        age = None
        if fetched_at:
            try:
                age = time.time() - time.mktime(time.strptime(fetched_at, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                age = None
        if age is None or age > WEATHER_REFRESH_AFTER:
            threading.Thread(target=_refresh_weather_only, daemon=True).start()
        return {"ok": True, "weather": w, "from_cache": True, "age_seconds": int(age) if age else None}
    try:
        w = fetchers.fetch_weather()
        cache = load_cache()
        cache["weather"] = w
        save_cache(cache)
        return {"ok": True, "weather": w, "from_cache": False}
    except Exception as e:
        return {"ok": False, "weather": None, "error": str(e)[:120]}


def _refresh_weather_only():
    """只刷天气，不动资讯缓存（失败静默）。"""
    try:
        w = fetchers.fetch_weather()
        with _lock:
            cache = load_cache()
            cache["weather"] = w
            save_cache(cache)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 科技爱好者周刊：单源轻接口（磁盘持久化缓存 + 限流冷却）
# 以前周刊面板刷新会触发 /api/news?refresh=1 全量重抓（7 源 + LLM 翻译，20s+），
# 现在只抓 ruanyf/weekly 一个源；缓存落盘，服务重启不丢；GitHub 匿名限额 60 次/小时，
# 被限流后进入 1 小时冷却，期间直接回退缓存，不再雪上加霜。
# ---------------------------------------------------------------------------
WEEKLY_CACHE_TTL = 6 * 3600        # 缓存有效期 6 小时（周刊每周五才更新）
WEEKLY_COOLDOWN = 60 * 60          # 限流冷却期 1 小时
WEEKLY_CACHE_FILE = os.path.join(DATA_DIR, "weekly_cache.json")
_weekly_cache = {"items": None, "ts": 0.0}
_weekly_lock = threading.Lock()
_weekly_fail = {"ts": 0.0, "rate_limited": False}


def _load_weekly_disk():
    data = _read_json(WEEKLY_CACHE_FILE, {})
    return (data.get("items") or []), float(data.get("ts") or 0)


def _save_weekly_disk(items):
    _write_json(WEEKLY_CACHE_FILE, {
        "items": items,
        "ts": time.time(),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
    })


def _merge_weekly(new_items, *old_sources):
    """把新抓结果与旧缓存按期号合并。

    网络抖动时 fetch_weekly 可能只抓到部分期（实测会在 2–6 期之间跳），
    若直接覆盖缓存，列表会忽长忽短。合并规则：
      * 旧缓存里本期未抓到的期 → 保留旧数据
      * 同期新数据缺日期 → 沿用旧日期
    返回按期号倒序、最多 8 条。
    """
    old_map = {}
    for src in old_sources:
        for it in (src or []):
            iss = it.get("issue")
            if iss and iss not in old_map:
                old_map[iss] = it
    merged = {}
    for it in new_items:
        iss = it.get("issue")
        if not iss:
            continue
        if not it.get("date") and old_map.get(iss, {}).get("date"):
            it["date"] = old_map[iss]["date"]
        merged[iss] = it
    for iss, old in old_map.items():
        merged.setdefault(iss, old)
    return sorted(merged.values(), key=lambda x: x.get("issue") or 0, reverse=True)[:8]


def get_weekly(force=False):
    """返回科技爱好者周刊条目。优先级：进程内缓存 → 磁盘缓存 → 现抓 → 回退任一旧缓存。"""
    with _weekly_lock:
        now = time.time()
        cached = _weekly_cache["items"]
        if cached is None:
            disk_items, disk_ts = _load_weekly_disk()
            if disk_items:
                _weekly_cache["items"], _weekly_cache["ts"] = disk_items, disk_ts
                cached = disk_items
        else:
            disk_items, disk_ts = _load_weekly_disk()

        # 1) 进程内缓存新鲜
        if not force and cached is not None and now - _weekly_cache["ts"] < WEEKLY_CACHE_TTL:
            return {"ok": True, "from_cache": True, "items": cached}
        # 2) 磁盘缓存新鲜（服务重启后仍可用）
        if not force and disk_items and now - disk_ts < WEEKLY_CACHE_TTL:
            return {"ok": True, "from_cache": True, "items": disk_items}
        # 3) 限流冷却期内不重试
        if not force and _weekly_fail["rate_limited"] and now - _weekly_fail["ts"] < WEEKLY_COOLDOWN:
            fallback = cached or disk_items
            if fallback:
                return {"ok": True, "from_cache": True, "stale": True, "items": fallback,
                        "refresh_error": "GitHub 接口限流冷却中（1 小时内不重试）"}

        # 4) 现抓
        try:
            items = fetchers.fetch_weekly()
            if items:                      # 空结果不覆盖已有好缓存
                items = _merge_weekly(items, cached, disk_items)
                _weekly_cache["items"], _weekly_cache["ts"] = items, now
                _save_weekly_disk(items)
                _weekly_fail["rate_limited"] = False
                return {"ok": True, "from_cache": False, "items": items}
            return {"ok": True, "from_cache": True, "stale": True,
                    "items": cached or disk_items or [], "refresh_error": "本次未取到内容"}
        except Exception as e:
            err = str(e)[:120]
            _weekly_fail["ts"] = now
            _weekly_fail["rate_limited"] = ("403" in err or "rate limit" in err.lower())
            fallback = cached or disk_items
            if fallback:
                return {"ok": True, "from_cache": True, "stale": True,
                        "items": fallback, "refresh_error": err}
            return {"ok": False, "error": err, "items": []}


# ---------------------------------------------------------------------------
# 待办 / 研究日志
# ---------------------------------------------------------------------------
def get_todos():
    return _read_json(TODOS_FILE, [])


def save_todos(todos):
    _write_json(TODOS_FILE, todos)


def get_journal():
    return _read_json(JOURNAL_FILE, [])


def save_journal(entries):
    _write_json(JOURNAL_FILE, entries)


# ---------------------------------------------------------------------------
# 文献工具：结构化数据 + arXiv 追踪（按需拉取，30 分钟缓存）
# ---------------------------------------------------------------------------
def load_literature(name):
    """读取 literature/ 下的 JSON 数据文件。name: journals|glossary|queries"""
    mapping = {
        "journals": "journals.json",
        "glossary": "glossary.json",
        "queries": "search_queries.json",
    }
    fname = mapping.get(name)
    if not fname:
        return {"error": "unknown literature type", "valid": list(mapping.keys())}
    path = os.path.join(LITERATURE_DIR, fname)
    return _read_json(path, {"items": [], "count": 0})


_arxiv_cache = {"data": None, "ts": 0.0}
ARXIV_CACHE_TTL = 15 * 60  # 文献追踪缓存 15 分钟

# 多平台追踪：arXiv 被限流时依次退到 Crossref / Semantic Scholar
TRACK_QUERY_CROSSREF = "microrobot magnetic actuation micro/nanomechanics flexoelectric actuator"
TRACK_QUERY_S2 = "microrobot magnetic actuation micro/nanomechanics"


def get_arxiv_feed(force=False):
    """文献追踪：多平台兜底，保证「有东西看」比「只用 arXiv」重要。

    本机出口调 arXiv API 会稳定 429（同一出口 IP 共用配额），所以顺序是：
    arXiv → Crossref → Semantic Scholar，任何一个出结果就返回，并在返回体里
    写明实际用的平台（source），前端据此显示来源标签。
    """
    if not force and _arxiv_cache["data"] and (time.time() - _arxiv_cache["ts"]) < ARXIV_CACHE_TTL:
        return dict(_arxiv_cache["data"], from_cache=True)
    errors, used, items = [], None, []
    try:
        items = fetchers.fetch_arxiv(fetchers.ARXIV_AIBIO_QUERY, 18)
        if items:
            used = "arxiv"
    except Exception as e:
        errors.append("arXiv: " + str(e)[:80])
    if not items:
        try:
            items = fetchers.fetch_crossref(TRACK_QUERY_CROSSREF, 18)
            if items:
                used = "crossref"
        except Exception as e:
            errors.append("Crossref: " + str(e)[:80])
    if not items:
        try:
            items = fetchers.fetch_semantic_scholar(TRACK_QUERY_S2, 18)
            if items:
                used = "semanticscholar"
        except Exception as e:
            errors.append("Semantic Scholar: " + str(e)[:80])
    if items:
        data = {
            "ok": True,
            "items": items,
            "count": len(items),
            "source": used,
            "fallback_errors": errors,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
            "error": None,
        }
    else:
        data = {
            "ok": False,
            "items": [],
            "count": 0,
            "source": None,
            "fallback_errors": errors,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
            "error": "；".join(errors)[:200] or "三个平台都没返回结果",
        }
    _arxiv_cache["data"] = data
    _arxiv_cache["ts"] = time.time()
    return data


# ---------------------------------------------------------------------------
# 热点·前沿统一资讯流（六类来源合并，10 分钟缓存）
# ---------------------------------------------------------------------------
_news_cache = {"data": None, "ts": 0, "days": None}
NEWS_CACHE_TTL = 10 * 60


FEED_CACHE_MAX_AGE = 3 * 24 * 3600   # 热点流落盘缓存最长保留：3 天


def _feed_cache_file(days):
    return os.path.join(DATA_DIR, "feed_cache_d%s.json" % int(days))


def _load_feed_disk(days):
    """读取落盘的热点流缓存：服务重启后首屏直接有内容，不必干等抓取。"""
    d = _read_json(_feed_cache_file(days), {})
    if not isinstance(d, dict):
        return None, 0.0
    return (d.get("data") or None), float(d.get("ts") or 0)


def _save_feed_disk(days, data):
    try:
        _write_json(_feed_cache_file(days), {"data": data, "ts": time.time()})
    except Exception:
        pass


_feed_fetching = {"d1": False, "d2": False}


def _feed_refresh_bg(days):
    """后台重抓热点流并落盘（失败静默，旧内容继续留在页面上）。

    同一时刻只允许一个抓取在跑：抓一次要几十秒到两三分钟，页面在 loading 期间
    会每 15 秒回来问一次，没有这个闸就会并发起好几条重抓。
    """
    key = "d%s" % int(days)
    if _feed_fetching.get(key):
        return
    _feed_fetching[key] = True
    try:
        data = fetchers.fetch_news_feed(days=days)
        data["ok"] = bool(data.get("items"))
        data["count"] = len(data.get("items") or [])
        data["fetched_at"] = data.get("generated")
        data["error"] = "；".join(data.get("errors") or [])[:160] or None
        _news_cache["data"] = data
        _news_cache["ts"] = time.time()
        _news_cache["days"] = days
        _save_feed_disk(days, data)
    except Exception:
        pass
    finally:
        _feed_fetching[key] = False


def get_news_feed(days=2, force=False):
    if (not force and _news_cache["data"] and _news_cache["days"] == days
            and (time.time() - _news_cache["ts"]) < NEWS_CACHE_TTL):
        return dict(_news_cache["data"], from_cache=True)
    # 内存里没有：先看落盘缓存（启动首屏走这里，毫秒级返回）
    disk_data, disk_ts = _load_feed_disk(days)
    if not force and disk_data:
        age = time.time() - disk_ts
        if age < NEWS_CACHE_TTL:
            _news_cache["data"] = disk_data
            _news_cache["ts"] = disk_ts
            _news_cache["days"] = days
            return dict(disk_data, from_cache=True)
        if age < FEED_CACHE_MAX_AGE:
            _news_cache["data"] = disk_data
            _news_cache["ts"] = disk_ts
            _news_cache["days"] = days
            threading.Thread(target=_feed_refresh_bg, args=(days,), daemon=True).start()
            return dict(disk_data, from_cache=True, stale=True)
    # 一份缓存都没有（刚装好 / 服务刚重启还没落盘）：不让浏览器干等一两分钟，
    # 先回 loading 占位，抓取丢后台线程，页面十几秒后再来问一次。
    if not force:
        threading.Thread(target=_feed_refresh_bg, args=(days,), daemon=True).start()
        return {"ok": True, "items": [], "loading": True, "days": days,
                "generated": "", "cats": [], "error": None}
    data = fetchers.fetch_news_feed(days=days)
    data["ok"] = bool(data.get("items"))
    data["count"] = len(data.get("items") or [])
    data["fetched_at"] = data.get("generated")
    data["error"] = "；".join(data.get("errors") or [])[:160] or None
    _news_cache["data"] = data
    _news_cache["ts"] = time.time()
    _news_cache["days"] = days
    _save_feed_disk(days, data)
    return data


# ---------------------------------------------------------------------------
# 前沿瞭望：双通道归档（领域前沿 + 深度评述，每期独立 HTML）
# ---------------------------------------------------------------------------
def list_frontier():
    """扫描前沿瞭望归档目录，按文件名倒序返回 .html 列表。
    命名约定：YYYY-MM-DD.html（每天一期，双通道合并排版）
    """
    os.makedirs(FRONTIER_DIR, exist_ok=True)
    files = []
    for fn in sorted(os.listdir(FRONTIER_DIR), reverse=True):
        if fn.endswith(".html") and not fn.startswith("."):
            path = os.path.join(FRONTIER_DIR, fn)
            try:
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
            except OSError:
                continue
            files.append({
                "file": fn,
                "date": fn[:-5],
                "size": size,
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
            })
    return {"items": files, "count": len(files)}


def read_frontier(filename):
    """读取指定一期前沿瞭望的原始 HTML。防目录穿越。"""
    safe = os.path.basename(filename)
    path = os.path.join(FRONTIER_DIR, safe)
    if not os.path.isfile(path) or not os.path.realpath(path).startswith(os.path.realpath(FRONTIER_DIR)):
        return None
    if not safe.endswith(".html"):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 热点日报：扫描归档目录 + 读取原始 HTML（每期独立完整 HTML，归档保留）
# ---------------------------------------------------------------------------
def list_hotspots():
    """扫描热点日报归档目录，按文件名倒序返回 .html 列表。
    命名约定：YYYY-MM-DD_上午版.html / YYYY-MM-DD_下午版.html
    """
    os.makedirs(HOTSPOT_DIR, exist_ok=True)
    files = []
    for fn in sorted(os.listdir(HOTSPOT_DIR), reverse=True):
        if fn.endswith(".html") and not fn.startswith("."):
            path = os.path.join(HOTSPOT_DIR, fn)
            try:
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
            except OSError:
                continue
            stem = fn[:-5]
            date, _, session = stem.partition("_")
            files.append({
                "file": fn,
                "date": date,
                "session": session or "日报",
                "size": size,
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
            })
    return {"items": files, "count": len(files)}


def read_hotspot(filename):
    """读取指定热点日报的原始 HTML。防目录穿越。"""
    safe = os.path.basename(filename)
    path = os.path.join(HOTSPOT_DIR, safe)
    if not os.path.isfile(path) or not os.path.realpath(path).startswith(os.path.realpath(HOTSPOT_DIR)):
        return None
    if not safe.endswith(".html"):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "PhDWorkbench/1.0"

    # ---------- 工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _log(self, *args):
        pass  # 静默访问日志，保持终端干净

    def _proxy_worker(self, method, full_path, body=None,
                      ctype="application/json; charset=utf-8", timeout=600):
        """把请求原样转发给 PDF worker（8766），浏览器只与主服务 8765 通信。"""
        url = WORKER_BASE + full_path
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else body.encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": ctype},
        )
        # 本地回环必须绕过系统代理
        proxy_handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy_handler)
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                rctype = resp.headers.get("Content-Type", "application/json; charset=utf-8")
                self.send_response(resp.status)
                self.send_header("Content-Type", rctype)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except (urllib.error.URLError, ConnectionError, OSError):
            self._send(503, {"ok": False, "error": "PDF 转写引擎未启动（pdf_worker 8766 未运行），请先运行 start.command 或手动启动 worker"})

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_static("/index.html")
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path == "/api/overview":
            self._send(200, {
                "phd": phd_progress(),
                "graduation": graduation_progress(),
                "sections": section_stats(),
                "tree": scan_tree(WORKSPACE, max_depth=2),
                "field_name": FIELD_NAME,
            })
        elif path == "/api/news":
            self._send(200, get_news(force="refresh" in query and query["refresh"][0] == "1"))
        elif path == "/api/weather":
            self._send(200, get_weather())
        elif path == "/api/weekly":
            force = "refresh" in query and query["refresh"][0] == "1"
            self._send(200, get_weekly(force=force))
        elif path == "/api/todos":
            self._send(200, get_todos())
        elif path == "/api/ablesci/points":
            pts = ablesci_points()
            j = _read_json(POINTS_FILE, {})
            self._send(200, {"ok": True, "points": pts,
                             "at": j.get("at") if isinstance(j, dict) else None,
                             "low": pts is not None and pts < 10})
        elif path == "/api/ablesci/queue":
            data = ablesci_queue()
            pend = [x for x in data["items"] if x.get("status") == "pending"]
            done = [x for x in data["items"] if x.get("status") != "pending"][-6:]
            done.sort(key=lambda x: x.get("finished") or "", reverse=True)
            self._send(200, {
                "ok": True,
                "pending": pend,
                "done": done,
                "total": len(data["items"]),
                "server_time": time.strftime("%H:%M"),
            })
        elif path == "/api/groups":
            kind = query.get("kind", ["summary"])[0]
            self._send(200, {"ok": True, "kind": kind, **library_groups(kind)})
        elif path == "/api/entry/names":
            kind = query.get("kind", [""])[0]
            self._send(200, {"ok": True, "names": entry_names(kind or None)})
        elif path == "/api/pdf-inbox":
            inbox = pdf_inbox_dir()
            self._send(200, {
                "ok": True,
                "dir": inbox,
                "auto": pdf_inbox_auto(),
                "transcribe": pdf_auto_transcribe(),
                "exists": os.path.isdir(inbox),
            })
        elif path == "/api/pdf-inbox/files":
            self._send(200, {"ok": True, "dir": pdf_inbox_dir(),
                             "files": pdf_inbox_files()})
        elif path == "/api/journal":
            self._send(200, get_journal())
        elif path.startswith("/api/literature/"):
            name = path.split("/")[-1]
            self._send(200, load_literature(name))
        elif path == "/api/lit/arxiv":
            force = "refresh" in query and query["refresh"][0] == "1"
            self._send(200, get_arxiv_feed(force=force))
        elif path == "/api/publications":
            self._send(200, {"publications": get_publications(), "graduation": graduation_progress()})
        elif path == "/api/frontier":
            self._send(200, list_frontier())
        elif path == "/api/frontier/file":
            filename = query.get("file", [""])[0]
            content = read_frontier(filename)
            if content is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, {"file": filename, "content": content})
        elif path.startswith("/frontier/"):
            # 供「新标签页打开」直接加载的前沿瞭望原始 HTML
            filename = unquote(path[len("/frontier/"):])
            content = read_frontier(filename)
            if content is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, content, ctype="text/html; charset=utf-8")
        elif path == "/api/news/feed":
            # 热点·前沿统一资讯流：六类内容合并，每条带原文链接
            force = "refresh" in query and query["refresh"][0] == "1"
            try:
                days = int(query.get("days", ["2"])[0])
            except Exception:
                days = 2
            try:
                self._send(200, get_news_feed(days=days, force=force))
            except Exception as e:
                self._send(200, {"ok": False, "items": [], "errors": [str(e)[:120]],
                                 "generated": "", "cats": []})
        elif path == "/api/hotspots":
            self._send(200, list_hotspots())
        elif path == "/api/hotspot":
            filename = query.get("file", [""])[0]
            content = read_hotspot(filename)
            if content is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, {"file": filename, "content": content})
        elif path.startswith("/hotspot/"):
            # 供 iframe 直接加载的原始热点日报 HTML
            filename = unquote(path[len("/hotspot/"):])
            content = read_hotspot(filename)
            if content is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, content, ctype="text/html; charset=utf-8")
        elif path == "/api/pdf/recent":
            # 最近转写完的任务（按结果文件时间倒序）：页面刷新后用它把「转写结果」自动铺回来
            self._send(200, {"ok": True, "jobs": recent_pdf_jobs()})
        elif path.startswith("/api/pdf/"):
            # PDF 转写：健康检查 / 状态轮询 / 取结果 / 取图片，全部转发 worker
            self._proxy_worker("GET", self.path)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/todos":
            body = self._body()
            todos = get_todos()
            action = body.get("action", "add")
            if action == "add":
                item = {
                    "id": int(time.time() * 1000),
                    "text": body.get("text", "").strip(),
                    "done": False,
                    "created": time.strftime("%Y-%m-%d %H:%M"),
                    "deadline": body.get("deadline", ""),
                    "priority": body.get("priority", "普通"),
                }
                if item["text"]:
                    todos.append(item)
                    save_todos(todos)
                    return self._send(200, {"ok": True, "todos": todos})
                return self._send(400, {"error": "待办内容为空"})
            elif action == "toggle":
                for t in todos:
                    if t["id"] == body.get("id"):
                        t["done"] = not t["done"]
                        break
                save_todos(todos)
                return self._send(200, {"ok": True, "todos": todos})
            elif action == "delete":
                todos = [t for t in todos if t["id"] != body.get("id")]
                save_todos(todos)
                return self._send(200, {"ok": True, "todos": todos})

        elif path == "/api/journal":
            body = self._body()
            entries = get_journal()
            action = body.get("action", "add")
            if action == "delete":
                entry_id = body.get("id")
                before = len(entries)
                entries = [e for e in entries if e.get("id") != entry_id]
                if len(entries) < before:
                    save_journal(entries)
                    return self._send(200, {"ok": True, "journal": entries})
                return self._send(404, {"error": "日志不存在"})
            else:
                entry = {
                    "id": int(time.time() * 1000),
                    "date": body.get("date") or time.strftime("%Y-%m-%d"),
                    "type": body.get("type", "日常"),
                    "content": body.get("content", "").strip(),
                    "created": time.strftime("%Y-%m-%d %H:%M"),
                }
                if entry["content"]:
                    entries.insert(0, entry)
                    save_journal(entries)
                    return self._send(200, {"ok": True, "journal": entries})
                return self._send(400, {"error": "日志内容为空"})

        elif path == "/api/publications":
            body = self._body()
            action = body.get("action", "list")
            if action == "add":
                pubs = get_publications()
                pub = {
                    "id": int(time.time() * 1000),
                    "title": body.get("title", "").strip(),
                    "type": body.get("type", "c_journal"),
                    "journal": body.get("journal", "").strip(),
                    "date": body.get("date", "").strip(),
                    "note": body.get("note", "").strip(),
                    "created": time.strftime("%Y-%m-%d %H:%M"),
                }
                if pub["title"]:
                    pubs.append(pub)
                    save_publications(pubs)
                    return self._send(200, {"ok": True, "publications": pubs, "graduation": graduation_progress()})
                return self._send(400, {"error": "论文标题为空"})
            elif action == "delete":
                pubs = get_publications()
                pub_id = body.get("id")
                before = len(pubs)
                pubs = [p for p in pubs if p.get("id") != pub_id]
                if len(pubs) < before:
                    save_publications(pubs)
                    return self._send(200, {"ok": True, "publications": pubs, "graduation": graduation_progress()})
                return self._send(404, {"error": "论文不存在"})
            else:
                return self._send(200, {"publications": get_publications(), "graduation": graduation_progress()})

        elif path == "/api/ablesci/queue":
            body = self._body()
            doi = (body.get("doi") or "").strip()
            title = (body.get("title") or "").strip()
            link = (body.get("link") or "").strip()
            if not doi and not title:
                return self._send(400, {"error": "缺少 doi 或 title"})
            return self._send(200, ablesci_enqueue(doi, title, link))

        elif path == "/api/ablesci/download":
            # 「下载全文」按钮：点了就立刻取，不排队、不反问
            body = self._body()
            doi = (body.get("doi") or "").strip()
            title = (body.get("title") or "").strip()
            if not doi:
                return self._send(400, {"error": "这篇没有 DOI，取不了全文"})
            dl_path, why = _try_oa_download(doi, title)
            if dl_path:
                v = verify_pdf_file(dl_path, title, doi)
                if v and v.get("ok"):
                    r = _ingest_pdf(dl_path, title or doi)
                    if r.get("ok"):
                        _log_ablesci("下载全文 | OA 直下并归档：%s | DOI %s" % (r.get("name"), doi))
                        return self._send(200, {"ok": True, "name": r.get("name"),
                                                "transcribe": r.get("transcribe"),
                                                "jobId": r.get("jobId")})
                    return self._send(500, {"ok": False, "error": r.get("error") or "归档失败"})
                return self._send(200, {"ok": False, "error": "下到的 PDF 无法核验为同一篇"})
            cands = [c[0] for c in _oa_pdf_candidates(doi) if c and c[0] != "__direct__"]
            if cands:
                # 出版商拦服务端（MDPI 等 403）：把直链交给浏览器下载，并登记「下载后自动归档」
                _pending_download_add(doi, title, cands[0])
                return self._send(200, {"ok": False, "need_browser": cands[0],
                                        "error": "开放获取，已交给浏览器下载"})
            # 闭源：入队等发求助（agent 在对话里或整点巡检时处理）
            ablesci_enqueue(doi, title)
            _log_ablesci("下载全文 | 闭源，已入队待发求助 | DOI %s" % doi)
            pts = ablesci_points()
            low = pts is not None and pts < 10
            return self._send(200, {"ok": False,
                                    "error": "闭源，已加入待发求助队列",
                                    "points": pts, "points_low": low})

        elif path == "/api/ablesci/queue/finish":
            body = self._body()
            qid = (body.get("id") or "").strip()
            status = (body.get("status") or "done").strip()
            note = body.get("note") or ""
            if not qid:
                return self._send(400, {"error": "缺少 id"})
            if status not in ("done", "failed", "skip"):
                return self._send(400, {"error": "status 只能是 done/failed/skip"})
            data = ablesci_queue()
            hit = False
            for it in data["items"]:
                if it.get("id") == qid:
                    it["status"] = status
                    it["note"] = note
                    it["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    hit = True
            if not hit:
                return self._send(404, {"error": "队列项不存在"})
            ablesci_queue_save(data)
            return self._send(200, {"ok": True, "pending": len(
                [x for x in data["items"] if x.get("status") == "pending"])})

        elif path == "/api/pdf-inbox":
            body = self._body()
            cfg_path = os.path.join(DATA_DIR, "settings.json")
            data = _read_json(cfg_path, {}) or {}
            if "auto" in body:
                data["pdf_auto_archive"] = bool(body.get("auto"))
            if "transcribe" in body:
                data["pdf_auto_transcribe"] = bool(body.get("transcribe"))
            new_dir = (body.get("dir") or "").strip()
            if new_dir:
                data["pdf_inbox_dir"] = new_dir
            try:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                return self._send(500, {"error": "写入失败：%s" % exc})
            inbox = pdf_inbox_dir()
            try:
                os.makedirs(inbox, exist_ok=True)
            except Exception:
                pass
            return self._send(200, {"ok": True, "dir": inbox, "auto": pdf_inbox_auto(),
                                    "transcribe": pdf_auto_transcribe()})

        elif path == "/api/pdf/from-inbox":
            # 「PDF 转写」面板里一键转写文献库里的某份 PDF（服务端直接提交给 worker）
            body = self._body()
            name = (body.get("name") or "").strip()
            if not name:
                return self._send(400, {"error": "缺少 name"})
            src = os.path.join(pdf_inbox_dir(), os.path.basename(name))
            if not os.path.isfile(src):
                return self._send(404, {"error": "文献库里没有这个文件：%s" % name})
            engine = (body.get("engine") or "local").strip()
            if engine not in ("local", "cloud"):
                engine = "local"
            res, err = submit_inbox_pdf(src, engine=engine,
                                        formula=body.get("formula", True),
                                        table=body.get("table", True))
            if res is None:
                return self._send(502, {"error": "提交转写失败：%s" % err})
            return self._send(200, res)

        elif path == "/api/pdf-inbox/ingest":
            # 把一个已下载的 PDF 收进文献库，并按论文标题重命名
            body = self._body()
            src = (body.get("file") or "").strip()
            title = body.get("title") or ""
            res = _ingest_pdf(src, title,
                              transcribe=body.get("transcribe"),
                              engine=(body.get("engine") or "local"))
            return self._send(200 if res.get("ok") else 400, res)

        elif path == "/api/groups":
            # 建组 / 改名 / 删组 / 条目进出组
            body = self._body()
            kind = (body.get("kind") or "summary").strip()
            action = (body.get("action") or "").strip()
            res = library_groups_edit(kind, action,
                                      name=body.get("name") or "",
                                      group_id=body.get("groupId") or "",
                                      entry_id=body.get("entryId") or "",
                                      on=body.get("on"))
            return self._send(200 if res.get("ok") else 400, res)

        elif path == "/api/entry/rename":
            # 给某一条摘要/译文/精读起个别名（只改展示名）
            body = self._body()
            res = entry_rename(body.get("kind") or "summary",
                               body.get("id") or "", body.get("name") or "")
            return self._send(200 if res.get("ok") else 400, res)

        elif path == "/api/graduation":
            body = self._body()
            key = (body.get("key") or "").strip()
            if not key:
                return self._send(400, {"error": "缺少环节 key"})
            track = grad_track()
            found = False
            for it in track["items"]:
                if it.get("key") == key:
                    if "done" in body:
                        it["done"] = bool(body.get("done"))
                    else:
                        it["done"] = not it.get("done")
                    found = True
                    break
            if not found:
                return self._send(404, {"error": "环节不存在"})
            data = _read_json(GRAD_TRACK_FILE, {}) or {}
            data["items"] = track["items"]
            data["updated"] = time.strftime("%Y-%m-%d")
            if track.get("basis"):
                data["basis"] = track["basis"]
            try:
                with open(GRAD_TRACK_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                return self._send(500, {"error": "写入失败：%s" % exc})
            return self._send(200, {"ok": True, "graduation": graduation_progress()})

        elif path == "/api/open":
            body = self._body()
            raw = body.get("path", "")
            # 支持相对路径（如 "01_文献库"、"data/hotspots"）和绝对路径
            # 相对路径先按资料库根（WORKSPACE）解析，找不到再按程序目录（BASE_DIR）解析，
            # 因为日报/摘要/译文这些程序产物都落在程序目录的 data/ 下。
            if os.path.isabs(raw):
                target = os.path.normpath(raw)
            else:
                target = os.path.normpath(os.path.join(WORKSPACE, raw))
                if not os.path.exists(target):
                    alt = os.path.normpath(os.path.join(BASE_DIR, raw))
                    if os.path.exists(alt):
                        target = alt
            roots = [os.path.normpath(WORKSPACE), os.path.normpath(BASE_DIR)]
            inside = any(target == r or target.startswith(r + os.sep) for r in roots)
            if inside and (os.path.isdir(target) or os.path.isfile(target)):
                ok = open_in_finder(target)
                return self._send(200, {"ok": bool(ok), "path": target})
            return self._send(400, {"error": "非法路径: " + target})

        elif path == "/api/refresh":
            data = get_news(force=True)
            return self._send(200, data)

        elif path == "/api/pdf/upload":
            # 前端拖拽上传：请求体是 PDF 原始字节，原样转发给 worker
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            return self._proxy_worker("POST", self.path, raw, "application/pdf")

        elif path.startswith("/api/pdf/"):
            # submit 等 JSON 请求
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            return self._proxy_worker("POST", self.path, raw, "application/json; charset=utf-8")

        self._send(404, {"error": "not found"})

    def _serve_static(self, rel):
        # 防目录穿越
        target = os.path.normpath(os.path.join(WEB_DIR, rel.lstrip("/")))
        if not target.startswith(WEB_DIR) or not os.path.isfile(target):
            return self._send(404, {"error": "not found"}, "application/json; charset=utf-8")
        ext = os.path.splitext(target)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        with open(target, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 本地工具，前端随时在改：一律走协商缓存，别让浏览器拿旧 app.js/style.css
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# 每日自动维护：git 自动存档 + data 轻量快照（保 14 天）+ pdf_jobs 去重瘦身
# 由 main() 启动 daemon 线程，每天 ≥03:00 执行一次；随 start.command 常驻，
# 不依赖任何外部会话。快照目录 data_snapshots/ 不入 git（见 .gitignore）。
# ---------------------------------------------------------------------------
BACKUP_STATE_FILE = os.path.join(DATA_DIR, "backup_state.json")
SNAPSHOT_ROOT = os.path.join(BASE_DIR, "data_snapshots")

# 不进快照的文件：给 data/ 做镜像会把 api_key / token 复制成十几份明文
# （保留 keep_days 天），一旦整个目录被拷走或打包就跟着泄露；这些配置本来也能重建。
SNAPSHOT_SKIP_FILES = {
    "llm_config.json",      # 百炼 api_key
    "pdf_config.json",      # MinerU cloud_token
    "weather_config.json",  # 和风 Host / Key
    "settings.json",        # 学制 / 本地扫描目录
}
JOB_TS_RE = re.compile(r"^(.+)_(\d{8})_(\d{6})_[a-z0-9]{4}$")


def _log_maint(msg):
    print("[maint] " + msg, flush=True)


def _dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def pdf_inbox_dir():
    """「下载到固定文件夹」的落点：默认 <资料库根>/01_文献库/原文PDF。
    可用 data/settings.json 的 pdf_inbox_dir 覆盖（填绝对路径）。"""
    s = _load_settings()
    d = (s.get("pdf_inbox_dir") or "").strip()
    if not d:
        d = os.path.join(os.path.abspath(WORKSPACE), "01_文献库", "原文PDF")
    return os.path.normpath(d)


def pdf_inbox_auto():
    """下载的 PDF 是否自动归档进文献库。默认开（settings 里 pdf_auto_archive=false 才关）。"""
    return bool(_load_settings().get("pdf_auto_archive", True))


def pdf_auto_transcribe():
    """下载归档的 PDF 是否自动送进「PDF 转写」。默认关，settings 里 pdf_auto_transcribe=true 才自动跑
    （本地解析一条要 1–2 分钟，不该在用户不知情时占满机器）。"""
    return bool(_load_settings().get("pdf_auto_transcribe", False))


JOBS_DIR = os.path.join(BASE_DIR, "data", "pdf_jobs")


def _transcribed_by_size():
    """把文献库里的 PDF 对到已有转写任务上：靠 input.pdf 的字节数做匹配。
    同一份 PDF 尺寸唯一，比按标题截断匹配可靠得多。
    同一尺寸可能有多条任务（点过「再转写」），这里**优先取已经出结果的那条**，
    免得状态在「已转写/转写中」之间来回跳。"""
    done_map = {}
    any_map = {}
    if not os.path.isdir(JOBS_DIR):
        return done_map
    for name in os.listdir(JOBS_DIR):
        d = os.path.join(JOBS_DIR, name)
        ip = os.path.join(d, "input.pdf")
        if not os.path.isfile(ip):
            continue
        try:
            size = os.path.getsize(ip)
            mtime = os.path.getmtime(d)
        except OSError:
            continue
        has_result = os.path.isfile(os.path.join(d, "result.md"))
        if has_result:
            # 同一尺寸有多条已完成的，留最新的那条
            if size not in done_map or mtime > done_map[size]["mtime"]:
                done_map[size] = {"jobId": name, "done": True, "mtime": mtime}
        if size not in any_map or mtime > any_map[size]["mtime"]:
            any_map[size] = {"jobId": name, "done": has_result, "mtime": mtime}
    out = {}
    for size, rec in any_map.items():
        out[size] = done_map.get(size) or rec
    return out


def recent_pdf_jobs(limit=12):
    """按结果文件时间倒序列出最近转写过的任务。
    用途：页面刷新后 localStorage 里的当前任务可能已失效，前端靠这个接口把
    「转写结果」自动铺回来，不至于一直停在一张空状态卡上。"""
    d = os.path.join(DATA_DIR, "pdf_jobs")
    out = []
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        job_dir = os.path.join(d, name)
        if not os.path.isdir(job_dir):
            continue
        rm = os.path.join(job_dir, "result.md")
        if not os.path.isfile(rm):
            continue
        try:
            st = os.stat(rm)
        except OSError:
            continue
        title = name
        m = re.match(r"^(.*)_(\d{8}_\d{6}_[0-9a-f]+)$", name)
        if m:
            title = m.group(1)
        out.append({
            "jobId": name,
            "title": title,
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "bytes": st.st_size,
            "_t": st.st_mtime,
        })
    out.sort(key=lambda x: x["_t"], reverse=True)
    for x in out:
        x.pop("_t", None)
    return out[:limit]


def pdf_inbox_files(with_jobs=True):
    """列出文献库目录里的 PDF，供「PDF 转写」面板一键挑文献。
    with_jobs=True 时顺带标出「是否已经转写过」（按字节数对到已有任务）。"""
    d = pdf_inbox_dir()
    out = []
    if not os.path.isdir(d):
        return out
    idx = _transcribed_by_size() if with_jobs else {}
    for name in sorted(os.listdir(d)):
        if not name.lower().endswith(".pdf"):
            continue
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        rec = idx.get(st.st_size, {})
        out.append({
            "name": name,
            "title": name[:-4],
            "path": p,
            "bytes": st.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "transcribed": bool(rec.get("done")),
            "inProgress": bool(rec) and not rec.get("done"),
            "jobId": rec.get("jobId", ""),
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


_INBOX_TRANS_LOCK = threading.Lock()
INBOX_AUTO_TICK = 30           # 每 30 秒扫一遍文献库：下载完很快就会自己进转写
INBOX_AUTO_STATE = {"last": 0.0, "skipped": 0}


def inbox_autotranscribe_once():
    """文献库里有还没转写过的 PDF，就自动提交一条（一轮只提一条，避免把机器占满）。
    这样不管 PDF 是从哪条链路进来的（求助全文、浏览器下载兜底、手工丢进目录），
    都会自动进转写，不用再逐个点「转写」。"""
    if not pdf_auto_transcribe():
        return {"ok": False, "reason": "auto_transcribe=false"}
    with _INBOX_TRANS_LOCK:
        for f in pdf_inbox_files(with_jobs=True):
            if f.get("transcribed"):
                continue
            jid = f.get("jobId") or ""
            if jid:
                # 已有任务：两小时内算「还在跑」，别重复提交；超过两小时还没结果就是卡死了，重提一次
                try:
                    age = time.time() - os.path.getmtime(os.path.join(JOBS_DIR, jid))
                except OSError:
                    age = 0
                if age < 2 * 3600:
                    continue
            job, err = submit_inbox_pdf(f["path"], engine="local")
            if job and job.get("ok"):
                _log_ablesci("文献库自动转写：%s → %s" % (f["name"], job.get("jobId")))
                return {"ok": True, "name": f["name"], "jobId": job.get("jobId")}
            INBOX_AUTO_STATE["skipped"] += 1
            _log_ablesci("文献库自动转写失败：%s（%s）" % (f["name"], err or (job or {}).get("error", "")))
            return {"ok": False, "name": f["name"], "error": err or (job or {}).get("error", "")}
    return {"ok": True, "reason": "nothing to do"}


def start_inbox_autotranscribe():
    def loop():
        while True:
            try:
                inbox_autotranscribe_once()
            except Exception:
                pass
            time.sleep(INBOX_AUTO_TICK)
    t = threading.Thread(target=loop, name="inbox-autotranscribe", daemon=True)
    t.start()
    return t


def submit_inbox_pdf(path, engine="local", formula=True, table=True):
    """把文献库里的 PDF 直接提交给 worker 转写（不走前端上传，省一次拷贝）。"""
    if not os.path.isfile(path):
        return None, "文件不存在: " + path
    payload = json.dumps({"pdfPath": path, "engine": engine,
                          "formula": bool(formula), "table": bool(table)}).encode("utf-8")
    req = urllib.request.Request(WORKER_BASE + "/api/pdf/submit", data=payload, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except Exception as exc:
        return None, str(exc)


_WIN_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _ingest_pdf(src, title, transcribe=None, engine="local"):
    """把一个已下载的 PDF 收进文献库：校验文件头 → 按标题重命名 → 可选送转写。
    抽成函数是为了让「归档接口」和「队列自动争取 OA」共用同一套落盘规则。"""
    if not src or not os.path.isfile(src):
        return {"ok": False, "error": "源文件不存在：%s" % src}
    if not src.lower().endswith(".pdf"):
        return {"ok": False, "error": "只接受 PDF 文件"}
    with open(src, "rb") as fh:
        head = fh.read(5)
    if head != b"%PDF-":
        return {"ok": False, "error": "不是有效 PDF（文件头 %r）" % head}
    inbox = pdf_inbox_dir()
    try:
        os.makedirs(inbox, exist_ok=True)
    except Exception as exc:
        return {"ok": False, "error": "无法创建目录：%s" % exc}
    name = safe_pdf_name(title, os.path.splitext(os.path.basename(src))[0])
    dest = os.path.join(inbox, name)
    base, ext = os.path.splitext(dest)
    n = 2
    while os.path.exists(dest):
        dest = "%s (%d)%s" % (base, n, ext)
        n += 1
    try:
        shutil.move(src, dest)
    except Exception as exc:
        return {"ok": False, "error": "移动失败：%s" % exc}
    want_trans = pdf_auto_transcribe() if transcribe is None else bool(transcribe)
    job = None
    if want_trans:
        job, jerr = submit_inbox_pdf(dest, engine=engine)
        if job and not job.get("ok"):
            job = None
    return {"ok": True, "dest": dest, "name": os.path.basename(dest),
            "bytes": os.path.getsize(dest),
            "transcribe": bool(job and job.get("ok")),
            "jobId": (job or {}).get("jobId") if job else None}


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
# Unpaywall 要求带一个联系邮箱；这里用工作台的专用地址，不写用户的私人邮箱
UNPAYWALL_EMAIL = "doubao.workbench@gmail.com"


def _http_get(url, timeout=25, max_bytes=64 * 1024 * 1024):
    """带 UA 的 GET（同域重定向自动跟随），返回 (最终 URL, 内容类型, 字节)。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl(), (resp.headers.get("Content-Type") or ""), resp.read(max_bytes)


def _oa_pdf_candidates(doi):
    """给一个 DOI，按可靠度列出「最可能的 PDF 直链」。
    顺序：OpenAlex 的 best_oa_location → Unpaywall → Crossref 的 TDM link →
    doi.org 落地页里的 citation_pdf_url。
    拿不到就返回空，交给人工/登录态发求助。"""
    doi = (doi or "").strip()
    if not doi:
        return []
    cands = []
    # 1) OpenAlex：免注册、覆盖最广，OA 判定与 pdf_url 都直接给
    try:
        _, _, raw = _http_get("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi),
                              timeout=20, max_bytes=2 * 1024 * 1024)
        j = json.loads(raw.decode("utf-8", "ignore"))
        best = j.get("best_oa_location") or {}
        if best.get("pdf_url"):
            cands.append(best["pdf_url"])
        oa = j.get("open_access") or {}
        if oa.get("oa_url"):
            cands.append(oa["oa_url"])
        for loc in (j.get("locations") or [])[:6]:
            if loc.get("pdf_url"):
                cands.append(loc["pdf_url"])
    except Exception:
        pass
    # 2) Unpaywall（需要一个邮箱参数，用工作台的专用地址，不用用户的）
    try:
        u = ("https://api.unpaywall.org/v2/%s?email=%s"
             % (urllib.parse.quote(doi), urllib.parse.quote(UNPAYWALL_EMAIL)))
        _, _, raw = _http_get(u, timeout=20, max_bytes=1024 * 1024)
        j = json.loads(raw.decode("utf-8", "ignore"))
        for loc in ([j.get("best_oa_location")] + (j.get("oa_locations") or []))[:6]:
            if loc and loc.get("url_for_pdf"):
                cands.append(loc["url_for_pdf"])
    except Exception:
        pass
    # 3) Crossref 的 TDM link（部分出版商允许）
    try:
        _, _, raw = _http_get("https://api.crossref.org/works/" + urllib.parse.quote(doi),
                              timeout=20, max_bytes=2 * 1024 * 1024)
        msg = json.loads(raw.decode("utf-8", "ignore")).get("message", {})
        for lk in (msg.get("link") or []):
            u = lk.get("URL") or ""
            if u.lower().endswith(".pdf") or "pdf" in (lk.get("content-type") or ""):
                cands.append(u)
    except Exception:
        pass
    # 4) doi.org 落地页的 citation_pdf_url
    try:
        final, ctype, raw = _http_get("https://doi.org/" + urllib.parse.quote(doi),
                                      timeout=25, max_bytes=3 * 1024 * 1024)
        if "pdf" in (ctype or "").lower() and raw[:5] == b"%PDF-":
            return [("__direct__", final, raw)]
        html = raw.decode("utf-8", "ignore")
        m = re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']', html, re.I)
        if m:
            cands.append(m.group(1))
    except Exception:
        pass
    seen, out = set(), []
    for c in cands:
        if isinstance(c, tuple):
            out.append(c); continue
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c); out.append((c, None, None))
    return out


def _try_oa_download(doi, want_title=""):
    """尝试把 OA 版 PDF 落到临时目录；成功返回 (路径, 说明)，否则 (None, 原因)。"""
    tmpdir = os.path.join(DATA_DIR, "downloads")
    os.makedirs(tmpdir, exist_ok=True)
    for url, _f, _raw in _oa_pdf_candidates(doi):
        if url == "__direct__":
            data, src = _raw, _f
        else:
            try:
                final, ctype, data = _http_get(url, timeout=30, max_bytes=64 * 1024 * 1024)
            except Exception as exc:
                last = "下载失败(%s)：%s" % (url, exc)
                continue
        if not data or data[:5] != b"%PDF-":
            last = "不是 PDF：%s" % url
            continue
        fname = re.sub(r'[^\w\-]+', '_', doi)[:60] + ".pdf"
        path = os.path.join(tmpdir, fname)
        with open(path, "wb") as fh:
            fh.write(data)
        return path, "OA 直下：" + (url if url != "__direct__" else "doi.org")
    return None, locals().get("last", "没有可直下的 OA 链接")


VERIFY_PY = os.path.join(BASE_DIR, "tools", "verify_pdf.py")
VERIFY_PYTHON = os.path.join(BASE_DIR, "pdf_worker", ".venv", "Scripts", "python.exe")


def verify_pdf_file(path, title="", doi=""):
    """调 tools/verify_pdf.py 判断「这份 PDF 是不是这篇」。ok=无法核验时会返回 None 交给人工。"""
    if not os.path.isfile(VERIFY_PY) or not os.path.isfile(VERIFY_PYTHON):
        return None
    cmd = [VERIFY_PYTHON, VERIFY_PY, path]
    if title:
        cmd += ["--title", title]
    if doi:
        cmd += ["--doi", doi]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        line = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else ""
        return json.loads(line) if line else None
    except Exception:
        return None


_QUEUE_LOCK = threading.Lock()
AUTO_CHECK_GAP = 6 * 3600      # 同一篇最多每 6 小时自动再试一次，避免反复打出版商
QUEUE_TICK = 60                # 每 60 秒扫一次队列：这才是「点完两分钟内有结果」的关键


def queue_autopass_once():
    """自动争取队列里的开放获取全文：能直下就归档+送转写，不能就标注「需要登录态发求助」。
    为什么要这一步：真正的「发求助」必须用用户浏览器里的登录态，服务端拿不到；
    但「判 OA + 直下」不需要登录，放在服务端做，绝大多数 OA 论文就能在两分钟内落地。"""
    with _QUEUE_LOCK:
        data = ablesci_queue()
        items = data.get("items", [])
        pend = [x for x in items if x.get("status") == "pending"]
        changed = False
        for it in pend:
            doi = (it.get("doi") or "").strip()
            title = it.get("title") or ""
            if not doi:
                it["note"] = "缺少 DOI，需人工发求助"
                changed = True
                continue
            try:
                lc = float(it.get("auto_check_at") or 0)
            except Exception:
                lc = 0.0
            if time.time() - lc < AUTO_CHECK_GAP and it.get("oa_state") in ("no", "done", "oa_link", "assist_pending"):
                continue
            path, why = _try_oa_download(doi, title)
            it["auto_check_at"] = str(int(time.time()))
            it["auto_check"] = time.strftime("%m-%d %H:%M")
            changed = True
            cands = [c[0] for c in _oa_pdf_candidates(doi) if isinstance(c, tuple) and c[0] != "__direct__"]
            it["oa_pdf_url"] = cands[0] if cands else ""
            if path:
                v = verify_pdf_file(path, title, doi)
                if v and v.get("ok"):
                    r = _ingest_pdf(path, title or doi)
                    if r.get("ok"):
                        it["oa_state"] = "done"
                        it["status"] = "done"
                        it["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        it["note"] = "OA 直下并已归档" + ("（已送转写）" if r.get("transcribe") else "")
                        it["pdf"] = r.get("name")
                        _log_ablesci("自动队列 | OA 直下已归档：%s | DOI %s" % (it["note"], doi))
                        continue
                    it["oa_state"] = "verify_fail"
                    it["note"] = "下载成功但归档失败：%s" % (r.get("error") or "")
                else:
                    it["oa_state"] = "verify_fail"
                    it["note"] = "下到了 PDF 但标题核验不通过（疑似不是这篇），已保留在 data/downloads"
            elif it["oa_pdf_url"]:
                # 论文是 OA、直链也找到了，只是出版商拦「非浏览器」请求（如 MDPI 的 403）
                it["oa_state"] = "oa_link"
                it["note"] = "开放获取可直下，但出版商拦服务端；点右侧「PDF 直链」即可秒下"
            else:
                it["oa_state"] = "no"
                it["note"] = "已判非 OA / 直下不可用，需登录态发求助（%s）" % why[:60]
        if changed:
            ablesci_queue_save(data)


def _log_ablesci(line):
    try:
        with open(os.path.join(DATA_DIR, "ablesci_daily.log"), "a", encoding="utf-8") as f:
            f.write("%s | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except Exception:
        pass


def start_queue_autopass():
    """后台线程：常驻扫队列。用 daemon 线程而不是定时任务，是因为
    cron 最小间隔被平台限制为 1 小时（实测 minExecInterval=3600），做不到「两分钟」。"""
    def loop():
        while True:
            try:
                queue_autopass_once()
            except Exception as exc:
                _log_ablesci("自动队列异常：%s" % exc)
            time.sleep(QUEUE_TICK)
    t = threading.Thread(target=loop, name="ablesci-autopass", daemon=True)
    t.start()
    return t


def ablesci_queue():
    """「求助全文」按钮的待办队列：页面点一下就入队，由定时巡检任务真正去搜索/发求助。"""
    data = _read_json(ABLESCI_QUEUE_FILE, {"items": []})
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        data = {"items": []}
    return data


def ablesci_queue_save(data):
    items = data.get("items", [])
    # 只保留最近 30 条已完成的，避免文件无限增长
    done = [x for x in items if x.get("status") != "pending"][-30:]
    pending = [x for x in items if x.get("status") == "pending"]
    data["items"] = pending + done
    with open(ABLESCI_QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def ablesci_enqueue(doi, title, link=""):
    """入队（同 DOI / 同标题去重）。「下载全文」按钮和巡检任务共用这一处。"""
    data = ablesci_queue()
    for it in data["items"]:
        if it.get("status") == "pending" and (
            (doi and (it.get("doi") or "").lower() == doi.lower())
            or (not doi and title and it.get("title") == title)
        ):
            return {"ok": True, "queued": False,
                    "pending": len([x for x in data["items"] if x.get("status") == "pending"])}
    entry = {
        "id": time.strftime("%Y%m%d%H%M%S") + "-" + str(int(time.time() * 1000) % 1000),
        "doi": doi,
        "title": title,
        "link": link,
        "status": "pending",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    data["items"].append(entry)
    ablesci_queue_save(data)
    return {"ok": True, "queued": True, "id": entry["id"],
            "pending": len([x for x in data["items"] if x.get("status") == "pending"])}


PENDING_DL_FILE = os.path.join(DATA_DIR, "pending_downloads.json")
POINTS_FILE = os.path.join(DATA_DIR, "ablesci_points.json")


def ablesci_points():
    """科研通积分缓存（agent 读账号页时顺手写入）。读不到就返回 None。"""
    try:
        j = _read_json(POINTS_FILE, {})
        return int(j.get("points"))
    except Exception:
        return None


def _pending_download_add(doi, title, url):
    """登记「浏览器正在下载这份 PDF」，落盘后由监听线程自动核验+归档+送转写。"""
    items = _read_json(PENDING_DL_FILE, [])
    if not isinstance(items, list):
        items = []
    items.append({"doi": doi, "title": title, "url": url,
                  "created": time.time(),
                  "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    try:
        with open(PENDING_DL_FILE, "w", encoding="utf-8") as f:
            json.dump(items[-20:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def browser_downloads_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads")


_SEEN_DL = set()


def _watch_downloads_once():
    """浏览器下载的 PDF 落盘后：核验 → 按标题归档 → 自动送转写。每 10 秒扫一次。"""
    try:
        items = _read_json(PENDING_DL_FILE, [])
        if not isinstance(items, list) or not items:
            return
        now = time.time()
        keep, done_any = [], False
        ddir = browser_downloads_dir()
        files = []
        try:
            files = [os.path.join(ddir, f) for f in os.listdir(ddir)
                     if f.lower().endswith(".pdf") and not f.lower().endswith(".crdownload")]
        except Exception:
            files = []
        for it in items:
            if now - float(it.get("created") or 0) > 2 * 3600:
                continue     # 两小时还没下下来就别等了
            matched = None
            for p in sorted(files, key=lambda x: os.path.getmtime(x), reverse=True):
                try:
                    if os.path.getsize(p) < 200 * 1024:
                        continue
                    if p in _SEEN_DL:
                        continue
                    if os.path.getmtime(p) < float(it.get("created") or 0) - 60:
                        continue
                except Exception:
                    continue
                v = verify_pdf_file(p, it.get("title") or "", it.get("doi") or "")
                if v and v.get("ok"):
                    matched = p
                    break
            if matched:
                _SEEN_DL.add(matched)
                r = _ingest_pdf(matched, it.get("title") or it.get("doi") or "",
                                transcribe=True, engine="local")
                _log_ablesci("下载全文 | 浏览器下载落盘并归档：%s | %s" % (
                    r.get("name") if r.get("ok") else matched, it.get("doi")))
                done_any = True
            else:
                keep.append(it)
        try:
            with open(PENDING_DL_FILE, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return done_any
    except Exception as e:
        try:
            _log_ablesci("下载监听异常：%s" % e)
        except Exception:
            pass


def start_download_watcher():
    def loop():
        while True:
            try:
                _watch_downloads_once()
            except Exception:
                pass
            time.sleep(10)
    t = threading.Thread(target=loop, name="download-watcher", daemon=True)
    t.start()
    return t


GROUP_KINDS = ("summary", "trans", "read")


def library_groups(kind):
    """摘要卡片 / 译文库 / 精读库 三处的「分组」：用户自己起名字，手动把条目加进去。
    存储结构 {"summary":{"groups":[{"id","name"}],"assign":{"<entryId>":["gid"]}}, ...}。
    为什么放在 server 侧而不是卡片记录里：卡片/译文/精读的 JSON 归 worker 管，
    而且分组是「用户的组织方式」，跟文件本身解耦，删了条目也不会污染文件。"""
    data = _read_json(GROUPS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    kind = kind if kind in GROUP_KINDS else "summary"
    node = data.get(kind) or {}
    groups = node.get("groups") if isinstance(node.get("groups"), list) else []
    assign = node.get("assign") if isinstance(node.get("assign"), dict) else {}
    # 统计每个分组的条目数，并剔除指向已不存在分组的脏数据
    gids = [g.get("id") for g in groups if isinstance(g, dict)]
    counts = {g: 0 for g in gids}
    for eid, lst in assign.items():
        if not isinstance(lst, list):
            continue
        lst = [g for g in lst if g in counts]
        assign[eid] = lst
        for g in lst:
            counts[g] = counts.get(g, 0) + 1
    return {"groups": [{"id": g.get("id"), "name": g.get("name"),
                        "count": counts.get(g.get("id"), 0)} for g in groups if g.get("id")],
            "assign": assign}


def entry_names(kind=None):
    """条目自定义标题（别名）侧表：{"summary":{"<id>":"我的叫法"}, "trans":{...}, "read":{...}}。

    只改展示名，不动原始记录文件，随时可以改回去（name 传空即删除别名）。
    """
    data = _read_json(ENTRY_NAMES_FILE, {})
    if not isinstance(data, dict):
        data = {}
    if kind:
        node = data.get(kind)
        return node if isinstance(node, dict) else {}
    return {k: (v if isinstance(v, dict) else {}) for k, v in data.items()}


def entry_rename(kind, entry_id, name):
    kind = kind if kind in GROUP_KINDS else "summary"
    entry_id = (entry_id or "").strip()
    if not entry_id:
        return {"ok": False, "error": "缺少条目 id"}
    data = _read_json(ENTRY_NAMES_FILE, {})
    if not isinstance(data, dict):
        data = {}
    node = data.get(kind)
    if not isinstance(node, dict):
        node = {}
    name = (name or "").strip()[:60]
    if name:
        node[entry_id] = name
    else:
        node.pop(entry_id, None)
    data[kind] = node
    with open(ENTRY_NAMES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return {"ok": True, "names": node}


def library_groups_save(kind, node):
    data = _read_json(GROUPS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data[kind] = node
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def library_groups_edit(kind, action, name="", group_id="", entry_id="", on=None):
    """一个函数覆盖 建组 / 改名 / 删组 / 条目进出组，避免前端多处拼逻辑。"""
    # 先把 kind 归一化：否则 GET 会兜底到 summary，写成 xxx 这种无名抽屉
    kind = kind if kind in GROUP_KINDS else "summary"
    node = library_groups(kind)
    groups = node["groups"]
    assign = node["assign"]
    name = (name or "").strip()[:24]

    if action == "create":
        if not name:
            return {"ok": False, "error": "分组名不能为空"}
        for g in groups:
            if g["name"] == name:
                return {"ok": True, "groups": groups, "assign": assign, "groupId": g["id"], "duplicated": True}
        gid = "g" + time.strftime("%Y%m%d%H%M%S") + "-" + str(len(groups))
        groups.append({"id": gid, "name": name, "count": 0})
        library_groups_save(kind, {"groups": [{"id": g["id"], "name": g["name"]} for g in groups],
                                   "assign": assign})
        # 建完顺手把条目放进去（前端一次点击就能「新建分组并加入」）
        if entry_id:
            assign.setdefault(entry_id, [])
            if gid not in assign[entry_id]:
                assign[entry_id].append(gid)
            library_groups_save(kind, {"groups": [{"id": g["id"], "name": g["name"]} for g in groups],
                                       "assign": assign})
        return {"ok": True, "groupId": gid, **library_groups(kind)}

    if action == "rename":
        if not any(g["id"] == group_id for g in groups):
            return {"ok": False, "error": "没有这个分组"}
        if not name:
            return {"ok": False, "error": "分组名不能为空"}
        for g in groups:
            if g["id"] == group_id:
                g["name"] = name
        library_groups_save(kind, {"groups": [{"id": g["id"], "name": g["name"]} for g in groups],
                                   "assign": assign})
        return {"ok": True, **library_groups(kind)}

    if action == "delete":
        if not any(g["id"] == group_id for g in groups):
            return {"ok": False, "error": "没有这个分组"}
        groups = [g for g in groups if g["id"] != group_id]
        for eid in list(assign.keys()):
            assign[eid] = [g for g in assign[eid] if g != group_id]
            if not assign[eid]:
                assign.pop(eid, None)
        library_groups_save(kind, {"groups": [{"id": g["id"], "name": g["name"]} for g in groups],
                                   "assign": assign})
        return {"ok": True, **library_groups(kind)}

    if action == "assign":
        if not any(g["id"] == group_id for g in groups):
            return {"ok": False, "error": "没有这个分组"}
        if not entry_id:
            return {"ok": False, "error": "缺少条目 id"}
        lst = assign.get(entry_id) or []
        want = bool(on)
        lst = [g for g in lst if g != group_id]
        if want:
            lst.append(group_id)
        if lst:
            assign[entry_id] = lst
        else:
            assign.pop(entry_id, None)
        library_groups_save(kind, {"groups": [{"id": g["id"], "name": g["name"]} for g in groups],
                                   "assign": assign})
        return {"ok": True, **library_groups(kind)}

    return {"ok": False, "error": "未知操作"}


def safe_pdf_name(title, fallback="paper"):
    """把论文标题当文件名：去掉 Windows 非法字符，压掉多余空格，限长 140 字。
    为什么要清洗：标题里常有「:」「/」「?」，直接落盘会创建失败或被截断。"""
    name = (title or "").strip() or fallback
    name = _WIN_BAD_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > 140:
        name = name[:140].rstrip(" .")
    return name + ".pdf"


def _auto_archive_enabled():
    """「每日自动 git 存档」的开关，**默认关闭**（data/settings.json 里 auto_archive=true 才启用）。
    为什么默认关：这个工作台会被别人 clone 到自己机器上，而存档执行的是 `git add -A`——
    会把使用者当时**未提交的改动一并提交**，混进他自己的提交历史里，且失败只在日志里。
    只有明确知道「这个目录就是我自己的 git 仓库」时才该打开。每次现读配置，改完不用重启。"""
    return bool(_load_settings().get("auto_archive"))


def _git_auto_commit(today):
    """把代码与轻量数据自动提交（pdf_jobs 已被 .gitignore 排除，提交很轻）。
    仅在 data/settings.json 的 auto_archive=true 时被调用（见 _auto_archive_enabled）。
    注意：必须检查 returncode——曾经因 .git/index.lock 陈旧残留导致
    提交静默失败而日志仍报「完成」，自动备份形同虚设。"""
    try:
        # 陈旧锁（>10 分钟，通常是崩溃/超时残留）自动清理，否则自动存档会永久失败
        lock = os.path.join(BASE_DIR, ".git", "index.lock")
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) > 600:
            try:
                os.remove(lock)
                _log_maint("git：清理陈旧 index.lock")
            except OSError:
                pass
        r = subprocess.run(["git", "add", "-A"], cwd=BASE_DIR,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            _log_maint("git：add 失败 " + (r.stderr or "")[:200])
            return False
        st = subprocess.run(["git", "status", "--porcelain"], cwd=BASE_DIR,
                            capture_output=True, text=True, timeout=30)
        if not st.stdout.strip():
            _log_maint("git：工作区干净，跳过提交")
            return True
        c = subprocess.run(["git", "commit", "-m", "自动存档 " + today],
                           cwd=BASE_DIR, capture_output=True, text=True, timeout=120)
        if c.returncode != 0:
            _log_maint("git：commit 失败 " + ((c.stderr or "") + (c.stdout or ""))[:200])
            return False
        _log_maint("git：自动存档完成")
        return True
    except Exception as e:
        _log_maint("git：存档失败 " + repr(e))
        return False


def _data_snapshot(today, keep_days=14):
    """把 data/ 下所有 *.json 与 *.md 轻量镜像到 data_snapshots/<日期>/
    （转写文本与摘要都在内；input.pdf/images 等重资源不备份），并清理过期快照。
    密钥类配置（SNAPSHOT_SKIP_FILES）不进快照——避免把 api_key 复制成多份明文。"""
    dst = os.path.join(SNAPSHOT_ROOT, today)
    for root, _, files in os.walk(DATA_DIR):
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in (".json", ".md"):
                continue
            if fn in SNAPSHOT_SKIP_FILES:
                continue
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, DATA_DIR)
            rel_parts = rel.split(os.sep)
            # pdf_jobs 只备份 job 顶层的小文件（job.json/summary/source/result.md），
            # local_out|cloud_out 深处的布局中间 json（input_middle 等）可重建，不备份
            if rel_parts[0] == "pdf_jobs" and len(rel_parts) > 3:
                continue
            dstp = os.path.join(dst, rel)
            try:
                os.makedirs(os.path.dirname(dstp), exist_ok=True)
                shutil.copy2(src, dstp)
            except OSError:
                pass
    _log_maint("快照完成: %s（%.1f MB）" % (today, _dir_size(dst) / 1048576.0))
    cutoff = time.time() - keep_days * 86400
    try:
        for name in os.listdir(SNAPSHOT_ROOT):
            p = os.path.join(SNAPSHOT_ROOT, name)
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                _log_maint("清理过期快照: " + name)
    except OSError:
        pass


def _prune_jobs():
    """同名论文（目录名去掉 _日期_时间_哈希 后缀）只保留最新 job 的重资源：
    旧 job 删除 input.pdf 与 images/，保留 *.json 与 *.md（转写文本仍可回看）"""
    jobs_dir = os.path.join(DATA_DIR, "pdf_jobs")
    try:
        names = [n for n in os.listdir(jobs_dir) if os.path.isdir(os.path.join(jobs_dir, n))]
    except OSError:
        return
    groups = {}
    for n in names:
        m = JOB_TS_RE.match(n)
        if m:
            groups.setdefault(m.group(1), []).append(n)
    freed = 0
    for slug, dirs in groups.items():
        if len(dirs) < 2:
            continue
        dirs.sort()                       # 目录名含时间戳，字符串序即时间序
        for old in dirs[:-1]:
            d = os.path.join(jobs_dir, old)
            before = _dir_size(d)
            try:
                ip = os.path.join(d, "input.pdf")
                if os.path.exists(ip):
                    os.remove(ip)
                img = os.path.join(d, "images")
                if os.path.isdir(img):
                    shutil.rmtree(img, ignore_errors=True)
                freed += before - _dir_size(d)
            except OSError as e:
                _log_maint("清理 %s 失败 %r" % (old, e))
    if freed:
        _log_maint("pdf_jobs 去重瘦身：回收 %.1f MB" % (freed / 1048576.0))


def _worker_alive():
    """探测 PDF worker（8766）是否在跑。"""
    try:
        req = urllib.request.Request(WORKER_BASE + "/api/pdf/health")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_worker():
    """确保 PDF 转写 worker（8766）在跑：不在则后台拉起。

    server 现在是常驻进程（开机自启 + 保活），它是唯一需要守护的对象；
    让 server 顺带守护 worker，用户就不必再关心「转写引擎忘了启动」
    ——这正是「本地未就绪 · 云端未配置」的常见成因。
    """
    if _worker_alive():
        return True
    if not (os.path.isfile(WORKER_PY) and os.path.isfile(WORKER_SCRIPT)):
        print("[worker] 未找到 venv 或 worker.py，跳过守护", flush=True)
        return False
    try:
        log = open(os.path.join(DATA_DIR, "pdf_worker.log"), "a")
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
             else {"start_new_session": True}
        subprocess.Popen([WORKER_PY, WORKER_SCRIPT], cwd=BASE_DIR,
                         stdout=log, stderr=log, **kw)
        print("[worker] 检测到 8766 未运行，已后台拉起", flush=True)
        return True
    except Exception as e:
        print("[worker] 拉起失败：%r" % e, flush=True)
        return False


def _maintenance_loop():
    while True:
        try:
            ensure_worker()          # 每分钟自愈检查：worker 挂了自动拉起
            if time.localtime().tm_hour >= 3:
                today = time.strftime("%Y-%m-%d")
                state = _read_json(BACKUP_STATE_FILE, {})
                if state.get("last_date") != today:
                    # git 存档是可选功能（默认关）：别人的 clone 里不该被自动提交
                    if _auto_archive_enabled():
                        _git_auto_commit(today)
                    else:
                        _log_maint("git：自动存档未启用（data/settings.json 的 auto_archive），已跳过")
                    _data_snapshot(today)
                    _prune_jobs()
                    _write_json(BACKUP_STATE_FILE, {
                        "last_date": today,
                        "last_run": time.strftime("%Y-%m-%d %H:%M"),
                    })
        except Exception as e:
            _log_maint("维护线程异常 " + repr(e))
        time.sleep(60)   # 每分钟醒一次：worker 守护需要较高检查频率（每日维护靠 last_date 去重）


def main():
    _ensure_data_dir()
    # 启动即确保转写引擎就绪，并交给维护线程持续守护
    ensure_worker()
    # 每日自动维护（git 存档 + 轻量快照 + pdf_jobs 瘦身），随服务常驻
    threading.Thread(target=_maintenance_loop, daemon=True).start()
    # 求助队列的「自动争取 OA」线程：每 60 秒扫一次，能直下的两分钟内就归档并送转写
    start_queue_autopass()
    # 「下载全文」里被出版商拦住的 OA 直链交给浏览器下，落盘后由这条线程自动归档+送转写
    start_download_watcher()
    # 文献库里只要还有没转写过的 PDF，就自动提交转写（不用再逐个点「转写」）
    start_inbox_autotranscribe()
    # 热点流两个窗口的落盘缓存：缺哪个就后台补哪个，这样重启后首屏和「近两天池子」都不用等
    for _d in (1, 2):
        if not _load_feed_disk(_d)[0]:
            threading.Thread(target=_feed_refresh_bg, args=(_d,), daemon=True).start()
    # 首次运行生成默认待办样例
    if not os.path.exists(TODOS_FILE):
        save_todos([
            {"id": 1, "text": "阅读一篇本领域论文并做精读笔记", "done": False, "created": "2026-01-01 09:00", "deadline": "", "priority": "高"},
            {"id": 2, "text": "把想到的问题记进研究日志", "done": True, "created": "2026-01-01 09:00", "deadline": "", "priority": "中"},
        ])
    # 默认只监听本机回环；加 --lan（或设环境变量 WORKBENCH_HOST=0.0.0.0）才对局域网开放，
    # 这样同一 Wi-Fi 下的手机/朋友的电脑也能打开 http://<本机内网IP>:8765
    host = os.environ.get("WORKBENCH_HOST", "").strip() or ("0.0.0.0" if "--lan" in sys.argv else "127.0.0.1")
    server = ThreadingHTTPServer((host, PORT), Handler)
    print("=" * 56)
    print("  学术工作台已启动")
    print(f"  本机打开： http://127.0.0.1:{PORT}")
    if host == "0.0.0.0":
        # 列出所有可能的局域网地址，并跳过 VPN/虚拟网卡常占的 198.18/198.19 段
        _ips = []
        try:
            for _info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                _a = _info[4][0]
                if _a.startswith(("127.", "169.254.", "198.18.", "198.19.")):
                    continue
                if _a not in _ips:
                    _ips.append(_a)
        except Exception:
            pass
        if not _ips:
            try:
                _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _s.connect(("8.8.8.8", 80))
                _ips = [_s.getsockname()[0]]
                _s.close()
            except Exception:
                _ips = []
        for _a in _ips:
            print(f"  同一 Wi-Fi 下可访问： http://{_a}:{PORT}")
        if not _ips:
            print("  未取到内网地址，可用 ipconfig 查看本机 IPv4 后拼上 :8765")
    else:
        print("  （仅本机可访问；要让同一 Wi-Fi 的设备访问，用 python server.py --lan）")
    print("  按 Ctrl+C 停止服务")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
