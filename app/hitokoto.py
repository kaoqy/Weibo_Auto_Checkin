"""每日一言 / 励志句子获取模块（v7.0）。

用于 Telegram 推送时附带一句每日一言，增加趣味性。
- 优先走公开免费接口（hitokoto.cn），失败自动回退到内置句库。
- 内置句库保证离线/无外网环境也能正常工作（本项目服务器可能无外网直连）。
- 结果按天缓存，避免同一天重复请求外部接口。
"""
from __future__ import annotations

import logging
import random
from datetime import date

import requests

log = logging.getLogger("weibo.hitokoto")

# 公开免费接口（无需鉴权）。c 参数筛选分类：d=文学 i=诗词 k=哲学
HITOKOTO_API = "https://v1.hitokoto.cn/?c=d&c=i&c=k&encode=json"

# 内置兜底句库（无外网时使用）
FALLBACK_QUOTES = [
    ("每一个不曾起舞的日子，都是对生命的辜负。", "尼采"),
    ("路虽远行则将至，事虽难做则必成。", "荀子"),
    ("你若决定灿烂，山无遮，海无拦。", "网络"),
    ("不要因为走得慢而灰心，要因为还在走而骄傲。", "网络"),
    ("生活不会因为你善良就对你手下留情，但你依然可以选择温柔。", "网络"),
    ("种一棵树最好的时间是十年前，其次是现在。", "谚语"),
    ("行动是治愈恐惧的良药。", "威廉·詹姆斯"),
    ("日拱一卒无有尽，功不唐捐终入海。", "网络"),
    ("心之所向，身之所往，终至所归。", "网络"),
    ("所有的坚持都值得尊敬，所有的努力都不会白费。", "网络"),
    ("愿你所有的坚持都能开出花来。", "网络"),
    ("慢慢来，一切都来得及。", "网络"),
    ("凡是过往，皆为序章。", "莎士比亚"),
    ("星光不问赶路人，时间不负有心人。", "网络"),
    ("与其感慨路难行，不如马上出发。", "网络"),
]

_cache: dict[str, tuple[str, str]] = {}


def _pick_fallback() -> tuple[str, str]:
    """按当天日期确定性挑选一句（同一天结果稳定）。"""
    idx = date.today().toordinal() % len(FALLBACK_QUOTES)
    return FALLBACK_QUOTES[idx]


def fetch_quote(timeout: int = 8, use_cache: bool = True) -> tuple[str, str]:
    """获取一句每日一言，返回 (正文, 出处)。

    永不抛异常：外部接口不可用时回退内置句库。
    """
    today = date.today().isoformat()
    if use_cache and today in _cache:
        return _cache[today]

    text, source = "", ""
    try:
        resp = requests.get(HITOKOTO_API, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            text = (data.get("hitokoto") or "").strip()
            source = (data.get("from") or "").strip()
            if data.get("from_who"):
                who = str(data["from_who"]).strip()
                source = f"{who}《{source}》" if source else who
    except Exception as exc:  # 网络不可用属正常情况，不打 error 噪音
        log.info("每日一言接口不可用（%s），使用内置句库", type(exc).__name__)

    if not text:
        text, source = _pick_fallback()

    result = (text, source)
    if use_cache:
        _cache[today] = result
    return result


def format_quote(text: str = "", source: str = "") -> str:
    """格式化成推送用的一行文案。"""
    if not text:
        text, source = fetch_quote()
    line = f"💬 每日一言\n{text}"
    if source:
        line += f"\n—— {source}"
    return line


def clear_cache() -> None:
    """清空当日缓存（测试或手动刷新用）。"""
    _cache.clear()
