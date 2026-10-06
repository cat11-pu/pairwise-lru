"""lru：纯内存的缓存淘汰内核。

对外入口：
    LRUCache         固定容量的 LRU 缓存：次序维护、容量淘汰、TTL 过期、命中率统计
    Clock            可注入的逻辑时钟，过期判定以它的整数刻度为准
    DEFAULT_CAPACITY 缺省容量
    CacheError       用法错误：容量、键、值或 ttl 不合法
"""

from .core import DEFAULT_CAPACITY, CacheError, Clock, LRUCache

__all__ = [
    "DEFAULT_CAPACITY",
    "CacheError",
    "Clock",
    "LRUCache",
]
