# pairwise-lru

一个纯内存的缓存淘汰内核：固定容量下的 LRU 次序维护、容量淘汰、TTL 过期与容量核算、
命中率统计，以及整批要么都生效、要么一条也不写的批量写入。
时间不是真实读数，而是调用方注入的整数刻度时钟，同一串调用永远得到同样的结果。

只依赖 Python 标准库，不联网、不落盘、不需要安装任何第三方包。

## 目录结构

- `lru/core.py` ：内核，包含 Clock、LRUCache 与参数校验
- `tests/test_core.py` ：内核的行为测试

## 运行测试

在项目根目录执行：

```
python3 -m unittest discover -s tests -v
```

Windows 上如果 `python3` 不在 PATH 中，可以换成完整路径的 Python 解释器，
例如：

```
C:/Users/<你>/AppData/Local/Programs/Python/Python313/python.exe -m unittest discover -s tests -v
```

测试全部通过时，unittest 结尾会打印 `OK`。
