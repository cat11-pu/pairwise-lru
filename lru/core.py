"""内存版缓存淘汰内核：LRU 次序、容量淘汰、TTL 过期、命中率与批量写入。

这个内核只做一件事：在固定容量里按最近使用次序维护键值条目。全部状态活在进程内存中，
不接任何外部系统；时间也不是真实读数，而是调用方通过 Clock 显式推进的整数刻度，所以
同一串调用无论跑多少遍，得到的次序、计数和回收记录都完全一样。

行为约定
--------
次序
    order() 从最近使用排到最久未使用，只列当前有效的键。get 命中、put 写入（包含更新
    已经存在的键）都会把该键提升到最近使用的位置；peek 只看一眼，不改变次序。
容量
    容量上限只统计未过期的条目。put 一个新键之前先腾位：占位数量达到容量时淘汰最久
    未使用的条目，被淘汰的键按顺序记在 evicted() 里。
过期
    ttl 以整数刻度计，写入时的到期刻是 now + ttl，ttl 为 None 表示永不过期；到期刻
    当刻即失效，now >= expires_at 就算过期。过期条目不再占用容量，也不出现在 order()
    与 size() 里；读到它时会被真正摘掉并记入 expired()。
更新
    对已经存在的键再写一次：值被覆盖，到期刻按本次传入的 ttl 重算（None 表示从此不再
    过期），该键升到最近使用的位置。
统计
    hits 与 misses 只由 get 驱动，peek 一概不计；get 没能取到有效值就算一次未命中。
    hit_rate() 等于 hits / (hits + misses)，一次查询都没有时是 0.0。
批量
    put_many 整批要么都生效、要么一条也不写：其中任何一条参数不合法，缓存都保持原样。
"""

DEFAULT_CAPACITY = 4


class CacheError(Exception):
    """缓存用法错误：容量、键、值或 ttl 不合法。"""


class Clock:
    """可注入的逻辑时钟：只由调用方推进，内核不读真实时间。"""

    def __init__(self, start=0):
        self._now = int(start)

    def now(self):
        """当前刻度。"""
        return self._now

    def advance(self, ticks=1):
        """把时钟往前推 ticks 拍，返回推进后的刻度。"""
        ticks = int(ticks)
        if ticks < 0:
            raise CacheError("ticks 不能为负：%r" % (ticks,))
        self._now += ticks
        return self._now


class _Entry:
    """一条缓存记录：值加上到期刻，到期刻为 None 表示永不过期。"""

    __slots__ = ("key", "value", "expires_at")

    def __init__(self, key, value, expires_at):
        self.key = key
        self.value = value
        self.expires_at = expires_at

    def __repr__(self):
        return "<条目 %s expires_at=%r>" % (self.key, self.expires_at)


def _check_key(key):
    """键必须是非空字符串。"""
    if not isinstance(key, str) or not key:
        raise CacheError("key 必须是非空字符串：%r" % (key,))
    return key


def _check_value(value):
    """值必须是字符串。"""
    if not isinstance(value, str):
        raise CacheError("value 必须是字符串：%r" % (value,))
    return value


def _check_ttl(ttl):
    """ttl 必须是 None（永不过期）或非负整数刻度。"""
    if ttl is None:
        return None
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        raise CacheError("ttl 必须是非负整数或 None：%r" % (ttl,))
    return ttl


