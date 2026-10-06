"""lru.core 的行为测试：次序维护、容量淘汰、TTL 过期、命中率与批量写入。"""

import unittest

from lru.core import CacheError, Clock, LRUCache


class LRUCacheCoreTest(unittest.TestCase):
    """覆盖正常路径、边界输入、不变量与异常路径。"""

    def setUp(self):
        self.clock = Clock()

    def test_puts_keep_order_and_capacity_evicts_the_oldest(self):
        """连续写入按最近使用排序，容量满了先淘汰最久未使用的条目。"""
        cache = LRUCache(capacity=3, clock=self.clock)
        for key in ("a", "b", "c"):
            cache.put(key, "v-" + key)
        self.assertEqual(cache.order(), ["c", "b", "a"])
        cache.put("d", "v-d")
        self.assertEqual(cache.order(), ["d", "c", "b"])
        self.assertEqual(cache.evicted(), ["a"])
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.size(), 3)

    def test_hit_promotes_the_entry_so_it_survives_later_eviction(self):
        """读命中把条目提升为最近使用，之后的淘汰不会先动它。"""
        cache = LRUCache(capacity=3, clock=self.clock)
        for key in ("a", "b", "c"):
            cache.put(key, "v-" + key)
        self.assertEqual(cache.get("a"), "v-a")
        self.assertEqual(cache.order(), ["a", "c", "b"])
        cache.put("d", "v-d")
        self.assertEqual(cache.order(), ["d", "a", "c"])
        self.assertEqual(cache.evicted(), ["b"])

    def test_peek_observes_without_touching_order_or_counters(self):
        """peek 是纯观察：不提升次序，也不计入命中与未命中。"""
        cache = LRUCache(capacity=3, clock=self.clock)
        for key in ("a", "b", "c"):
            cache.put(key, "v-" + key)
        self.assertEqual(cache.peek("a"), "v-a")
        self.assertEqual(cache.order(), ["c", "b", "a"])
        self.assertIsNone(cache.peek("missing"))
        self.assertEqual(cache.order(), ["c", "b", "a"])
        stats = cache.stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)

    def test_expired_entries_do_not_hold_capacity(self):
        """过期条目不占容量，写入新键时不该挤掉任何有效条目。"""
        cache = LRUCache(capacity=2, clock=self.clock)
        cache.put("a", "v-a", ttl=2)
        cache.put("b", "v-b")
        cache.advance(1)
        cache.put("a", "v-a2", ttl=2)
        self.assertEqual(cache.order(), ["a", "b"])
        self.assertEqual(cache.advance(2), ["a"])
        self.assertEqual(cache.order(), ["b"])
        cache.put("c", "v-c")
        self.assertEqual(cache.order(), ["c", "b"])
        self.assertEqual(cache.evicted(), [])
        self.assertEqual(cache.size(), 2)

    def test_entry_dies_at_its_deadline_tick(self):
        """到期刻当刻即失效：时钟走到那一拍，条目就该读不到了。"""
        cache = LRUCache(capacity=4, clock=self.clock)
        cache.put("a", "v-a", ttl=3)
        cache.put("b", "v-b", ttl=10)
        self.assertEqual(cache.advance(3), ["a"])
        self.assertEqual(cache.size(), 1)
        self.assertIsNone(cache.peek("a"))
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.expired(), ["a"])
        self.assertEqual(cache.order(), ["b"])

    def test_hit_rate_matches_the_reads(self):
        """命中率只由 get 驱动，读到过期条目要按未命中计。"""
        cache = LRUCache(capacity=2, clock=self.clock)
        cache.put("a", "v-a", ttl=2)
        self.assertEqual(cache.get("a"), "v-a")
        self.assertIsNone(cache.get("missing"))
        self.assertEqual(cache.advance(2), ["a"])
        self.assertIsNone(cache.get("a"))
        stats = cache.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 2)
        self.assertAlmostEqual(stats["hit_rate"], 1 / 3, places=6)
        cache.put("b", "v-b")
        self.assertEqual(cache.peek("b"), "v-b")
        self.assertEqual(cache.stats()["hits"], 1)

    def test_batch_write_is_all_or_nothing(self):
        """批量写入先整批校验：有一条不合法，整批都不生效。"""
        cache = LRUCache(capacity=3, clock=self.clock)
        cache.put("a", "v-a")
        with self.assertRaises(CacheError):
            cache.put_many([("b", "v-b"), ("c", "v-c"), (123, "v-d")])
        self.assertEqual(cache.order(), ["a"])
        self.assertEqual(cache.size(), 1)
        self.assertEqual(cache.evicted(), [])
        with self.assertRaises(CacheError):
            cache.put_many([("b", "v-b"), ("c", 7)])
        self.assertEqual(cache.order(), ["a"])
        self.assertEqual(cache.put_many([("x", "v-x"), ("y", "v-y")]), ["x", "y"])
        self.assertEqual(cache.order(), ["y", "x", "a"])

    def test_failed_batch_leaves_capacity_and_order_untouched(self):
        """整批失败时连淘汰都不能发生：缓存原样保留。"""
        cache = LRUCache(capacity=2, clock=self.clock)
        cache.put("a", "v-a")
        cache.put("b", "v-b")
        with self.assertRaises(CacheError):
            cache.put_many([("c", "v-c"), ("d", "v-d"), ("e", None)])
        self.assertEqual(cache.order(), ["b", "a"])
        self.assertEqual(cache.evicted(), [])
        self.assertEqual(cache.size(), 2)

    def test_update_refreshes_expiry_and_promotes(self):
        """重写一个已有键：值替换、到期刻按新的 ttl 重算、并升到最近使用。"""
        cache = LRUCache(capacity=3, clock=self.clock)
        cache.put("a", "v-a", ttl=5)
        cache.put("b", "v-b")
        cache.put("c", "v-c")
        self.assertEqual(cache.order(), ["c", "b", "a"])
        cache.put("a", "v-a2", ttl=2)
        self.assertEqual(cache.get("a"), "v-a2")
        self.assertEqual(cache.order(), ["a", "c", "b"])
        self.assertEqual(cache.advance(4), ["a"])
        self.assertIsNone(cache.peek("a"))
        cache.put("d", "v-d")
        self.assertEqual(cache.order(), ["d", "c", "b"])
        self.assertEqual(cache.size(), 3)

    def test_batch_ttl_and_capacity_invariant(self):
        """批量写入的 ttl 逐条生效，且有效条目数任何时刻都不超过容量。"""
        cache = LRUCache(capacity=3, clock=self.clock)
        cache.put_many([("a", "v-a", 4), ("b", "v-b"), ("c", "v-c")])
        self.assertEqual(cache.order(), ["c", "b", "a"])
        self.assertEqual(cache.advance(4), ["a"])
        self.assertEqual(cache.size(), 2)
        cache.put_many([("d", "v-d"), ("e", "v-e")])
        self.assertEqual(cache.size(), 3)
        self.assertEqual(cache.order(), ["e", "d", "c"])
        for key in cache.order():
            self.assertIsNotNone(cache.peek(key))


if __name__ == "__main__":
    unittest.main()