class LRUCache:
    """固定容量的 LRU 缓存。

    参数：
        capacity  最多容纳多少个未过期条目，必须是正整数。
        clock     可注入的逻辑时钟；缺省新建一个，过期判定以它的刻度为准。
    """

    def __init__(self, capacity=DEFAULT_CAPACITY, clock=None):
        capacity = int(capacity)
        if capacity < 1:
            raise CacheError("capacity 必须是正整数：%r" % (capacity,))
        self.capacity = capacity
        self.clock = clock if clock is not None else Clock()
        self._entries = {}   # 键 -> 条目，可能暂时留着已经过期但还没回收的条目
        self._order = []     # 最近使用的键在前，最久未使用的键在后
        self._hits = 0
        self._misses = 0
        self._evicted = []   # 因容量被淘汰的键，按淘汰顺序
        self._expired = []   # 因过期被回收的键，按回收顺序

    # ------------------------------------------------------------ 读路径

    def get(self, key):
        """取值：命中返回对应值，未命中返回 None。"""
        key = _check_key(key)
        now = self.clock.now()
        entry = self._entries.get(key)
        if entry is not None and not self._is_expired(entry, now):
            self._hits += 1
            self._touch(key)
            return entry.value
        if entry is None:
            self._misses += 1
            return None
        self._misses += 1
        self._reclaim(key)
        return None

    def peek(self, key):
        """只看一眼：返回当前值，供只读观察用。"""
        key = _check_key(key)
        entry = self._entries.get(key)
        if entry is None or self._is_expired(entry, self.clock.now()):
            return None
        return entry.value

    # ------------------------------------------------------------ 写路径

    def put(self, key, value, ttl=None):
        """写入或更新一个键，返回写入的值。"""
        key = _check_key(key)
        value = _check_value(value)
        ttl = _check_ttl(ttl)
        now = self.clock.now()
        entry = self._entries.get(key)
        if entry is None:
            self._make_room()
            self._entries[key] = _Entry(key, value, self._deadline(ttl, now))
        else:
            entry.value = value
            entry.expires_at = self._deadline(ttl, now)
        self._touch(key)
        return value

    def put_many(self, items):
        """按顺序写入一批键值对，返回写入的键。

        批次里每一项是 (key, value) 或者 (key, value, ttl)。
        """
        checked = [self._check_item(item) for item in items]
        written = []
        for key, value, ttl in checked:
            self.put(key, value, ttl)
            written.append(key)
        return written

    def delete(self, key):
        """删除一个键；返回删除前它是不是一个有效（未过期）条目。"""
        key = _check_key(key)
        entry = self._entries.get(key)
        if entry is None:
            return False
        live = not self._is_expired(entry, self.clock.now())
        self._entries.pop(key, None)
        if key in self._order:
            self._order.remove(key)
        return live

    def advance(self, ticks=1):
        """推进逻辑时钟，返回当前已经过期、但还留在缓存里的键（从最旧到最新）。"""
        self.clock.advance(ticks)
        now = self.clock.now()
        waiting = []
        for key in reversed(self._order):
            entry = self._entries.get(key)
            if entry is not None and self._is_expired(entry, now):
                waiting.append(key)
        return waiting

    # ------------------------------------------------------------ 观察

    def size(self):
        """当前有效（未过期）条目数。"""
        return self._occupied()

    def order(self):
        """从最近使用到最久未使用的有效键。"""
        now = self.clock.now()
        live = []
        for key in self._order:
            entry = self._entries.get(key)
            if entry is not None and not self._is_expired(entry, now):
                live.append(key)
        return live

    def evicted(self):
        """累计因容量被淘汰的键，按淘汰顺序。"""
        return list(self._evicted)

    def expired(self):
        """累计因过期被回收的键，按回收顺序。"""
        return list(self._expired)

    def hit_rate(self):
        """命中数占查询总数的比例，一次查询都没有时是 0.0。"""
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / float(total)

    def stats(self):
        """各项计数的小快照。"""
        return {
            "entries": self.size(),
            "capacity": self.capacity,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": len(self._evicted),
            "expirations": len(self._expired),
            "hit_rate": self.hit_rate(),
        }

    def __repr__(self):
        return "LRUCache(entries=%d, capacity=%d)" % (self.size(), self.capacity)

    # ------------------------------------------------------------ 内部

    def _check_item(self, item):
        """校验批量写入里的一项，展开成 (key, value, ttl)。"""
        if not isinstance(item, (tuple, list)) or len(item) not in (2, 3):
            raise CacheError("每一项必须是 (key, value) 或 (key, value, ttl)：%r" % (item,))
        key = _check_key(item[0])
        value = _check_value(item[1])
        ttl = _check_ttl(item[2] if len(item) == 3 else None)
        return (key, value, ttl)

    def _deadline(self, ttl, now):
        """把 ttl 换算成到期刻，None 表示永不过期。"""
        if ttl is None:
            return None
        return now + ttl

    def _is_expired(self, entry, now):
        """条目在给定的刻度上是不是已经过期。"""
        return entry.expires_at is not None and now >= entry.expires_at

    def _touch(self, key):
        """把键提升到最近使用的位置。"""
        if key in self._order:
            self._order.remove(key)
        self._order.insert(0, key)

    def _reclaim(self, key):
        """把一条已经过期的条目从常驻表和次序里摘掉，并记入过期回收。"""
        entry = self._entries.pop(key, None)
        if key in self._order:
            self._order.remove(key)
        if entry is not None:
            self._expired.append(key)
        return entry

    def _occupied(self):
        """当前占用容量的条目数。"""
        now = self.clock.now()
        return sum(1 for entry in self._entries.values() if not self._is_expired(entry, now))

    def _make_room(self):
        """容量不够就淘汰条目，直到占位数量腾出一个空位。"""
        now = self.clock.now()
        for key in list(self._order):
            entry = self._entries.get(key)
            if entry is not None and self._is_expired(entry, now):
                self._reclaim(key)
        while self._occupied() >= self.capacity:
            self._evict_one()

    def _evict_one(self):
        """淘汰一条条目，返回被淘汰的键。"""
        victim = self._order[-1]
        self._entries.pop(victim, None)
        self._order.pop()
        self._evicted.append(victim)
        return victim
